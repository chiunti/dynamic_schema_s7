import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


# MCP feature batch — per-user MCP tokens (feature tag: mcp).
# feature: mcp
#
# UserMCPToken is a per-user bearer credential for the MCP channel. Unlike
# project API credentials — a B2B contract scoped by boolean capability
# flags — each row belongs to one user and carries an mcp_level
# (1 = schema authoring, 2 = catalog authoring) that the server evaluates to
# gate MCP-only capabilities; the same user may hold several tokens at
# different levels and each can be revoked independently.
#
# The token secret is never persisted: only its SHA-256 hex digest is stored
# in token_digest, so a leaked row cannot be replayed as a bearer credential.
# Tokens expire on expires_at and are revoked by stamping revoked_at; the
# (user, revoked_at) index supports listing a user's active tokens.


class Migration(migrations.Migration):

    dependencies = [
        ("schemas", "0006_mcp_hardening"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="UserMCPToken",
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
                ("name", models.CharField(max_length=255)),
                ("token_digest", models.CharField(max_length=64, unique=True)),
                (
                    "mcp_level",
                    models.PositiveSmallIntegerField(
                        choices=[(1, "Schema authoring"), (2, "Catalog authoring")],
                        default=1,
                    ),
                ),
                ("expires_at", models.DateField()),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mcp_tokens",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "User MCP token",
                "verbose_name_plural": "User MCP tokens",
                "db_table": "schema_user_mcp_tokens",
                "indexes": [
                    models.Index(
                        fields=["user", "revoked_at"], name="idx_user_mcp_tok_active"
                    )
                ],
            },
        ),
    ]
