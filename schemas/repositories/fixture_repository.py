"""Persistence helpers for fixture load-status inspection."""

from django.apps import apps

from ..models import FixtureLoad


class FixtureRepository:
    """Counts which fixture PKs already exist in the database."""

    def count_existing(self, model_label: str, pks: list[str]) -> int:
        model = apps.get_model('schemas', model_label)
        return model.objects.filter(id__in=pks).count()

    def natural_key_exists(self, model_label: str, ref: list[str]) -> bool:
        """Whether a natural-key reference resolves to a live row.

        The model is resolved dynamically by label — the fixture loader
        validates references against arbitrary catalog models. Any failure
        (unknown label, missing row, manager without the method) reads as
        "does not resolve".
        """
        try:
            model = apps.get_model('schemas', model_label)
            model.objects.get_by_natural_key(*ref)
            return True
        except Exception:
            return False

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
