from django.db import migrations


# MCP feature batch — display_order build exclusion (feature tag: mcp).
# feature: mcp
#
# s7_build_node_json emits node JSON from two channels: rows stored in
# schema_node_attributes aggregated in v_attrs, and values derived from
# schema_nodes columns in v_natural_attrs. The declared invariant is that
# natural_* datatypes and display_order never store a value row —
# display_order is column-mapped and server-managed, derived from
# sort_order as sort_order + 1.
#
# The v_attrs SELECT already filtered out natural_* and internal
# datatypes, but not display_order: any display_order row that does land
# in schema_node_attributes anyway (dead data written before the
# invariant was enforced) was emitted verbatim, leaking the stored value
# next to the derived one. The SELECT now excludes
# dt.name = 'display_order' as well, sealing the output channel for rows
# already persisted in existing databases.
#
# The derived v_natural_attrs block is unchanged, including its
# ad.variant_key IS NULL predicate.
#
# The forward also NULLs last_cached_build in schema_build_state: the
# cache-serving predicate (last_cached_build IS NOT DISTINCT FROM
# current_build) would otherwise keep serving schema_json rows built by
# the previous routine body until the next natural invalidation.
#
# reverse_sql restores the exact pre-migration routine body — the last
# definition applied before this migration.


 # ------------------------------
 # build_node_json
 # ------------------------------
FN_BUILD_NODE_JSON_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_build_node_json(p_node_id UUID)
RETURNS JSONB AS $$
DECLARE
  v_key             TEXT;
  v_node_key        TEXT;
  v_node_id_text    TEXT;
  v_json_scope      TEXT;
  v_attrs           JSONB;
  v_natural_attrs   JSONB;
  v_children_json   JSONB;
  v_keyed_children  JSONB;
  v_map_children    JSONB;
  v_result          JSONB;
  v_slots           JSONB;
  v_node_type_name  TEXT;
  v_node_sort_order INT;
