from django.db import migrations, models
from django.db.models import Q


# MCP feature batch — catalog hardening (feature tag: `mcp`).
#
# --- root scope uniqueness --------------------------------------------------
#
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
#
# --- attribute-def domain backfill -----------------------------------------
#
# Backfill stale materialized AttributeDefs that carry no domain. The strict
# validate path resolves the effective domain of an attribute through its
# declaration: an exact-variant row wins over the variant_key IS NULL
# template. Materialized defs created before the template declared a domain
# — or by permissive imports that never copy it — therefore resolved to
# themselves (domain=None) and silently disabled the domain check. The
# declaration lookup is now domain-aware, and the backfill repairs the
# existing rows by copying domain_id from the template row of the same
# (node_type_id, json_key).
#
# The scalar subquery with ORDER BY t.id LIMIT 1 keeps the UPDATE
# deterministic: uq_sattr_defs_key does not use NULLS NOT DISTINCT, so in
# theory several variant_key IS NULL rows can coexist for one
# (node_type_id, json_key) — an UPDATE ... FROM would then pick an
# arbitrary template among them.
#
# The statement is idempotent (rows already carrying a domain, or without
# a domain-bearing template, are left untouched) and intentionally keeps a
# noop reverse: backfilled rows cannot be told apart from rows that had
# domain_id set before this migration ran, so no selective undo is
# possible.

BACKFILL_DOMAIN_SQL = """
UPDATE s7.schema_attribute_defs AS d
SET domain_id = (
    SELECT t.domain_id
    FROM s7.schema_attribute_defs AS t
    WHERE t.node_type_id = d.node_type_id
      AND t.json_key = d.json_key
      AND t.variant_key IS NULL
      AND t.domain_id IS NOT NULL
    ORDER BY t.id
    LIMIT 1
)
WHERE d.variant_key IS NOT NULL
  AND d.domain_id IS NULL
  AND EXISTS (
      SELECT 1
      FROM s7.schema_attribute_defs AS t
      WHERE t.node_type_id = d.node_type_id
        AND t.json_key = d.json_key
        AND t.variant_key IS NULL
        AND t.domain_id IS NOT NULL
  );
"""


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
        migrations.RunSQL(
            sql=BACKFILL_DOMAIN_SQL,
            # Intentional noop: rows updated by the backfill are
            # indistinguishable from rows that already carried a domain,
            # so the reverse cannot selectively undo only the backfill.
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
