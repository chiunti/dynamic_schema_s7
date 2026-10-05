"""Persistence for the node-editor extension registry."""

from ..models import EditorExtension


class EditorExtensionRepository:
    """Thin ORM wrapper around EditorExtension rows."""

    def get_by_name(self, name: str) -> EditorExtension | None:
        return EditorExtension.objects.filter(name=name).first()

    def list_all(self) -> list[EditorExtension]:
        return list(EditorExtension.objects.order_by('name'))

    def list_enabled(self) -> list[EditorExtension]:
        return list(
            EditorExtension.objects.filter(is_enabled=True).order_by('name')
        )

    def register_static(self, name: str, filename: str) -> EditorExtension:
        """Idempotent registry row for a JS file present on disk."""
        ext, _ = EditorExtension.objects.get_or_create(
            name=name,
            defaults={
                'filename': filename,
                'source': EditorExtension.SOURCE_STATIC,
            },
        )
        return ext

    def upsert_uploaded(
        self,
        name: str,
        filename: str,
        content: str,
        description: str = '',
    ) -> EditorExtension:
        """Create or replace a DB-sourced extension."""
        ext = self.get_by_name(name)
        if ext is None:
            ext = EditorExtension(name=name)
        ext.filename = filename
        ext.source = EditorExtension.SOURCE_DB
        ext.content = content
        if description:
            ext.description = description
        ext.save()
        return ext

    def disable_missing_static(self, present_names: set[str]) -> int:
        """Disable static-sourced rows whose file no longer exists."""
        return EditorExtension.objects.filter(
            source=EditorExtension.SOURCE_STATIC,
        ).exclude(name__in=present_names).update(is_enabled=False)

    def set_enabled(self, names: list[str], enabled: bool) -> int:
        return EditorExtension.objects.filter(name__in=names).update(
            is_enabled=enabled,
        )
