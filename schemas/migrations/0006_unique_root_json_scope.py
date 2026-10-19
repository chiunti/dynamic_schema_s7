from django.db import migrations, models
from django.db.models import Q


# Partial unique constraint: at most one root NodeType per json_scope.
# PostgreSQL treats NULLs as distinct in unique indexes, so root types with
# json_scope=NULL never collide — the check below mirrors that by excluding
# NULL scopes. Empty strings DO collide, which is intended: a scope that
# identifies no catalog must not be shared silently.


def check_unique_root_scopes(apps, schema_editor):
    """Abort with a readable listing if root NodeTypes already share a scope."""
    NodeType = apps.get_model("schemas", "NodeType")
    duplicates = (
        NodeType.objects.filter(is_root=True)
        .exclude(json_scope__isnull=True)
        .values("json_scope")
        .annotate(total=models.Count("id"))
        .filter(total__gt=1)
        .order_by("json_scope")
    )
    scopes = [row["json_scope"] for row in duplicates]
    if scopes:
        listing = ", ".join(repr(scope) for scope in scopes)
        raise RuntimeError(
            "Cannot create constraint uq_snode_types_root_scope: duplicate "
            f"json_scope among root node types: {listing}. Resolve the "
            "duplicates before re-running this migration."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("schemas", "0005_example_seed"),
    ]

    operations = [
        migrations.RunPython(
            check_unique_root_scopes,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="nodetype",
            constraint=models.UniqueConstraint(
                fields=["json_scope"],
                condition=Q(is_root=True),
                name="uq_snode_types_root_scope",
            ),
        ),
    ]
