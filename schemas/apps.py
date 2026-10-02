from django.apps import AppConfig
from django.contrib import admin


class SchemasConfig(AppConfig):
    # NOTE: This default applies ONLY to third-party apps (django.contrib.*).
    # All models in this app (schemas) explicitly define UUID primary keys per AGENTS.md
    # conventions. Exception: accounts.CustomUser uses BigAutoField (standard Django
    # user-model convention). Do NOT rely on this default - always specify UUID PK explicitly.
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'schemas'
    verbose_name = 'Structure Seven (S7)'

    def ready(self):
        import schemas.admin
        # Configure admin branding
        admin.site.site_header = "Structure Seven (S7) Admin"
        admin.site.site_title = "S7 Admin"
        admin.site.index_title = "S7 Administration"
