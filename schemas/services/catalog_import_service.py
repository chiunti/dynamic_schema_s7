"""Atomic catalog-grammar import for machine consumers (MCP level 2).

``CatalogImportService`` accepts a fixture-format payload
(``{"objects": [{model, pk?, fields, delete?}]}`` — the ``objects``
list reaches the service), validates it against the catalog model
whitelist, resolves each object to a create, an update or a delete,
and applies everything inside one transaction.

Upsert semantics are loaddata-like: an object whose ``pk`` or natural
key matches a live row **updates** that row's fields; no match
**creates** a row. An object carrying ``"delete": true`` instead
**removes** one catalog row, identified by exactly one arm — a UUID
``pk``, or a ``fields`` dict holding a complete natural key; the XOR
is enforced at structural validation, so carrying both or neither is
a per-object error and a delete can never blur into upsert shape.

Two semantic rules sit between structural validation and
normalization: an ``attributedef`` upsert declaring datatype
``natural_version`` under a non-null ``variant_key`` is rejected per
object — a version carrier is honored only at ``variant_key IS
NULL`` scope, so a variant-scoped declaration could never write
``Node.version`` (post-AI-106 the structural field-map and the root
version sync read IS NULL declarations only) — and so is an upsert
whose ``name`` matches a ``Node`` column
(``key``/``name``/``sort_order``/``version``): post-AI-141 the
field-map resolves every Node-column carrier at ``variant_key IS
NULL`` scope only, so a variant-scoped name-match is equally inert
whatever the def's datatype.

Deletes share the update implementation gate: a catalog row already
instantiated by schema data (``Node``/``NodeAttribute`` rows, in any
project — the grammar is global and status-agnostic) rejects its own
delete, reported as ``catalog_object_in_use`` with a usage breakdown.
There is no no-op exemption — deletes are always gated. A
``nodetype`` delete is widened by its contract rows (D109): the
type's own AttributeDefs, NodeTypeVariants and every
NodeTypeComposition touching it are deleted with it — each dependent
requires its own ``delete_<label>`` permission and passes the usage
gate individually — while foreign variants bound only through
``props_node_type`` are detached by the ORM's SET_NULL: gated, never
deleted, and requiring no extra permission. Dependents are enumerated
before any write: a would-be dependent row created by an upsert of the
same payload is not enumerated, so the type's delete detaches it via
SET_NULL instead of removing it — it keeps its own gate/permission
record rather than dying ungated.

A ``domain`` delete instead carries its ``DomainItem`` rows down by
plain FK cascade — items are the domain's owned content, not a peer
contract: no per-item permission is required, and item implementation
is covered transitively (a stored item value counts as usage of the
domain itself, so the domain gate blocks the whole delete).

An update is a **full-row replace**: deserialization fills every field
absent from ``fields`` with the model default, so omitting a field
whose live value is non-default would silently reset it. Such objects
are rejected pre-write with the field names they would reset — omitted
fields whose live value already IS the default stay harmless. Creates
are never gated, and neither are no-op updates — re-sending the live
values verbatim keeps a re-import idempotent. The usage check is an
advisory read inside the transaction, so under READ COMMITTED a schema
committed between the gate and this import's commit escapes
verification — a residual TOCTOU window accepted for v1.

Apply order is upserts first in ascending ``_APPLY_RANK`` — so
natural-key FKs resolve against rows written earlier in the same
payload — then deletes in descending rank, children before parents
(attributedef before variant/composition before nodetype/domain), so
a rename written as create-new-NK + delete-old-NK applies atomically.
An upsert and a delete naming the same live row through different
identification arms (pk vs natural key) is not a payload conflict:
the upsert applies first and the delete deterministically removes
the row — delete wins.

On success the envelope carries ``results`` — one entry per payload
object, in payload order, with ``index``, ``model``, ``natural_key``,
``action`` (``created``/``updated``/``deleted``) and
``changed_fields`` — so the caller can audit exactly which row each
object touched; ``nodetype`` delete entries also report the
``dependents`` counts removed with the type. ``warnings`` reports the
non-fatal payload normalizations applied pre-write: an
``attributedef`` declaring ``is_required`` on a server-managed attribute
is demoted to ``False``, never rejected. Catalog grammar is global and
takes effect immediately (no draft lifecycle); every import is
attributed to the calling user through an admin ``LogEntry``.

The same implementation check is exposed as a pure read:
``get_object_usage`` backs ``GET /api/catalog/usage/`` — the pre-check
an L2 agent runs before proposing updates/deletes instead of
discovering implementation by rejection. It resolves the addressed row
(pk XOR natural-key params) and relays the same ``{implemented,
usage}`` evidence the gate would report, so both surfaces share one
calculation: ``catalog_object_usage``.
"""

