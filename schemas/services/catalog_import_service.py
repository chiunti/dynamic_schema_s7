"""Atomic catalog-grammar import for machine consumers (MCP level 2).

``CatalogImportService`` accepts a fixture-format payload
(``{"objects": [{model, pk?, fields}]}`` — the ``objects`` list reaches
the service), validates it against the catalog model whitelist, resolves
each object to a create or an update, and applies everything inside one
transaction.

Semantics are loaddata-like: an object whose ``pk`` or natural key
matches a live row **updates** that row's fields; no match **creates** a
row. Deletes are NOT supported — a payload never removes catalog rows;
pruning stays a manual admin operation. Catalog grammar is global and
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
    ERR_INVALID_CATALOG_OBJECT,
    ERR_MISSING_MODEL_PERMISSION,
)
from ..repositories.fixture_repository import FixtureRepository
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
            "updated": {label: n}, "warnings": []}``; on any validation
            or apply failure ``{"applied": False, "errors": [{"code",
            "message", "path"}], "warnings": []}`` with no partial
            writes (the whole transaction rolls back). Errors carry
            ``path: $.objects[i]`` locating the offending entry, and
            apply-time failures attribute the ORM error to the object —
            and, when determinable, the field — that broke, so a raw
            conversion message never reaches the client bare.

            The payload is mutated in place: generated uuid5 pks are
            written into objects that omit ``pk``, and objects whose
            natural key already exists get ``pk`` rewritten to the live
            row's pk so deserialization updates it.

        Raises:
            PermissionError: the user lacks a required per-model
                permission; the message names codename and model. Raised
                before any write — never partial.
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

        # Apply one object at a time in dependency order (the sort is
        # stable, so same-rank objects keep payload order): a failure is
        # mapped back to the payload index of the object that broke —
        # one batch deserialize call would lose that attribution.
        ordered = sorted(
            classified,
            key=lambda entry: _APPLY_RANK[entry[2]],
        )
        for orig_i, obj, label, _ in ordered:
            try:
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
        return {
            'applied': True,
            'created': dict(sorted(created.items())),
            'updated': dict(sorted(updated.items())),
            'warnings': [],
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
    def _error_envelope(errors: list) -> dict:
        """Failure half of the endpoint's response envelope."""
        return {'applied': False, 'errors': errors, 'warnings': []}
