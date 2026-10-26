"""Persistence helpers for fixture load-status inspection and fixture apply."""

import json
from typing import Optional

from django.apps import apps
from django.contrib.admin.models import CHANGE
from django.core import serializers
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import (
    DatabaseError, IntegrityError, models, transaction,
)

from ..models import FixtureLoad


class FixtureRepository:
    """Counts which fixture PKs already exist in the database."""

    def count_existing(self, model_label: str, pks: list[str]) -> int:
        model = apps.get_model('schemas', model_label)
        return model.objects.filter(id__in=pks).count()

    def pk_exists(self, model_label: str, pk: str) -> bool:
        """Whether a row with this primary key exists for the model label."""
        model = apps.get_model('schemas', model_label)
        return model.objects.filter(pk=pk).exists()

    def get_model_field_names(
        self, model_label: str,
    ) -> Optional[frozenset[str]]:
        """Concrete field names of the model, or None if unresolvable."""
        try:
            model = apps.get_model('schemas', model_label)
            return frozenset(f.name for f in model._meta.fields)
        except (LookupError, AttributeError):
            return None

    def get_nullable_field_names(
        self, model_label: str,
    ) -> Optional[frozenset[str]]:
        """Concrete field names declared ``null=True``, or None if
        unresolvable.

        Natural-key positions may legitimately be NULL (e.g.
        ``AttributeDef.variant_key`` marks a base def), so validation
        needs the model's nullability map rather than treating every
        absent value as an error.
        """
        try:
            model = apps.get_model('schemas', model_label)
            return frozenset(
                f.name for f in model._meta.fields if f.null
            )
        except (LookupError, AttributeError):
            return None

    def get_required_field_names(
        self, model_label: str,
    ) -> Optional[frozenset[str]]:
        """Concrete field names a fixture author must supply, or None if
        unresolvable.

        "Required" mirrors the model definition: concrete, non-PK,
        ``null=False`` and ``has_default()==False`` — so CharFields that
        would silently deserialize to ``''`` and required FKs are both
        captured. Auto-populated fields are excluded: ``auto_now``/
        ``auto_now_add`` timestamps (e.g. ``Organization.updated_at``)
        have ``has_default()==False`` yet are filled by the field's
        ``pre_save`` hook at write time. Validation needs the
        required-for-the-author set, not the database NOT NULL set.
        """
        try:
            model = apps.get_model('schemas', model_label)
            return frozenset(
                f.name for f in model._meta.fields
                if not f.primary_key
                and not f.null
                and not f.has_default()
                and not getattr(f, 'auto_now', False)
                and not getattr(f, 'auto_now_add', False)
            )
        except (LookupError, AttributeError):
            return None

    def natural_key_exists(self, model_label: str, ref: list) -> bool:
        """Whether a natural-key reference resolves to a live row.

        The model is resolved dynamically by label — the fixture loader
        validates references against arbitrary catalog models. A missing
        row or an unresolvable lookup (unknown label, manager without the
        method, malformed ref) reads as "does not resolve". Genuine data
        corruption — multiple rows matching a key, database failures —
        propagates: it must be loud, not mistaken for a miss.
        """
        try:
            model = apps.get_model('schemas', model_label)
            model.objects.get_by_natural_key(*ref)
            return True
        except (ObjectDoesNotExist, LookupError, AttributeError,
                TypeError, ValueError):
            return False

    def get_pk_by_natural_key(
        self, model_label: str, ref: list,
    ) -> Optional[str]:
        """Resolve a natural-key reference to the live row's pk, or None.

        Same lookup as ``natural_key_exists`` — same contract too:
        missing rows and unresolvable lookups return None while real
        corruption (MultipleObjectsReturned, DatabaseError) propagates.
        """
        try:
            model = apps.get_model('schemas', model_label)
            return str(model.objects.get_by_natural_key(*ref).pk)
        except (ObjectDoesNotExist, LookupError, AttributeError,
                TypeError, ValueError):
            return None

    def get_fixture_state(
        self, model_label: str, pk: str,
    ) -> Optional[dict]:
        """Fixture-format ``fields`` snapshot of a live row, or None.

        The row is serialized with the JSON fixture serializer
        (``use_natural_foreign_keys=True``), so the returned dict has
        the same shape as an incoming payload's ``fields`` — FK values
        carry natural-key lists and ``None`` is the no-reference shape.
        Returns None when the model label does not resolve or the row
        does not exist; the caller treats that as "no baseline".
        """
        try:
            model = apps.get_model('schemas', model_label)
            row = model.objects.get(pk=pk)
        except (ObjectDoesNotExist, LookupError, AttributeError,
                TypeError, ValueError, ValidationError):
            return None
        return self._fixture_fields(row)

    def get_default_fixture_fields(
        self, model_label: str,
    ) -> Optional[dict]:
        """Fixture-format ``fields`` of a fresh, unsaved instance.

        Serializing ``model()`` yields the all-defaults state — what a
        full-row replace writes into every field the payload omits. The
        import service diffs live rows against this baseline to detect
        silent resets. None when the model label does not resolve.
        """
        try:
            model = apps.get_model('schemas', model_label)
            instance = model()
        except (LookupError, AttributeError, TypeError):
            return None
        return self._fixture_fields(instance)

    @staticmethod
    def _fixture_fields(instance: models.Model) -> dict:
        """``fields`` dict of one instance in JSON fixture format."""
        data = json.loads(serializers.serialize(
            'json', [instance], use_natural_foreign_keys=True,
        ))
        return data[0]['fields']

    def apply_fixture_objects(self, objects: list[dict]) -> None:
        """Persist serialized fixture objects with loaddata upsert semantics.

        Each ``{model, pk, fields}`` object is deserialized and saved:
        an existing pk updates the row, a new pk inserts it. Natural-key
        FK values resolve against the live database — including rows
        written by earlier objects of the same payload, so the caller
        must order objects by dependency.

        Raises:
            ValueError: integrity constraint violations (contract).
            RuntimeError: infrastructure failures.
            serializers.base.DeserializationError: malformed objects or
                unresolvable natural-key references.
        """
        try:
            for deserialized in serializers.deserialize(
                'json', json.dumps(objects),
            ):
                deserialized.save()
        except IntegrityError as e:
            raise ValueError(
                f'Catalog objects violate integrity constraints: {e}',
            ) from e
        except DatabaseError as e:
            raise RuntimeError(
                f'Database error while applying catalog objects: {e}',
            ) from e

    def record_catalog_import(self, user, change_message: str) -> None:
        """Attribute a catalog import to ``user`` via an admin LogEntry.

        Best-effort audit inside its own savepoint: any failure is
        swallowed so bookkeeping can never break or poison the import
        transaction. Users without a pk are skipped silently.
        """
        user_id = getattr(user, 'pk', None)
        if user_id is None:
            return
        try:
            log_entry = apps.get_model('admin', 'LogEntry')
            content_type = apps.get_model('contenttypes', 'ContentType')
            # Grammar rows are the audit subject; NodeType anchors the
            # entry's content type (it is not tied to one object).
            node_type = apps.get_model('schemas', 'NodeType')
            with transaction.atomic():
                log_entry.objects.create(
                    user_id=user_id,
                    content_type_id=content_type.objects
                    .get_for_model(node_type).pk,
                    object_id=None,
                    object_repr='MCP catalog import',
                    action_flag=CHANGE,
                    change_message=change_message,
                )
        except Exception:
            pass

    def get_load_records(self) -> dict:
        """{name: sha256} of fixtures loaded through this tool."""
        return {
            r.name: r.sha256
            for r in FixtureLoad.objects.only('name', 'sha256')
        }

    def record_load(
        self, name: str, sha256: str, object_count: int,
    ) -> FixtureLoad:
        """Upsert the provenance record after a successful loaddata."""
        record, _ = FixtureLoad.objects.update_or_create(
            name=name,
            defaults={'sha256': sha256, 'object_count': object_count},
        )
        return record