import re
import uuid
from collections import Counter
from typing import TYPE_CHECKING, Optional

from django.core.serializers.base import DeserializationError
from django.db import DatabaseError, IntegrityError, transaction

from ..constants import (
    ERR_CATALOG_DEPENDENT_IN_USE_MSG,
    ERR_CATALOG_OBJECT_IN_USE,
    ERR_CATALOG_OBJECT_IN_USE_MSG,
    ERR_DELETE_TARGET_NOT_FOUND,
    ERR_DUPLICATE_DELETE_TARGET,
    ERR_INVALID_CATALOG_OBJECT,
    ERR_MISSING_MODEL_PERMISSION,
    ERR_MISSING_NATURAL_KEY_FIELDS,
    ERR_PK_MUST_BE_VALID_UUID,
    ERR_UNKNOWN_CATALOG_MODEL_LABEL,
    ERR_UPDATE_RESET_FIELDS,
    ERR_USAGE_IDENTIFIER_CONFLICT,
    ERR_USAGE_IDENTIFIER_REQUIRED,
    ERR_USAGE_MODEL_REQUIRED,
    ERR_USAGE_UNKNOWN_NK_PARAMS,
    ERR_VARIANT_COLUMN_CARRIER_MSG,
    ERR_VARIANT_NODE_FIELD_CARRIER_MSG,
    ERR_VARIANT_VERSION_CARRIER_MSG,
    WARN_IS_REQUIRED_DEMOTED,
)
from ..repositories.catalog_usage_repository import CatalogUsageRepository
from ..repositories.fixture_repository import FixtureRepository
from .datatype_plugins import (
    _AUTHOR_SETTABLE_NODE_FIELDS,
    is_column_mapped_datatype,
    is_server_managed_attribute,
)
from .exceptions import CatalogObjectNotFoundError
from .fixture_service import NATURAL_KEY_FIELDS, FixtureService

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
    def import_objects(
        self, objects: list, user: 'Optional[User]',
        enforce_perms: bool = True,
    ) -> dict:
        """Validate + apply a fixture-format catalog payload atomically.

        Args:
            objects: the ``objects`` list of the request payload; each
                entry is either an upsert ``{model: "schemas.<label>",
                pk?: str, fields: {...}}`` — ``pk`` optional, a
                deterministic uuid5 by natural key is generated when
                absent — or a delete ``{model, pk | fields,
                "delete": true}``, which identifies its target row by
                exactly one arm: a UUID ``pk`` (``fields`` absent) or a
                ``fields`` dict carrying a complete natural key
                (``pk`` absent). Both or neither is a validation error
                — the XOR keeps a delete from blurring into upsert
                shape.
            user: the authenticated user; per-model admin permissions
                (``schemas.add_<label>`` / ``schemas.change_<label>``
                for upserts, ``schemas.delete_<label>`` for deletes —
                plus ``delete_<dep_label>`` for every dependent row a
                ``nodetype`` delete removes) gate each planned write.
                May be ``None`` when ``enforce_perms`` is False.
            enforce_perms: when False the protective gates of the
                direct-import contract are skipped — the per-model
                permission pass, the catalog-usage gate (implemented
                rows accept updates and deletes), and the LogEntry
                attribution. The payload is still validated, classified
                and applied through the same machinery; intended for
                transactional probes that run the real apply inside a
                savepoint and roll it back (the changeset validator's
                post-merge coherence tier, whose own risk classes
                replace the usage gate) and for the governed apply a
                changeset approval authorizes. The public endpoint
                keeps the default True.

        Returns:
            On success ``{"applied": True, "created": {label: n},
            "updated": {label: n}, "deleted": {label: n}, "warnings":
            [{"code", "message", "path"}], "results":
            [{"index", "model", "natural_key", "action",
            "changed_fields"}]}`` — one ``results`` entry per payload
            object, in payload order, with ``action`` one of
            ``created``/``updated``/``deleted``; a ``nodetype`` delete
            entry additionally carries ``dependents: {label: n}`` — the
            real counts of contract rows removed with the type. On any
            validation or apply failure ``{"applied": False,
            "errors": [{"code", "message", "path"}], "warnings": []}``
            with no partial writes (the whole transaction rolls back).
            Errors carry ``path: $.objects[i]`` locating the offending
            entry, and apply-time failures attribute the ORM error to
            the object — and, when determinable, the field — that
            broke, so a raw conversion message never reaches the client
            bare.

            A semantic gate runs between structural validation and
            normalization: an ``attributedef`` upsert declaring
            datatype ``natural_version`` under a non-null
            ``variant_key`` is rejected with
            ``invalid_catalog_object``. Post-AI-106 a version carrier
            is honored only at ``variant_key IS NULL`` scope — the
            structural field-map skips variant-scoped defs and the
            root version sync resolves the carrier from IS NULL
            declarations only — so the def could never write
            ``Node.version`` and admitting it would persist an inert
            declaration.

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
            catalog verbatim stays possible once schemas exist.

            Deletes are gated the same way, with no no-op exemption:
            every delete targets a live row resolved at classification
            (a delete resolving to nothing is a validation error, and
            two deletes resolving to the same row conflict), and an
            implemented row refuses to die. A ``nodetype`` delete
            additionally gates every enumerated dependent — its own
            AttributeDefs, variants and compositions (deleted with it,
            D109) and the foreign variants bound through
            ``props_node_type`` (detached via SET_NULL) — individually;
            a blocking dependent is reported under the parent object's
            path with the dependent row named. The usage check is an
            advisory read under READ COMMITTED: a schema committed
            between the gate and this import's commit escapes it, a
            residual TOCTOU window accepted for v1.

            Apply order (D108): upserts first in ascending
            ``_APPLY_RANK`` — natural-key FKs resolve against rows
            written earlier in the same payload — then deletes in
            descending rank, children before parents, so a rename
            written as create-new-NK + delete-old-NK applies
            atomically. A ``nodetype`` delete removes its own contract
            rows (defs, variants, compositions in either position) in
            reverse-rank order before the type itself. A delete whose
            target — or a dependent — vanished between the gate and
            its write counts nothing: the payload's intent, the row
            gone, is already satisfied.

            Classification and apply run in one transaction under READ
            COMMITTED, so a row matched as an update may still be
            deleted by a concurrent transaction before its write — the
            save then inserts it. Existence is re-checked per update
            object right before the save — and the reclassified insert
            must then pass the ``add_<label>`` check the permission
            pass gated only as ``change_<label>`` — so the envelope
            reports the action actually taken.

            The payload is mutated in place: generated uuid5 pks are
            written into upsert objects that omit ``pk``, objects whose
            natural key already exists get ``pk`` rewritten to the live
            row's pk so deserialization updates it, delete objects get
            ``pk`` set to their resolved target for reporting, and
            unsatisfiable ``is_required`` flags (see ``warnings``) are
            demoted to ``False``.

        Raises:
            PermissionError: (``enforce_perms`` only) the user lacks a
                required per-model permission; the message names
                codename and model. Never partial — a mid-apply raise
                (a reclassified insert missing ``add``) rolls back the
                writes before it with the whole transaction.
        """
        # PK generation runs before validation: validate_objects requires
        # a UUID pk on every entry, so fulfilling the 'pk optional'
        # contract must come first. Malformed entries are skipped here
        # and reported by validation with a precise path.
        self._assign_missing_pks(objects)

        errors = self.fixture_service.validate_objects(
            objects, allowed_models=CATALOG_MODELS, allow_delete=True,
        )
        if errors:
            return self._error_envelope(
                [self._to_error_entry(e) for e in errors],
            )

        # Variant-scoped column-carrier gate — fail-fast before the
        # demote pass mutates anything: a rejected payload must be
        # reported as declared, not normalized first. Post-AI-106 a
        # ``natural_version`` attributedef only acts as the version
        # carrier at ``variant_key IS NULL`` scope — the structural
        # field-map skips variant-scoped defs and the root version
        # sync resolves the carrier from IS NULL declarations only —
        # so a def scoped to a variant can never write
        # ``Node.version``. Post-AI-141 the field-map applies the same
        # IS NULL rule to every def whose ``name`` matches a Node
        # column (``key``/``name``/``sort_order``/``version``)
        # regardless of datatype. Rejecting both declaration shapes
        # here seals the catalog instead of persisting a carrier that
        # silently never fires.
        column_carrier_errors = (
            self._reject_variant_scoped_node_column_carriers(objects)
        )
        if column_carrier_errors:
            return self._error_envelope(column_carrier_errors)

        # Server-managed required demotion — before classification so
        # the permission pass, the reset guard, the apply pass and the
        # reported ``changed_fields`` all operate on the flag actually
        # persisted. The pass mutates ``fields`` in place and returns
        # the warnings the success envelope reports.
        warnings = self._demote_server_managed_required(objects)

        # Existence + pk-resolution pass. A pk pointing at one row while
        # the natural key resolves to another is a payload conflict —
        # reported per object instead of surfacing as a raw constraint
        # error during apply. The conflict is an upsert-only concern: a
        # delete object carries exactly one identifier arm (the
        # validation XOR guarantees it), so it can never disagree with
        # itself — it either resolves to a live row or it does not, and
        # an unresolved delete is its own per-object error.
        conflicts = []
        classified = []
        snapshots = {}
        delete_targets = set()
        for i, obj in enumerate(objects):
            label = obj['model'].split('.')[1]
            fields = obj.get('fields')
            pk = obj.get('pk')

            if obj.get('delete') is True:
                if pk is not None:
                    # pk arm — the row must exist. Canonicalize like the
                    # upsert path: non-canonical UUID spellings match
                    # case-insensitively in Postgres.
                    resolved = (
                        str(uuid.UUID(str(pk)))
                        if self.fixture_repository.pk_exists(label, pk)
                        else None
                    )
                else:
                    # NK arm — validation already guaranteed a complete
                    # natural key in ``fields``.
                    nk = self.fixture_service.natural_key_for(
                        label, fields,
                    )
                    resolved = (
                        self.fixture_repository.get_pk_by_natural_key(
                            label, list(nk),
                        )
                        if nk is not None else None
                    )
                if resolved is None:
                    conflicts.append({
                        'code': ERR_INVALID_CATALOG_OBJECT,
                        'message': ERR_DELETE_TARGET_NOT_FOUND,
                        'path': f'$.objects[{i}]',
                    })
                    continue
                # A pk-arm and an NK-arm delete — or two NK arms
                # carrying NULL components — can escape the
                # payload-level dedup and still resolve to the same
                # row; the second one conflicts.
                if (label, resolved) in delete_targets:
                    conflicts.append({
                        'code': ERR_INVALID_CATALOG_OBJECT,
                        'message': ERR_DUPLICATE_DELETE_TARGET,
                        'path': f'$.objects[{i}]',
                    })
                    continue
                delete_targets.add((label, resolved))
                # Snapshot the live row now — it is the only source of
                # natural_key (pk arm) and changed_fields (both arms)
                # in the results entry, since the row is gone by
                # report time.
                snapshots[i] = self.fixture_repository.get_fixture_state(
                    label, resolved,
                )
                classified.append((i, obj, label, resolved))
                continue

            nk = self.fixture_service.natural_key_for(label, fields)
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
        dependents = {}
        for i, obj, label, existing_pk in classified:
            if obj.get('delete') is True:
                if enforce_perms:
                    self._require_perm(user, 'delete', label)
                # Point the object at the resolved row — a delete never
                # reaches deserialization, so the rewrite is reporting
                # metadata only (results path, apply-error context).
                obj['pk'] = existing_pk
                if label == 'nodetype':
                    # D109: the type's own contract rows (its defs, its
                    # variants, every composition touching it) are
                    # deleted with it — each demands its own
                    # delete_<dep_label> permission. 'detach' rows are
                    # SET_NULL side-effects of the delete itself and
                    # require no extra permission.
                    deps = self.fixture_repository.node_type_dependents(
                        existing_pk,
                    )
                    dependents[i] = deps
                    for dep_label, dep_pks in deps['delete'].items():
                        if dep_pks and enforce_perms:
                            self._require_perm(
                                user, 'delete', dep_label,
                            )
                continue
            if existing_pk is not None:
                if enforce_perms:
                    self._require_perm(user, 'change', label)
                # Point the object at the live row so deserialization
                # updates it instead of colliding on the natural key.
                obj['pk'] = existing_pk
                updated[label] += 1
            else:
                if enforce_perms:
                    self._require_perm(user, 'add', label)
                created[label] += 1

        # Full-row-replace guard — still before any write: an update
        # that omits a field whose live value differs from the model
        # default would silently reset it (deserialization fills absent
        # fields with defaults). Reject such objects naming the fields
        # they would reset; omitted fields whose live value already IS
        # the default are harmless and pass. The live snapshots double
        # as the ``changed_fields`` baseline of the success results —
        # the dict already carries the delete snapshots taken at
        # classification.
        defaults_by_label = {}
        reset_errors = []
        for i, obj, label, existing_pk in classified:
            if existing_pk is None or obj.get('delete') is True:
                # Creates have no baseline to reset against; deletes
                # replace nothing — the silent-reset hazard is an
                # update-only concern.
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
        # over the snapshots the reset guard just built. Deletes are
        # always gated — there is no no-op delete — and a nodetype
        # delete additionally gates every enumerated dependent row,
        # both the ones deleted with it and the ones SET_NULL-detached,
        # individually. The check is an advisory read inside this
        # transaction: under READ COMMITTED a schema committed between
        # this pass and the outer commit escapes verification — a
        # residual TOCTOU window accepted for v1.
        #
        # Skipped under ``enforce_perms=False``: the gate is
        # direct-import governance — an anonymous payload may never
        # mutate an implemented row. The changeset validator runs this
        # apply inside a savepoint under its own risk classes (tier-3
        # classifies the impact as breaking/warning/info instead), and
        # the governed apply the changeset approves needs the same
        # bypass — otherwise no changeset could ever mutate an
        # implemented row.
        usage_errors = []
        for i, obj, label, existing_pk in (
            classified if enforce_perms else ()
        ):
            if obj.get('delete') is True:
                usage = self.catalog_object_usage(
                    label, existing_pk,
                )
                if usage['implemented']:
                    nk = self._delete_result_nk(
                        label, obj, snapshots.get(i),
                    )
                    usage_errors.append({
                        'code': ERR_CATALOG_OBJECT_IN_USE,
                        'message': ERR_CATALOG_OBJECT_IN_USE_MSG.format(
                            label=label,
                            natural_key=list(nk) if nk else [],
                            usage=self._usage_summary(usage['usage']),
                            action='deletes',
                        ),
                        'path': f'$.objects[{i}]',
                        'usage': usage['usage'],
                    })
                    continue
                if label == 'nodetype':
                    # Every dependent row gates individually (D109): a
                    # def referenced by another type's NodeAttribute or
                    # a composition edge instantiated elsewhere blocks
                    # the whole delete. The error names the dependent
                    # row and points at the parent object's path.
                    parent_nk = self._delete_result_nk(
                        label, obj, snapshots.get(i),
                    )
                    for bucket in ('delete', 'detach'):
                        for dep_label, dep_pks in (
                            dependents[i].get(bucket, {}).items()
                        ):
                            for dep_pk in dep_pks:
                                self._gate_dependent(
                                    usage_errors, i,
                                    dep_label, dep_pk,
                                    label, parent_nk,
                                )
                continue
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
            usage = self.catalog_object_usage(label, existing_pk)
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
                        action='updates',
                    ),
                    'path': f'$.objects[{i}]',
                    # Extra key beyond code/message/path: the counts
                    # and owning project slugs the caller needs to
                    # understand what blocks the update (D106).
                    'usage': usage['usage'],
                })
        if usage_errors:
            return self._error_envelope(usage_errors)

        # Apply one object at a time: upserts first in ascending
        # dependency rank (the sort is stable, so same-rank objects keep
        # payload order), then deletes in DESCENDING rank — children
        # (attributedef) before the parents (nodetype, domain) that own
        # them, so no delete relies on cascade ordering and a rename
        # written as create-new-NK + delete-old-NK applies atomically
        # in one payload (D108). A failure is mapped back to the
        # payload index of the object that broke — one batch call would
        # lose that attribution.
        upsert_entries = [
            entry for entry in classified
            if entry[1].get('delete') is not True
        ]
        delete_entries = [
            entry for entry in classified
            if entry[1].get('delete') is True
        ]
        ordered = sorted(
            upsert_entries,
            key=lambda entry: _APPLY_RANK[entry[2]],
        )
        ordered_deletes = sorted(
            delete_entries,
            key=lambda entry: _APPLY_RANK[entry[2]],
            reverse=True,
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
                    if enforce_perms:
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

        # Deletes last, in descending rank (D108). ``deleted`` counts
        # the rows the ORM cascade collector actually reports — a
        # target already gone (vanished under READ COMMITTED, or a
        # dependent shared with another delete of this payload)
        # returns {} and counts nothing.
        deleted: Counter = Counter()
        dep_deleted: dict[int, Counter] = {}
        for orig_i, obj, label, resolved_pk in ordered_deletes:
            try:
                ctx_obj, ctx_label = obj, label
                if label == 'nodetype':
                    # D109: the type's contract rows die before it —
                    # its defs, its variants and every composition
                    # touching it, in reverse-rank order. Each was
                    # permission-checked and usage-gated individually
                    # in the earlier passes.
                    dep_rows = [
                        (dep_label, dep_pk)
                        for dep_label, dep_pks in
                        dependents.get(orig_i, {}).get(
                            'delete', {},
                        ).items()
                        for dep_pk in dep_pks
                    ]
                    dep_rows.sort(
                        key=lambda entry: _APPLY_RANK[entry[0]],
                        reverse=True,
                    )
                    for dep_label, dep_pk in dep_rows:
                        ctx_obj, ctx_label = {'pk': dep_pk}, dep_label
                        counts = (
                            self.fixture_repository
                            .delete_fixture_object(dep_label, dep_pk)
                        )
                        self._count_deleted(deleted, counts)
                        dep_counts = dep_deleted.setdefault(
                            orig_i, Counter(),
                        )
                        self._count_deleted(dep_counts, counts)
                    ctx_obj, ctx_label = obj, label
                counts = self.fixture_repository.delete_fixture_object(
                    label, resolved_pk,
                )
                self._count_deleted(deleted, counts)
            except (DeserializationError, ValueError, RuntimeError,
                    IntegrityError, DatabaseError) as exc:
                # Same rollback contract as upserts: a mid-delete
                # failure reverts the whole apply.
                transaction.set_rollback(True)
                return self._error_envelope([
                    {
                        'code': ERR_INVALID_CATALOG_OBJECT,
                        'message': self._apply_error_message(
                            exc, ctx_obj, ctx_label,
                        ),
                        'path': f'$.objects[{orig_i}]',
                    },
                ])

        if enforce_perms:
            # The trial apply of the changeset validator shares this
            # machinery but writes no audit record — its writes roll
            # back inside the savepoint and the proposer may be a
            # credential with no User to attribute.
            self.fixture_repository.record_catalog_import(
                user, self._change_message(created, updated, deleted),
            )

        # Per-object audit trail in payload order — ``classified`` kept
        # the original indices and the pre-apply snapshots pinpoint
        # which incoming fields actually differ from the live row (or,
        # for deletes, which fields the removed row held).
        results = []
        for i, obj, label, existing_pk in classified:
            if obj.get('delete') is True:
                live = snapshots.get(i)
                entry = {
                    'index': i,
                    'model': label,
                    'natural_key': list(
                        self._delete_result_nk(label, obj, live) or ()
                    ),
                    'action': 'deleted',
                    # Every live field was removed — the snapshot keys
                    # are the complete change set.
                    'changed_fields': sorted(live) if live else [],
                }
                if label == 'nodetype':
                    entry['dependents'] = dict(
                        sorted(dep_deleted.get(i, {}).items())
                    )
                results.append(entry)
                continue
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
            'deleted': dict(sorted(deleted.items())),
            'warnings': warnings,
            'results': results,
        }

    def catalog_object_usage(self, label: str, pk: 'str | uuid.UUID') -> dict:
        """Shared 'implemented by existing schemas' calculation.

        Thin delegation to ``usage_repository.usage_for`` — same
        contract: a well-formed pk of a vanished row returns zeroed
        usage (the gate's reclassify-as-insert path relies on it).
        Called by the update/delete gates and by the usage pre-check
        endpoint.
        """
        return self.usage_repository.usage_for(label, pk)

    def get_object_usage(
        self,
        model: Optional[str],
        pk: Optional[str],
        nk_params: dict,
    ) -> dict:
        """Usage pre-check for one catalog row (GET /api/catalog/usage/).

        ``model`` is ``schemas.<label>`` — the label must be in
        ``CATALOG_MODELS``. The object is identified by exactly one
        arm: ``pk`` (uuid) or ``nk_params`` (the natural-key
        components as flat scalar query params — omit a component,
        or send an empty string, to address a NULL position like
        ``variant_key``/``collection_key``/``discriminator_attr``/
        unbound ``node_type``). Raises ``ValueError`` for the 400
        family and ``CatalogObjectNotFoundError`` when the
        identifier resolves to no live row. Returns the
        ``{implemented, usage}`` dict of ``catalog_object_usage``.
        """
        if not model or not isinstance(model, str):
            raise ValueError(ERR_USAGE_MODEL_REQUIRED)
        # Exactly one dot, like validate_objects — 'schemas.x.y' or
        # 'schemas' can never smuggle a label in position [1].
        if (not model.startswith('schemas.') or model.count('.') != 1
                or model.split('.')[1] not in CATALOG_MODELS):
            raise ValueError(
                ERR_UNKNOWN_CATALOG_MODEL_LABEL.format(label=model),
            )
        label = model.split('.')[1]

        # Params beyond the model's natural-key fields are rejected
        # before the pk/NK exclusivity check: a stray key usually means
        # a misspelled component or a foreign param (e.g. ``project_id``
        # bouncing in from the schema reads) — name it instead of
        # mislabeling the call a pk-vs-NK "conflict".
        expected = NATURAL_KEY_FIELDS[label]
        unknown = set(nk_params) - set(expected)
        if unknown:
            raise ValueError(
                ERR_USAGE_UNKNOWN_NK_PARAMS.format(
                    params=', '.join(sorted(unknown)),
                    label=label,
                    fields=', '.join(expected),
                )
            )

        has_pk = pk is not None
        has_nk = bool(nk_params)
        if has_pk and has_nk:
            raise ValueError(ERR_USAGE_IDENTIFIER_CONFLICT)
        if not has_pk and not has_nk:
            raise ValueError(ERR_USAGE_IDENTIFIER_REQUIRED)

        if has_pk:
            # Canonicalize like the classification pass: non-canonical
            # UUID spellings match case-insensitively in Postgres.
            try:
                canonical_pk = str(uuid.UUID(str(pk)))
            except (ValueError, AttributeError, TypeError) as e:
                raise ValueError(
                    ERR_PK_MUST_BE_VALID_UUID.format(pk=pk),
                ) from e
            if not self.fixture_repository.pk_exists(
                label, canonical_pk,
            ):
                raise CatalogObjectNotFoundError(model, {'pk': pk})
            return self.catalog_object_usage(label, canonical_pk)

        # NK arm — the unknown-params check ran above the pk/NK
        # exclusivity gate, so ``nk_params`` is already vocabulary-clean
        # here: silently dropping a stray key would query a different
        # (incomplete) identity than the caller meant.
        # NULL positions are addressed by omitting the param or by
        # sending it empty — both map to None. natural_key_for reads
        # the same fixture-format dict its import-pass calls build:
        # FK positions accept the scalar the query param carries
        # (``nk1`` unwraps lists but passes scalars through).
        fields = {
            name: (nk_params[name] or None)
            for name in expected
            if name in nk_params
        }
        # A non-nullable position must carry a real value; a nullable
        # one legitimately resolves to NULL (base attributedefs carry
        # ``variant_key`` NULL, global defs ``node_type`` NULL). An
        # unresolvable nullability map reads as "nothing nullable".
        nullable = (
            self.fixture_repository.get_nullable_field_names(label)
            or frozenset()
        )
        missing = [
            name
            for name in expected
            if name not in nullable and fields.get(name) is None
        ]
        if missing:
            raise ValueError(
                ERR_MISSING_NATURAL_KEY_FIELDS.format(
                    fields=', '.join(missing),
                )
            )
        # ``nk`` is never None: every CATALOG_MODELS label declares a
        # natural key in NATURAL_KEY_FIELDS.
        nk = self.fixture_service.natural_key_for(label, fields)
        resolved = self.fixture_repository.get_pk_by_natural_key(
            label, list(nk),
        )
        if resolved is None:
            raise CatalogObjectNotFoundError(
                model, {'natural_key': nk_params},
            )
        return self.catalog_object_usage(label, resolved)

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
            if 'delete' in obj:
                # A delete identifies its target by 'pk' or by the
                # natural key in 'fields' — an injected uuid5 would
                # break the XOR arm check validation performs.
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
        already guaranteed on upserts — and before the classification
        pass, so every later stage (permission checks, the reset guard,
        the apply pass, the reported ``changed_fields``) sees the
        demoted value. Delete objects are skipped entirely: their
        ``fields`` exist only to resolve identity and nothing is
        persisted, so demotion does not apply. The ``isinstance``
        guards keep the pass total on unexpected input: a non-string
        ``dt_name`` would break the predicate's ``startswith`` and an
        unhashable ``attr_name`` its membership test.
        """
        warnings = []
        for i, obj in enumerate(objects):
            if obj['model'].split('.')[1] != 'attributedef':
                continue
            if obj.get('delete') is True:
                continue
            fields = obj.get('fields')
            if not isinstance(fields, dict):
                continue
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

    def _reject_variant_scoped_node_column_carriers(
        self, objects: list,
    ) -> list[dict]:
        """Reject attributedef upserts carrying a Node column under a variant.

        A Node-column carrier is honored only at ``variant_key IS
        NULL`` scope, so two declaration shapes are inert under a
        variant and rejected per object:

        - a def bound to a column-mapped datatype (``display_order``
          or any ``natural_*``): post-AI-106 the structural field-map
          (``SchemaService._build_json_key_to_field_map``) skips
          variant-scoped defs and the root version sync resolves the
          carrier from IS NULL declarations only, so a column-mapped
          def carrying a ``variant_key`` can never write the column —
          ``natural_version`` (AI-117) is the original case; post-AI-146
          the editor write-path skips the same defs, making the whole
          set inert under a variant (AI-154). ``natural_version`` keeps
          its specific literal; the rest report the generic
          column-carrier message naming the datatype.
        - any def whose ``name`` matches a Node column
          (``key``/``name``/``sort_order``/``version``): post-AI-141
          the same field-map resolves name-matched carriers at IS
          NULL scope only, so a variant-scoped name-match can never
          write the column whatever the def's datatype — a ``string``
          def named ``'version'`` diverges exactly like the sealed
          ``natural_version`` carrier.

        Admitting either shape would persist an inert declaration and
        let the catalog diverge silently, so the import declares the
        pattern invalid instead — a column carrier must be a
        non-variant declaration. An upsert matching both shapes
        reports the datatype diagnosis: it is the more specific of the
        two rules it violates (``natural_version`` names the version
        carrier, any other column-mapped datatype names itself).

        Runs after structural validation — the ``data_type`` FK
        shape and ``variant_key``'s presence as a ``str``-or-``None``
        NK position are already guaranteed — and before the demote
        pass so a rejected payload is reported as declared, never
        normalized first. Delete objects are skipped: their
        ``fields`` exist only to resolve identity and nothing is
        persisted. The ``isinstance`` guards keep the pass total on
        unexpected input; a stray scalar ``data_type`` is tolerated
        and judged as-is, mirroring the demote pass.

        Returns one ``ERR_INVALID_CATALOG_OBJECT`` entry per
        offending upsert (``path: $.objects[i]``), empty when the
        payload is clean.
        """
        errors = []
        for i, obj in enumerate(objects):
            if obj['model'].split('.')[1] != 'attributedef':
                continue
            if obj.get('delete') is True:
                continue
            fields = obj.get('fields')
            if not isinstance(fields, dict):
                continue
            variant_key = fields.get('variant_key')
            if variant_key is None:
                continue
            # ``data_type`` serializes as a natural-key list of
            # strings; a stray scalar is tolerated and judged as-is.
            dt_ref = fields.get('data_type')
            dt_name = (
                dt_ref[0]
                if isinstance(dt_ref, list) and dt_ref else dt_ref
            )
            attr_name = fields.get('name')
            is_column_carrier = (
                isinstance(dt_name, str)
                and is_column_mapped_datatype(dt_name)
            )
            is_node_field_carrier = (
                isinstance(attr_name, str)
                and attr_name in _AUTHOR_SETTABLE_NODE_FIELDS
            )
            if not is_column_carrier and not is_node_field_carrier:
                continue
            nk = self.fixture_service.natural_key_for(
                'attributedef', fields,
            )
            if is_column_carrier:
                if dt_name == 'natural_version':
                    message = ERR_VARIANT_VERSION_CARRIER_MSG.format(
                        natural_key=list(nk),
                        variant_key=variant_key,
                    )
                else:
                    message = ERR_VARIANT_COLUMN_CARRIER_MSG.format(
                        natural_key=list(nk),
                        data_type=dt_name,
                        variant_key=variant_key,
                    )
            else:
                message = ERR_VARIANT_NODE_FIELD_CARRIER_MSG.format(
                    natural_key=list(nk),
                    name=attr_name,
                    variant_key=variant_key,
                )
            errors.append({
                'code': ERR_INVALID_CATALOG_OBJECT,
                'message': message,
                'path': f'$.objects[{i}]',
            })
        return errors

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
    def _change_message(
        created: Counter, updated: Counter, deleted: Counter,
    ) -> str:
        """Summarize the applied counts for the audit LogEntry."""
        parts = []
        if created:
            parts.append(f'created: {dict(sorted(created.items()))}')
        if updated:
            parts.append(f'updated: {dict(sorted(updated.items()))}')
        if deleted:
            parts.append(f'deleted: {dict(sorted(deleted.items()))}')
        return '; '.join(parts) or 'no changes'

    def _gate_dependent(
        self, usage_errors: list, i: int,
        dep_label: str, dep_pk: str,
        label: str, parent_nk,
    ) -> None:
        """Usage-gate one dependent row of a ``nodetype`` delete.

        A dependent already instantiated by schema data blocks the
        whole delete — the error carries ``catalog_object_in_use``,
        names the dependent row (its natural key when it still
        resolves, else its pk) plus the parent delete's identity, and
        points at the parent object's payload path.
        """
        dep_usage = self.catalog_object_usage(dep_label, dep_pk)
        if not dep_usage['implemented']:
            return
        dep_live = self.fixture_repository.get_fixture_state(
            dep_label, dep_pk,
        )
        dep_nk = (
            self.fixture_service.natural_key_for(dep_label, dep_live)
            if dep_live else None
        )
        usage_errors.append({
            'code': ERR_CATALOG_OBJECT_IN_USE,
            'message': ERR_CATALOG_DEPENDENT_IN_USE_MSG.format(
                dep_label=dep_label,
                dep_ref=list(dep_nk) if dep_nk else dep_pk,
                label=label,
                natural_key=list(parent_nk) if parent_nk else [],
                usage=self._usage_summary(dep_usage['usage']),
            ),
            'path': f'$.objects[{i}]',
            # Extra key beyond code/message/path: the counts and
            # owning project slugs blocking the dependent row (D106).
            'usage': dep_usage['usage'],
        })

    def _delete_result_nk(
        self, label: str, obj: dict, live: 'dict | None',
    ) -> 'tuple | None':
        """Natural key reported for a delete object.

        NK-arm deletes derive it from their own identifying ``fields``;
        pk-arm deletes from the live snapshot captured at
        classification — the row is gone by report time.
        """
        fields = obj.get('fields')
        if isinstance(fields, dict):
            return self.fixture_service.natural_key_for(label, fields)
        if live:
            return self.fixture_service.natural_key_for(label, live)
        return None

    @staticmethod
    def _count_deleted(counter: Counter, counts: dict) -> None:
        """Accumulate ORM delete counts into a deleted counter.

        Keys are normalized to bare catalog labels so the envelope and
        results report ``attributedef`` regardless of the repository
        returning bare labels or ``schemas.AttributeDef`` model labels;
        a falsy ``counts`` (a vanished row) adds nothing.
        """
        for key, n in (counts or {}).items():
            counter[key.split('.')[-1].lower()] += n

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
