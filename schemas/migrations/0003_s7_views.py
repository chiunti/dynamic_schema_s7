from django.db import migrations


# Unified published-schema view.
#
# Works for ANY root NodeType (is_root=True, parent_id IS NULL) that follows
# the convention:
#   - n.key (native column on schema_nodes)
#   - AttributeDef json_key='status' → value_string (filtered to 'published')
#   - n.version (native column on schema_nodes)
#
# This single view replaces the former domain-specific published views.
VW_SCHEMA_PUBLISHED_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE VIEW v_schema_published AS
SELECT
  n.id          AS root_node_id,
  nt.name       AS node_type_name,
  nt.json_scope AS json_scope,
  n.key         AS key,
  status_attr.value_string AS status,
  n.version     AS version
FROM schema_nodes n
JOIN schema_node_types nt
  ON nt.id = n.node_type_id
 AND nt.is_root = TRUE
 AND n.parent_id IS NULL
-- status attribute
JOIN schema_attribute_defs ad_status
  ON ad_status.node_type_id = nt.id
 AND ad_status.json_key = 'status'
 AND (ad_status.variant_key IS NULL OR ad_status.variant_key = '')
JOIN schema_node_attributes status_attr
  ON status_attr.node_id = n.id
 AND status_attr.attribute_def_id = ad_status.id
WHERE status_attr.value_string = 'published'
  AND n.key IS NOT NULL
  AND n.version IS NOT NULL;
"""

VW_SCHEMA_PUBLISHED_ROLLBACK_SQL = r"""
SET search_path TO s7, public;
DROP VIEW IF EXISTS v_schema_published;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("schemas", "0002_s7_routines"),
    ]

    operations = [
        migrations.RunSQL(
            sql=VW_SCHEMA_PUBLISHED_SQL,
            reverse_sql=VW_SCHEMA_PUBLISHED_ROLLBACK_SQL,
        ),
    ]
