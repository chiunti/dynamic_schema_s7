# feature: mcp

import django.db.models.deletion
import django.utils.timezone
import schemas.models
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("schemas", "0008_value_string_text"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CatalogChangeSet",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        db_default=models.Func(function="gen_random_uuid"),
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("proposed", "Proposed"),
                            ("validating", "Validating"),
                            ("valid", "Valid"),
                            ("invalid", "Invalid"),
                            ("approved", "Approved"),
                            ("applied", "Applied"),
                            ("rejected", "Rejected"),
                            ("stale", "Stale"),
                            ("superseded", "Superseded"),
                        ],
                        default="draft",
                        max_length=16,
                    ),
                ),
                ("ops", models.JSONField(default=list)),
                ("rationale", models.TextField()),
                ("scopes", models.JSONField(default=list)),
                (
                    "base_revision",
                    models.CharField(blank=True, max_length=64, null=True),
                ),
                ("validation_report", models.JSONField(blank=True, null=True)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "proposed_by_credential",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="proposed_catalog_changesets",
                        to="schemas.projectapicredential",
                    ),
                ),
                (
                    "proposed_by_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="proposed_catalog_changesets",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reviewed_catalog_changesets",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Catalog changeset",
                "verbose_name_plural": "Catalog changesets",
                "db_table": "schema_catalog_changesets",
                "indexes": [
                    models.Index(
                        fields=["status", "created_at"],
                        name="idx_scatalogchg_status_created",
                    ),
                    schemas.models.GinIndex(
                        fields=["scopes"], name="idx_scatalogchg_scopes"
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("proposed_by_credential__isnull", False),
                                ("proposed_by_user__isnull", True),
                            ),
                            models.Q(
                                ("proposed_by_credential__isnull", True),
                                ("proposed_by_user__isnull", False),
                            ),
                            _connector="OR",
                        ),
                        name="chk_scatalogchg_one_proposer",
                        violation_error_message="exactly one of proposed_by_credential / proposed_by_user is required",
                    )
                ],
            },
        ),
    ]
