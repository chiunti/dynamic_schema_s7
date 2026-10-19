from django.db import migrations, models
from django.db.models import Q


# Partial unique constraint: at most one root NodeType per json_scope —
# including the scope-less case. nulls_distinct=False emits NULLS NOT
# DISTINCT (PostgreSQL >= 15, already the project floor), so at most one
# root may also carry json_scope=NULL; without it PostgreSQL treats NULLs
# as distinct and unscoped roots would never collide.
#
# The check below covers both dimensions before the index is built:
# duplicated named scopes AND multiple NULL-scope roots — either could
# legitimately exist beforehand. Empty strings DO collide, which is
# intended: a scope that identifies no catalog must not be shared silently.


def check_unique_root_scopes(apps, schema_editor):
    """Abort with a readable listing if root NodeTypes violate scope uniqueness."""
    NodeType = apps.get_model("schemas", "NodeType")

    null_scope_names = list(
        NodeType.objects.filter(is_root=True, json_scope__isnull=True)
        .values_list("name", flat=True)
        .order_by("name")
    )
    if len(null_scope_names) > 1:
        listing = ", ".join(repr(name) for name in null_scope_names)
        raise RuntimeError(
            "Cannot create constraint uq_snode_types_root_scope with "
            "NULLS NOT DISTINCT: multiple root node types have "
            f"json_scope=NULL: {listing}. Keep at most one before "
            "re-running this migration."
        )

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
                nulls_distinct=False,
            ),
        ),
    ]
