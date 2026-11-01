"""Atomic catalog-grammar import for machine consumers (MCP level 2).

``CatalogImportService`` accepts a fixture-format payload
(``{"objects": [{model, pk?, fields}]}`` — the ``objects`` list reaches
the service), validates it against the catalog model whitelist, resolves
each object to a create or an update, and applies everything inside one
transaction.

Semantics are loaddata-like: an object whose ``pk`` or natural key
matches a live row **updates** that row's fields; no match **creates** a
row. The object's wrapper keys are an allowlist — anything outside
``{model, pk, fields}`` is a per-object validation error, so a
``"delete": true`` hint can never pass silently (deletes are NOT
supported — a payload never removes catalog rows; pruning stays a
manual admin operation).

An update is a **full-row replace**: deserialization fills every field
absent from ``fields`` with the model default, so omitting a field
whose live value is non-default would silently reset it. Such objects
are rejected pre-write with the field names they would reset — omitted
fields whose live value already IS the default stay harmless.

Updates are also gated on **implementation**: a catalog row already
instantiated by schema data (``Node``/``NodeAttribute`` rows, in any
project — the grammar is global and status-agnostic) rejects an
update that changes at least one field, reported as
``catalog_object_in_use`` with a usage breakdown. Creates are never
gated, and neither are no-op updates — re-sending the live values
verbatim keeps a re-import idempotent. The check is an advisory read
inside the transaction, so under READ COMMITTED a schema committed
between the gate and this import's commit escapes verification — a
residual TOCTOU window accepted for v1.

On success the envelope carries ``results`` — one entry per payload
object, in payload order, with ``index``, ``model``, ``natural_key``,
``action`` (``created``/``updated``) and ``changed_fields`` — so the
caller can audit exactly which row each object touched. ``warnings``
reports the non-fatal payload normalizations applied pre-write: an
``attributedef`` declaring ``is_required`` on a server-managed attribute
is demoted to ``False``, never rejected. Catalog grammar is global and
takes effect immediately (no draft lifecycle); every import is
attributed to the calling user through an admin ``LogEntry``.
"""

import re
import uuid
from collections import Counter
from typing import TYPE_CHECKING

from django.core.serializers.base import DeserializationError
from django.db import DatabaseError, IntegrityError, transaction

from ..constants import (
    ERR_CATALOG_OBJECT_IN_USE,
    ERR_CATALOG_OBJECT_IN_USE_MSG,
    ERR_INVALID_CATALOG_OBJECT,
    ERR_MISSING_MODEL_PERMISSION,
    ERR_UPDATE_RESET_FIELDS,
    WARN_IS_REQUIRED_DEMOTED,
)
from ..repositories.catalog_usage_repository import CatalogUsageRepository
from ..repositories.fixture_repository import FixtureRepository
from .datatype_plugins import is_server_managed_attribute
from .fixture_service import FixtureService

if TYPE_CHECKING:
    from ..repositories.user_repository import User

# Catalog grammar a level-2 token may author. ``datatype`` is excluded:
# DataTypes govern engine storage/validation semantics that a JSON row
# cannot declare. organization/project/node/nodeattribute are outside
# the catalog domain entirely.
CATALOG_MODELS = {
    'domain', 'domainitem', 'nodetype', 'nodetypecomposition',
    'nodetypevariant', 'attributedef',
}

# Dependency rank for the apply pass — stable-sorted so natural-key FKs
# resolve against rows written earlier in the same payload even when the
# payload arrives unordered.
_APPLY_RANK = {
    'nodetype': 0,
    'domain': 0,
    'domainitem': 1,
    'nodetypevariant': 1,
    'nodetypecomposition': 1,
    'attributedef': 2,
}

# Namespace for deterministic uuid5 PKs — the same convention the
# canonical fixture seeds use, so a re-imported object lands on the same
# primary key (idempotent).
SEED_NS = uuid.uuid5(uuid.NAMESPACE_DNS, 's7.oaxaca.gob.mx')

# Prefix emitted by FixtureService.validate_objects for per-object errors.
_OBJECT_INDEX_RE = re.compile(r'^object\[(\d+)\]:\s*')

