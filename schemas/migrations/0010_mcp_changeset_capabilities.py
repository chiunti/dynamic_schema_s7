# feature: mcp

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("schemas", "0009_mcp_changesets"),
    ]

    operations = [
        migrations.AddField(
            model_name="usermcptoken",
            name="allowed_scopes",
            field=models.JSONField(
                blank=True,
                help_text=(
                    "Allowlist of json_scope values the token's changesets "
                    "may touch. NULL or empty list means unrestricted — "
                    "user tokens carry no project bound since the catalog "
                    "grammar is global."
                ),
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="usermcptoken",
            name="can_propose_changeset",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Grants the token the changeset-propose capability on "
                    "top of catalog-authoring level — proposing governed "
                    "mutations without direct apply."
                ),
            ),
        ),
    ]
