from django.db import migrations, models


# Core storage — widen NodeAttribute.value_string to text.
#
# Real schema payloads carry attribute values well beyond 255 characters
# (e.g. HTML hint/description strings), so the varchar(255) cap was
# arbitrary rather than a real constraint: inserts hitting it raised
# DataError at the database layer. The s7 routines already treat
# value_string as text everywhere, so this is a pure column-type
# widening — varchar(n) and text share the same on-disk representation
# in PostgreSQL, making the alter cheap and lossless.
#
# v_schema_published reads value_string, and PostgreSQL refuses to alter
# the type of a column referenced by a view or rule. The view is
# therefore dropped before the alter and recreated afterwards with its
# current definition (the project_id variant). Reversing restores the
# varchar(255) column — possible while no stored value exceeds the old
# cap — between the same drop/recreate pair.

DROP_VW_SCHEMA_PUBLISHED_SQL = r"""
SET search_path TO s7, public;

DROP VIEW IF EXISTS v_schema_published;
"""

CREATE_VW_SCHEMA_PUBLISHED_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE VIEW v_schema_published AS
SELECT
  n.id          AS root_node_id,
  nt.name       AS node_type_name,
  nt.json_scope AS json_scope,
  n.key         AS key,
  status_attr.value_string AS status,
  n.version     AS version,
  n.project_id  AS project_id
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


class Migration(migrations.Migration):

    dependencies = [
        ("schemas", "0007_mcp_feature"),
    ]

    operations = [
        migrations.RunSQL(
            sql=DROP_VW_SCHEMA_PUBLISHED_SQL,
            reverse_sql=CREATE_VW_SCHEMA_PUBLISHED_SQL,
        ),
        migrations.AlterField(
            model_name="nodeattribute",
            name="value_string",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.RunSQL(
            sql=CREATE_VW_SCHEMA_PUBLISHED_SQL,
            reverse_sql=DROP_VW_SCHEMA_PUBLISHED_SQL,
        ),
    ]