BEGIN
  SELECT nt.json_scope, nt.name, n.key, n.id::text, n.sort_order
  INTO v_json_scope, v_node_type_name, v_node_key, v_node_id_text, v_node_sort_order
  FROM s7.schema_nodes n
  JOIN s7.schema_node_types nt ON nt.id = n.node_type_id
  WHERE n.id = p_node_id;

  IF v_node_id_text IS NULL THEN
    RAISE EXCEPTION 'Node not found for id=%', p_node_id;
  END IF;
  
  -- Emit 'key' in the output JSON only when the node type has an explicit AttributeDef for it.
  -- Metadata nodes do not define a 'key' AttributeDef,
  -- so v_key remains NULL and is stripped by jsonb_strip_nulls.
  SELECT na.value_string
  INTO v_key
  FROM s7.schema_node_attributes na
  JOIN s7.schema_attribute_defs ad ON ad.id = na.attribute_def_id
  JOIN s7.schema_nodes n ON n.id = p_node_id
  WHERE na.node_id = p_node_id
    AND ad.json_key = 'key'
    AND ad.node_type_id = n.node_type_id
  LIMIT 1;

  -- Collect stored attributes (excluding natural_*, internal and
  -- display_order types). display_order never stores a value row: any
  -- stored row is dead data — the emitted value is derived from
  -- sort_order in the v_natural_attrs block below.
  SELECT
    jsonb_object_agg(ad.json_key,
                     CASE dt.name
                       WHEN 'int' THEN to_jsonb(TRUNC(na.value_number)::bigint)
                       ELSE COALESCE(na.value_number::text::jsonb, na.value_bool::text::jsonb, na.value_json, to_jsonb(na.value_string))
                     END)
  INTO v_attrs
  FROM s7.schema_node_attributes na
  JOIN s7.schema_attribute_defs ad ON ad.id = na.attribute_def_id
  JOIN s7.schema_data_types dt ON dt.id = ad.data_type_id
  WHERE na.node_id = p_node_id
    AND dt.name NOT LIKE 'natural_%'
    AND dt.name <> 'internal'
    AND dt.name <> 'display_order';

  IF EXISTS (
    SELECT 1 FROM s7.schema_node_types
    WHERE name = v_node_type_name AND json_scalar_attribute IS NOT NULL
  ) THEN
    RETURN v_attrs -> (
      SELECT json_scalar_attribute FROM s7.schema_node_types WHERE name = v_node_type_name
    );
  END IF;

  -- Build natural field overrides from schema_nodes columns, driven by AttributeDef definitions.
  -- Only include natural fields that are explicitly defined as AttributeDefs for this node's type.
  SELECT jsonb_object_agg(
    ad.json_key,
    CASE dt.name
      WHEN 'natural_uuid'    THEN to_jsonb(p_node_id::text)
      WHEN 'natural_key'     THEN to_jsonb(v_node_key)
      WHEN 'natural_version' THEN to_jsonb(n.version)
      WHEN 'natural_order'   THEN to_jsonb(v_node_sort_order)
      WHEN 'display_order'   THEN to_jsonb(v_node_sort_order + 1)
    END
  )
  INTO v_natural_attrs
  FROM s7.schema_attribute_defs ad
  JOIN s7.schema_data_types dt ON dt.id = ad.data_type_id
  JOIN s7.schema_nodes n ON n.id = p_node_id
  WHERE ad.node_type_id = n.node_type_id
    AND (dt.name LIKE 'natural_%' OR dt.name = 'display_order')
    AND ad.variant_key IS NULL;

  -- Extract all children grouped by collection_key
  -- If max_children=1, extract as object (not array)
  -- For singleton slots (max_children=1): child.key must match collection_key exactly
  -- For collections (max_children != 1): include all children of the correct type
  SELECT
    jsonb_object_agg(
      col.collection_key,
      CASE
        WHEN col.max_children = 1
        THEN col.children_json->0
        ELSE col.children_json
      END
    )
  INTO v_children_json
  FROM (
    SELECT
      ntc.collection_key,
      ntc.max_children,
      jsonb_agg(s7_build_node_json(ch.id) ORDER BY ch.sort_order) AS children_json
    FROM s7.schema_nodes parent
    JOIN s7.schema_node_type_compositions ntc
      ON ntc.parent_type_id = parent.node_type_id
      AND ntc.collection_key IS NOT NULL
    JOIN s7.schema_nodes ch
      ON ch.parent_id = parent.id
      AND ch.node_type_id = ntc.child_type_id
      AND NOT EXISTS (SELECT 1 FROM s7.schema_node_types mapped WHERE mapped.id = ch.node_type_id AND mapped.is_json_map)
      AND (
        -- For singleton slots: key must match collection_key exactly
        (ntc.max_children = 1 AND ch.key = ntc.collection_key)
        OR
        -- For collections: include all children of this type under this parent
        (ntc.max_children IS NULL OR ntc.max_children != 1)
      )
    WHERE parent.id = p_node_id
    GROUP BY ntc.collection_key, ntc.max_children
  ) AS col;

  -- Extract children without collection_key (e.g. layout, props sub-nodes).
  -- Also captures shorthand-dispatch children: nodes in singleton compositions
  -- (max_children=1) that were stored under a key != ntc.collection_key
  -- (e.g. appbar, fab, drawer stored as screen_section children).
  -- Fallback key for unkeyed children: the child type's declared
  -- default_json_key first (e.g. a props node type declares 'props'),
  -- then the raw node_type name as last resort.
  SELECT jsonb_object_agg(COALESCE(ch.key, ch_nt.default_json_key, ch_nt.name), s7_build_node_json(ch.id))
  INTO v_keyed_children
  FROM s7.schema_nodes ch
  JOIN s7.schema_node_types ch_nt ON ch_nt.id = ch.node_type_id
  JOIN s7.schema_node_type_compositions ntc
    ON ntc.child_type_id = ch.node_type_id
  JOIN s7.schema_nodes parent ON parent.id = p_node_id
    AND ntc.parent_type_id = parent.node_type_id
  WHERE ch.parent_id = p_node_id
    AND ch_nt.is_json_map = FALSE
    AND (
      ntc.collection_key IS NULL
      OR (ntc.max_children = 1 AND ch.key <> ntc.collection_key)
    );

  IF v_keyed_children IS NOT NULL THEN
    v_children_json := COALESCE(v_children_json, '{}'::jsonb) || v_keyed_children;
  END IF;

  v_result := (
    jsonb_strip_nulls(
      COALESCE(v_natural_attrs, '{}'::jsonb)
    || COALESCE(v_attrs, '{}'::jsonb)
    )
    || COALESCE(v_children_json, '{}'::jsonb)
    || COALESCE(v_natural_attrs, '{}'::jsonb)
  );

  -- Strip empty objects (e.g., "props": {}, "layout": {}) from the final JSON
  v_result := s7_strip_empty_values(v_result);

  SELECT jsonb_object_agg(ch.key, s7_build_node_json(ch.id))
  INTO v_map_children
  FROM s7.schema_nodes ch
  JOIN s7.schema_node_types nt ON nt.id = ch.node_type_id
  WHERE ch.parent_id = p_node_id
    AND nt.is_json_map = TRUE;
  v_result := v_result || COALESCE(v_map_children, '{}'::jsonb);

  RETURN v_result;