# Trailing marker Django's DeserializationError.WithData stamps on its
# message — it carries the raw field value that failed conversion.
_FIELD_VALUE_RE = re.compile(r"field_value was '(.*)'\s*$")


class CatalogImportService:
    """Validate and apply a fixture-format catalog payload atomically."""

    def __init__(self) -> None:
        self.fixture_service = FixtureService()
        self.fixture_repository = FixtureRepository()
        self.usage_repository = CatalogUsageRepository()

    @transaction.atomic
    def import_objects(self, objects: list, user: 'User') -> dict:
        """Validate + apply a fixture-format catalog payload atomically.

        Args:
            objects: the ``objects`` list of the request payload; each
                entry is ``{model: "schemas.<label>", pk?: str, fields:
                {...}}``. ``pk`` is optional — a deterministic uuid5 by
                natural key is generated when absent.
            user: the authenticated user; per-model admin permissions
                (``schemas.add_<label>`` / ``schemas.change_<label>``)
                gate each planned write.

        Returns:
            On success ``{"applied": True, "created": {label: n},
            "updated": {label: n}, "warnings": [{"code", "message",
            "path"}], "results":
            [{"index", "model", "natural_key", "action",
            "changed_fields"}]}`` — one ``results`` entry per payload
            object, in payload order; on any validation or apply
            failure ``{"applied": False, "errors": [{"code",
            "message", "path"}], "warnings": []}`` with no partial
            writes (the whole transaction rolls back). Errors carry
            ``path: $.objects[i]`` locating the offending entry, and
            apply-time failures attribute the ORM error to the object —
            and, when determinable, the field — that broke, so a raw
            conversion message never reaches the client bare.

            ``warnings`` reports the non-fatal normalizations applied to
            the payload pre-write: an ``attributedef`` declaring
            ``is_required`` on a server-managed attribute (a generated
            ``natural_*`` column or ``display_order`` — the engine
            ignores the payload value entirely, so the flag can never be
            honored) is demoted to ``False`` and reported with its
            object path. Author-settable column-mapped attributes
            (``natural_key`` → ``key``, ``natural_version`` →
            ``version``) keep the flag — it is legitimate contract
            metadata the resolved catalog emits as ``required: true``.

            An object matched to a live row is updated by full-row
            replace: omitting a field that holds a non-default live
            value is a per-object validation error (the would-be reset
            fields are named), not a silent reset.

            An update is further gated on implementation: when schema
            data already instantiates the matched catalog row
            (``Node``/``NodeAttribute`` rows in any project — the
            grammar is global), an update changing at least one field
            is rejected with ``catalog_object_in_use``, and its error
            entry carries an extra ``usage`` key (``{<metric>: int,
            "projects": [slugs]}``) detailing what implements it.
            Creates and no-op updates — every payload field equal to
            the live value — are never gated, so re-importing the
            catalog verbatim stays possible once schemas exist. The
            usage check is an advisory read under READ COMMITTED: a
            schema committed between the gate and this import's commit
            escapes it, a residual TOCTOU window accepted for v1.

            Classification and apply run in one transaction under READ
            COMMITTED, so a row matched as an update may still be
            deleted by a concurrent transaction before its write — the
            save then inserts it. Existence is re-checked per update
            object right before the save — and the reclassified insert
            must then pass the ``add_<label>`` check the permission
            pass gated only as ``change_<label>`` — so the envelope
            reports the action actually taken.

            The payload is mutated in place: generated uuid5 pks are
            written into objects that omit ``pk``, objects whose
            natural key already exists get ``pk`` rewritten to the live
            row's pk so deserialization updates it, and unsatisfiable
            ``is_required`` flags (see ``warnings``) are demoted to
            ``False``.

        Raises:
            PermissionError: the user lacks a required per-model
                permission; the message names codename and model. Never
                partial — a mid-apply raise (a reclassified insert
                missing ``add``) rolls back the writes before it with
                the whole transaction.
        """
        # PK generation runs before validation: validate_objects requires
        # a UUID pk on every entry, so fulfilling the 'pk optional'
        # contract must come first. Malformed entries are skipped here
        # and reported by validation with a precise path.
        self._assign_missing_pks(objects)

        errors = self.fixture_service.validate_objects(
            objects, allowed_models=CATALOG_MODELS,
        )
        if errors:
            return self._error_envelope(
                [self._to_error_entry(e) for e in errors],
            )

        # Server-managed required demotion — before classification so
        # the permission pass, the reset guard, the apply pass and the
        # reported ``changed_fields`` all operate on the flag actually
        # persisted. The pass mutates ``fields`` in place and returns
        # the warnings the success envelope reports.
        warnings = self._demote_server_managed_required(objects)

        # Existence + pk-resolution pass. A pk pointing at one row while
        # the natural key resolves to another is a payload conflict —
        # reported per object instead of surfacing as a raw constraint
        # error during apply.
        conflicts = []
        classified = []
        for i, obj in enumerate(objects):
            label = obj['model'].split('.')[1]
            fields = obj['fields']
            nk = self.fixture_service.natural_key_for(label, fields)
            pk = obj.get('pk')

            pk_match = (
                pk is not None
                and self.fixture_repository.pk_exists(label, pk)
            )
            nk_pk = (
                self.fixture_repository.get_pk_by_natural_key(
                    label, list(nk),
                )
                if nk is not None else None
            )
            # Canonicalize: the JSON pk may carry a non-canonical UUID
            # spelling while pk_exists matched it case-insensitively.
            pk_norm = str(uuid.UUID(str(pk))) if pk_match else None
            if pk_norm is not None and nk_pk is not None and nk_pk != pk_norm:
                conflicts.append({
                    'code': ERR_INVALID_CATALOG_OBJECT,
                    'message': (
                        f'pk {pk} and natural key {list(nk)} '
                        'resolve to different rows'
                    ),
                    'path': f'$.objects[{i}]',
                })
                continue
            classified.append((i, obj, label, pk_norm or nk_pk))

        if conflicts:
            return self._error_envelope(conflicts)

        # Permission pass — still before any write: the first missing
        # permission aborts the import with an empty transaction.
        created: Counter = Counter()
        updated: Counter = Counter()
        for _, obj, label, existing_pk in classified:
            if existing_pk is not None:
                self._require_perm(user, 'change', label)
                # Point the object at the live row so deserialization
                # updates it instead of colliding on the natural key.
                obj['pk'] = existing_pk
                updated[label] += 1
            else:
                self._require_perm(user, 'add', label)
                created[label] += 1

        # Full-row-replace guard — still before any write: an update
        # that omits a field whose live value differs from the model
        # default would silently reset it (deserialization fills absent
        # fields with defaults). Reject such objects naming the fields
        # they would reset; omitted fields whose live value already IS
        # the default are harmless and pass. The live snapshots double
        # as the ``changed_fields`` baseline of the success results.
        snapshots = {}
        defaults_by_label = {}
        reset_errors = []
        for i, obj, label, existing_pk in classified:
            if existing_pk is None:
                continue
            live = self.fixture_repository.get_fixture_state(
                label, existing_pk,
            )
            if live is None:
                # The row vanished between classification and now — the
                # apply pass will insert it instead of updating, so
                # there is no baseline to reset against.
                continue
            snapshots[i] = live
            if label not in defaults_by_label:
                defaults_by_label[label] = (
                    self.fixture_repository.get_default_fixture_fields(
                        label,
                    )
                    or {}
                )
            defaults = defaults_by_label[label]
            reset_fields = sorted(
                name for name, value in live.items()
                if name not in obj['fields']
                and value != defaults.get(name)
            )
            if reset_fields:
                reset_errors.append({
                    'code': ERR_INVALID_CATALOG_OBJECT,
                    'message': ERR_UPDATE_RESET_FIELDS.format(
                        fields=', '.join(reset_fields),
                    ),
                    'path': f'$.objects[{i}]',
                })
        if reset_errors:
            return self._error_envelope(reset_errors)

        # Usage gate — still before any write: a catalog row already
        # instantiated by schema data (Node/NodeAttribute rows in ANY
        # project — the grammar is global and status-agnostic) rejects
        # an update that changes at least one field, since it would
        # rewrite the meaning of existing schema rows. Creates are
        # never gated, and neither are no-op updates — re-sending the
        # live values verbatim keeps an idempotent re-import working
        # once the first schema exists. The no-op test mirrors the
        # ``changed_fields`` comparison the results pass runs below,
        # over the snapshots the reset guard just built. The check is
        # an advisory read inside this transaction: under READ
        # COMMITTED a schema committed between this pass and the outer
        # commit escapes verification — a residual TOCTOU window
        # accepted for v1.
        usage_errors = []
        for i, obj, label, existing_pk in classified:
            if existing_pk is None:
                # Creates are never gated (D105): adding to the catalog
                # is always allowed, even under implemented parents.
                continue
            live = snapshots.get(i)
            if live is None:
                # The row vanished between classification and the
                # snapshot pass — the apply pass reclassifies the
                # object as an insert, and inserts are never gated.
                continue
            if not any(
                live.get(name) != value
                for name, value in obj['fields'].items()
            ):
                # No-op update (D102): every payload field equals the
                # live value, so the row is exempt even when
                # implemented — a re-import of the catalog cannot be
                # permanently broken by existing schemas.
                continue
            usage = self.usage_repository.usage_for(label, existing_pk)
            if usage['implemented']:
                nk = self.fixture_service.natural_key_for(
                    label, obj['fields'],
                )
                usage_errors.append({
                    'code': ERR_CATALOG_OBJECT_IN_USE,
                    'message': ERR_CATALOG_OBJECT_IN_USE_MSG.format(
                        label=label,
                        natural_key=list(nk) if nk else [],
                        usage=self._usage_summary(usage['usage']),
                    ),
                    'path': f'$.objects[{i}]',
                    # Extra key beyond code/message/path: the counts
                    # and owning project slugs the caller needs to
                    # understand what blocks the update (D106).
                    'usage': usage['usage'],
                })
        if usage_errors:
            return self._error_envelope(usage_errors)

        # Apply one object at a time in dependency order (the sort is
        # stable, so same-rank objects keep payload order): a failure is
        # mapped back to the payload index of the object that broke —
        # one batch deserialize call would lose that attribution.
        ordered = sorted(
            classified,
            key=lambda entry: _APPLY_RANK[entry[2]],
        )
        # Rows classified as updates may have been deleted after
        # classification — under READ COMMITTED a concurrent DELETE can
        # commit between passes, and the save below then INSERTs the
        # row instead of UPDATEing it. Re-check existence right before
        # each update write and correct the action/counts so the report
        # reflects what is actually written (obj['pk'] already carries
        # the canonical existing_pk from the permission pass).
        inserted: set[int] = set()
        for orig_i, obj, label, existing_pk in ordered:
            try:
                if (existing_pk is not None
                        and not self.fixture_repository.pk_exists(
                            label, obj['pk'],
                        )):
                    # The matched row vanished — the save inserts it,
                    # so this object is a create, not an update. The
                    # real write is an INSERT the permission pass
                    # gated only as ``change_<label>`` — demand
                    # ``add_<label>`` as well, before touching the
                    # report. PermissionError is outside the caught
                    # tuple, so it propagates for a full rollback.
                    self._require_perm(user, 'add', label)
                    inserted.add(orig_i)
                    updated[label] -= 1
                    if not updated[label]:
                        # Counter keeps zeroed keys; drop them so the
                        # envelope never reports ``label: 0``.
                        del updated[label]
                    created[label] += 1
                self.fixture_repository.apply_fixture_objects([obj])
            except (DeserializationError, ValueError, RuntimeError,
                    IntegrityError, DatabaseError) as exc:
                # Caught inside the atomic block: mark it for rollback
                # so the partial writes of this pass cannot commit.
                transaction.set_rollback(True)
                return self._error_envelope([
                    {
                        'code': ERR_INVALID_CATALOG_OBJECT,
                        'message': self._apply_error_message(
                            exc, obj, label,
                        ),
                        'path': f'$.objects[{orig_i}]',
                    },
                ])

        self.fixture_repository.record_catalog_import(
            user, self._change_message(created, updated),
        )

        # Per-object audit trail in payload order — ``classified`` kept
        # the original indices and the pre-apply snapshots pinpoint
        # which incoming fields actually differ from the live row.
        results = []
        for i, obj, label, existing_pk in classified:
            nk = self.fixture_service.natural_key_for(
                label, obj['fields'],
            )
            if existing_pk is None or i in inserted:
                # ``i in inserted``: the row matched as an update was
                # gone by write time and got inserted — every payload
                # field was written and no live baseline exists.
                action = 'created'
                changed_fields = sorted(obj['fields'])
            else:
                action = 'updated'
                live = snapshots.get(i)
                changed_fields = sorted(
                    name for name, value in obj['fields'].items()
                    if live is None or live.get(name) != value
                )
            results.append({
                'index': i,
                'model': label,
                'natural_key': list(nk) if nk else [],
                'action': action,
                'changed_fields': changed_fields,
            })

        return {
            'applied': True,
            'created': dict(sorted(created.items())),
            'updated': dict(sorted(updated.items())),
            'warnings': warnings,
            'results': results,
        }

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    def _assign_missing_pks(self, objects) -> None:
        """Inject deterministic uuid5 PKs into objects that omit one.

        Entries too malformed to derive a natural key are left untouched
        — structural validation reports them with their own path.
        """
        if not isinstance(objects, list):
            return
        for obj in objects:
            if not isinstance(obj, dict) or obj.get('pk') is not None:
                continue
            model = obj.get('model')
            fields = obj.get('fields')
            if (not isinstance(model, str)
                    or not model.startswith('schemas.')
                    or not isinstance(fields, dict)):
                continue
            label = model.split('.')[1]
            if label not in CATALOG_MODELS:
                continue
            nk = self.fixture_service.natural_key_for(label, fields)
            if nk is None:
                continue
            obj['pk'] = self._seed_pk(label, nk)

    @staticmethod
    def _seed_pk(label: str, natural_key: tuple) -> str:
        """Deterministic uuid5 for a row, derived from its natural key."""
        key = ':'.join(
            '' if part is None else str(part) for part in natural_key
        )
        return str(uuid.uuid5(SEED_NS, f'{label}:{key}'))

    def _demote_server_managed_required(self, objects: list) -> list[dict]:
        """Demote ``is_required`` on defs bound to server-managed attributes.

        An ``attributedef`` bound to a server-managed attribute — a
        ``natural_*`` attribute the engine generates (``natural_uuid`` →
        ``id``) or any ``display_order`` — can never honor
        ``is_required``: the payload value is ignored entirely, no
        ``NodeAttribute`` row is ever written for it (post-AI-94 the
        required check excludes the whole column-mapped set), and the
        resolved catalog already emits ``required: false`` for the pair.
        Persisting the flag would store a promise nothing keeps, so it
        is rewritten to ``False`` in place and each demotion is returned
        as a warning — never an error, so a catalog that declares the
        pattern (e.g. ``forms.json``'s ``natural_uuid`` defs)
        re-imports cleanly instead of failing validation.

        The gate is exactly ``is_server_managed_attribute`` — not the
        broader ``is_column_mapped_datatype`` set. Author-settable
        column-mapped attributes (``natural_key`` → ``key``,
        ``natural_version`` → ``version``, ``natural_*`` attributes
        named after writable node columns) keep the flag: the payload
        does write through to the column there, so ``is_required`` is
        legitimate client-enforceable contract metadata that the
        resolved catalog emits as ``required: true``.

        Runs after structural validation — ``fields`` shape, the
        ``data_type`` natural-key reference and ``name`` presence are
        already guaranteed — and before the classification pass, so
        every later stage (permission checks, the reset guard, the
        apply pass, the reported ``changed_fields``) sees the demoted
        value. The ``isinstance`` guards keep the pass total on
        unexpected input: a non-string ``dt_name`` would break the
        predicate's ``startswith`` and an unhashable ``attr_name`` its
        membership test.
        """
        warnings = []
        for i, obj in enumerate(objects):
            if obj['model'].split('.')[1] != 'attributedef':
                continue
            fields = obj['fields']
            if not fields.get('is_required'):
                continue
            # ``data_type`` serializes as a natural-key list of strings;
            # a stray scalar is tolerated and judged as-is.
            dt_ref = fields.get('data_type')
            dt_name = (
                dt_ref[0]
                if isinstance(dt_ref, list) and dt_ref else dt_ref
            )
            attr_name = fields.get('name')
            if (not isinstance(dt_name, str) or not dt_name
                    or not isinstance(attr_name, str)):
                continue
            if not is_server_managed_attribute(dt_name, attr_name):
                continue
            fields['is_required'] = False
            nk = self.fixture_service.natural_key_for(
                'attributedef', fields,
            )
            warnings.append({
                'code': WARN_IS_REQUIRED_DEMOTED,
                'message': (
                    f'attributedef {list(nk)}: is_required demoted — '
                    f"attribute '{attr_name}' (data_type '{dt_name}') "
                    'is server-managed'
                ),
                'path': f'$.objects[{i}]',
            })
        return warnings

    @staticmethod
    def _to_error_entry(error: str) -> dict:
        """Map a ``object[i]: message`` validation error to the envelope."""
        match = _OBJECT_INDEX_RE.match(error)
        path = f'$.objects[{match.group(1)}]' if match else None
        message = error[match.end():] if match else error
        return {
            'code': ERR_INVALID_CATALOG_OBJECT,
            'message': message,
            'path': path,
        }

    @staticmethod
    def _apply_error_message(exc: Exception, obj: dict, label: str) -> str:
        """Render an apply failure with object/field context.

        ``DeserializationError.WithData`` embeds the offending value in
        a trailing ``field_value was '<value>'`` marker. When that value
        ``str()``-matches exactly one field of the object, the error is
        reported against ``label.field``; several matches name every
        candidate. A ``'None'`` marker is the signature of a ``pk``
        conversion failure (``WithData`` passes ``field_value=None``
        there), so it must not be matched against nullable fields.
        Anything without a single-field attribution falls back to
        naming the object by label and pk — the raw ORM message never
        reaches the client bare.

        ``exc.__context__`` carries the conversion error ``WithData``
        wrapped (it chains implicitly), which is the clean message; the
        wrapper's own text is a serialization-format detail.
        """
        original = str(exc.__context__ or exc)
        match = _FIELD_VALUE_RE.search(str(exc))
        fields = obj.get('fields')
        if (match and match.group(1) != 'None'
                and isinstance(fields, dict)):
            field_value = match.group(1)
            candidates = [
                name for name, value in fields.items()
                if str(value) == field_value
            ]
            if len(candidates) == 1:
                return f'{label}.{candidates[0]}: {original}'
            if candidates:
                names = ', '.join(sorted(candidates))
                return f'{label} ({names}): {original}'
        return f"{label} object (pk={obj.get('pk')}): {exc}"

    @staticmethod
    def _require_perm(user, action: str, label: str) -> None:
        """Enforce the per-model admin permission for a planned write."""
        codename = f'{action}_{label}'
        if not user.has_perm(f'schemas.{codename}'):
            raise PermissionError(
                ERR_MISSING_MODEL_PERMISSION.format(
                    codename=codename, label=label,
                ),
            )

    @staticmethod
    def _change_message(created: Counter, updated: Counter) -> str:
        """Summarize the applied counts for the audit LogEntry."""
        parts = []
        if created:
            parts.append(f'created: {dict(sorted(created.items()))}')
        if updated:
            parts.append(f'updated: {dict(sorted(updated.items()))}')
        return '; '.join(parts) or 'no changes'

    @staticmethod
    def _usage_summary(usage: dict) -> str:
        """Render a ``usage`` dict as short prose for the in-use message.

        ``usage`` carries a single count metric (``nodes`` or
        ``node_attributes``) plus the sorted slugs of the projects
        owning the implementing rows, e.g. ``"2 nodes"`` or
        ``"3 node_attributes across projects 'a', 'b'"`` — enough for
        the caller to see what blocks the update without extra
        queries.
        """
        parts = [
            f"{count} {name.removesuffix('s') if count == 1 else name}"
            for name, count in usage.items()
            if name != 'projects'
        ]
        projects = usage.get('projects') or []
        if projects:
            parts.append(
                'across projects '
                + ', '.join(repr(slug) for slug in projects)
            )
        return ' '.join(parts)

    @staticmethod
    def _error_envelope(errors: list) -> dict:
        """Failure half of the endpoint's response envelope."""
        return {'applied': False, 'errors': errors, 'warnings': []}