END;
$$ LANGUAGE plpgsql;

-- Force a rebuild of every cached schema so the next read serves output
-- of this routine body, not stale JSON produced by the previous
-- definition (the serving predicate is
-- last_cached_build IS NOT DISTINCT FROM current_build).
UPDATE s7.schema_build_state SET last_cached_build = NULL;
"""

FN_BUILD_NODE_JSON_ROLLBACK_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_build_node_json(p_node_id UUID)
RETURNS JSONB AS $$
DECLARE
  v_key             TEXT;
  v_node_key        TEXT;
  v_node_id_text    TEXT;
  v_json_scope      TEXT;
  v_attrs           JSONB;
  v_natural_attrs   JSONB;
  v_children_json   JSONB;
  v_keyed_children  JSONB;
  v_map_children    JSONB;
  v_result          JSONB;
  v_slots           JSONB;
  v_node_type_name  TEXT;
  v_node_sort_order INT;
BEGIN
  SELECT nt.json_scope, nt.name, n.key, n.id::text, n.sort_order
  INTO v_json_scope, v_node_type_name, v_node_key, v_node_id_text, v_node_sort_order
  FROM s7.schema_nodes n
  JOIN s7.schema_node_types nt ON nt.id = n.node_type_id
  WHERE n.id = p_node_id;

  IF v_node_id_text IS NULL THEN
    RAISE EXCEPTION 'Node not found for id=%', p_node_id;
  END IF;
  
  -- Emit 'key' in the output JSON only when the node type has an explicit AttributeDef for it.
  -- Metadata nodes do not define a 'key' AttributeDef,
  -- so v_key remains NULL and is stripped by jsonb_strip_nulls.
  SELECT na.value_string
  INTO v_key
  FROM s7.schema_node_attributes na
  JOIN s7.schema_attribute_defs ad ON ad.id = na.attribute_def_id
  JOIN s7.schema_nodes n ON n.id = p_node_id
  WHERE na.node_id = p_node_id
    AND ad.json_key = 'key'
    AND ad.node_type_id = n.node_type_id
  LIMIT 1;

  -- Collect stored attributes (excluding natural_* and internal types)
  SELECT
    jsonb_object_agg(ad.json_key,
                     CASE dt.name
                       WHEN 'int' THEN to_jsonb(TRUNC(na.value_number)::bigint)
                       ELSE COALESCE(na.value_number::text::jsonb, na.value_bool::text::jsonb, na.value_json, to_jsonb(na.value_string))
                     END)
  INTO v_attrs
  FROM s7.schema_node_attributes na
  JOIN s7.schema_attribute_defs ad ON ad.id = na.attribute_def_id
  JOIN s7.schema_data_types dt ON dt.id = ad.data_type_id
  WHERE na.node_id = p_node_id
    AND dt.name NOT LIKE 'natural_%'
    AND dt.name <> 'internal';

  IF EXISTS (
    SELECT 1 FROM s7.schema_node_types
    WHERE name = v_node_type_name AND json_scalar_attribute IS NOT NULL
  ) THEN
    RETURN v_attrs -> (
      SELECT json_scalar_attribute FROM s7.schema_node_types WHERE name = v_node_type_name
    );
  END IF;

  -- Build natural field overrides from schema_nodes columns, driven by AttributeDef definitions.
  -- Only include natural fields that are explicitly defined as AttributeDefs for this node's type.
  SELECT jsonb_object_agg(
    ad.json_key,
    CASE dt.name
      WHEN 'natural_uuid'    THEN to_jsonb(p_node_id::text)
      WHEN 'natural_key'     THEN to_jsonb(v_node_key)
      WHEN 'natural_version' THEN to_jsonb(n.version)
      WHEN 'natural_order'   THEN to_jsonb(v_node_sort_order)
      WHEN 'display_order'   THEN to_jsonb(v_node_sort_order + 1)
    END
  )
  INTO v_natural_attrs
  FROM s7.schema_attribute_defs ad
  JOIN s7.schema_data_types dt ON dt.id = ad.data_type_id
  JOIN s7.schema_nodes n ON n.id = p_node_id
  WHERE ad.node_type_id = n.node_type_id
    AND (dt.name LIKE 'natural_%' OR dt.name = 'display_order')
    AND ad.variant_key IS NULL;

  -- Extract all children grouped by collection_key
  -- If max_children=1, extract as object (not array)
  -- For singleton slots (max_children=1): child.key must match collection_key exactly
  -- For collections (max_children != 1): include all children of the correct type
  SELECT
    jsonb_object_agg(
      col.collection_key,
      CASE
        WHEN col.max_children = 1
        THEN col.children_json->0
        ELSE col.children_json
      END
    )
  INTO v_children_json
  FROM (
    SELECT
      ntc.collection_key,
      ntc.max_children,
      jsonb_agg(s7_build_node_json(ch.id) ORDER BY ch.sort_order) AS children_json
    FROM s7.schema_nodes parent
    JOIN s7.schema_node_type_compositions ntc
      ON ntc.parent_type_id = parent.node_type_id
      AND ntc.collection_key IS NOT NULL
    JOIN s7.schema_nodes ch
      ON ch.parent_id = parent.id
      AND ch.node_type_id = ntc.child_type_id
      AND NOT EXISTS (SELECT 1 FROM s7.schema_node_types mapped WHERE mapped.id = ch.node_type_id AND mapped.is_json_map)
      AND (
        -- For singleton slots: key must match collection_key exactly
        (ntc.max_children = 1 AND ch.key = ntc.collection_key)
        OR
        -- For collections: include all children of this type under this parent
        (ntc.max_children IS NULL OR ntc.max_children != 1)
      )
    WHERE parent.id = p_node_id
    GROUP BY ntc.collection_key, ntc.max_children
  ) AS col;

  -- Extract children without collection_key (e.g. layout, props sub-nodes).
  -- Also captures shorthand-dispatch children: nodes in singleton compositions
  -- (max_children=1) that were stored under a key != ntc.collection_key
  -- (e.g. appbar, fab, drawer stored as screen_section children).
  -- Fallback key for unkeyed children: the child type's declared
  -- default_json_key first (e.g. a props node type declares 'props'),
  -- then the raw node_type name as last resort.
  SELECT jsonb_object_agg(COALESCE(ch.key, ch_nt.default_json_key, ch_nt.name), s7_build_node_json(ch.id))
  INTO v_keyed_children
  FROM s7.schema_nodes ch
  JOIN s7.schema_node_types ch_nt ON ch_nt.id = ch.node_type_id
  JOIN s7.schema_node_type_compositions ntc
    ON ntc.child_type_id = ch.node_type_id
  JOIN s7.schema_nodes parent ON parent.id = p_node_id
    AND ntc.parent_type_id = parent.node_type_id
  WHERE ch.parent_id = p_node_id
    AND ch_nt.is_json_map = FALSE
    AND (
      ntc.collection_key IS NULL
      OR (ntc.max_children = 1 AND ch.key <> ntc.collection_key)
    );

  IF v_keyed_children IS NOT NULL THEN
    v_children_json := COALESCE(v_children_json, '{}'::jsonb) || v_keyed_children;
  END IF;

  v_result := (
    jsonb_strip_nulls(
      COALESCE(v_natural_attrs, '{}'::jsonb)
    || COALESCE(v_attrs, '{}'::jsonb)
    )
    || COALESCE(v_children_json, '{}'::jsonb)
    || COALESCE(v_natural_attrs, '{}'::jsonb)
  );

  -- Strip empty objects (e.g., "props": {}, "layout": {}) from the final JSON
  v_result := s7_strip_empty_values(v_result);

  SELECT jsonb_object_agg(ch.key, s7_build_node_json(ch.id))
  INTO v_map_children
  FROM s7.schema_nodes ch
  JOIN s7.schema_node_types nt ON nt.id = ch.node_type_id
  WHERE ch.parent_id = p_node_id
    AND nt.is_json_map = TRUE;
  v_result := v_result || COALESCE(v_map_children, '{}'::jsonb);

  RETURN v_result;
END;
$$ LANGUAGE plpgsql;
"""


class Migration(migrations.Migration):
    dependencies = [
        ('schemas', '0008_mcp_root_key_version_scope'),
    ]

    operations = [
        migrations.RunSQL(
            FN_BUILD_NODE_JSON_SQL,
            reverse_sql=FN_BUILD_NODE_JSON_ROLLBACK_SQL,
        ),
    ]
