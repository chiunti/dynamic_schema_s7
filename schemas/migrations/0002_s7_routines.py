from django.db import migrations


 # ------------------------------
 # prevent_node_cycles
 # ------------------------------
FN_PREVENT_NODE_CYCLES_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_prevent_node_cycles()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.parent_id IS NULL THEN
    RETURN NEW;
  END IF;

  IF NEW.id = NEW.parent_id THEN
    RAISE EXCEPTION 'Node cannot be its own parent';
  END IF;

  IF TG_OP = 'UPDATE' AND OLD.parent_id IS NOT DISTINCT FROM NEW.parent_id THEN
    RETURN NEW;
  END IF;

  IF EXISTS (
    WITH RECURSIVE ancestors AS (
      SELECT parent_id FROM schema_nodes WHERE id = NEW.parent_id
      UNION ALL
      SELECT n.parent_id
      FROM schema_nodes n
      JOIN ancestors a ON n.id = a.parent_id
      WHERE n.parent_id IS NOT NULL
    )
    SELECT 1 FROM ancestors WHERE parent_id = NEW.id
  ) THEN
    RAISE EXCEPTION 'Cycle detected in node hierarchy';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # validate_value_type
 # ------------------------------
FN_VALIDATE_VALUE_TYPE_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_validate_value_type()
RETURNS TRIGGER AS $$
DECLARE
  v_type_name TEXT;
BEGIN
  SELECT dt.name
  INTO v_type_name
  FROM schema_attribute_defs ad
  JOIN schema_data_types dt ON dt.id = ad.data_type_id
  WHERE ad.id = NEW.attribute_def_id;

  IF v_type_name IS NULL THEN
    RAISE EXCEPTION 'schema_attribute_defs/schema_data_types not found for attribute_def_id=%', NEW.attribute_def_id;
  END IF;

  -- natural_* types and display_order never store a value row — they read directly from schema_nodes columns.
  -- Rows with these types should never reach this trigger, but guard here for safety.
  IF v_type_name LIKE 'natural_%' OR v_type_name = 'display_order' THEN
    RETURN NEW;
  END IF;

  IF v_type_name IN ('string', 'date', 'color', 'internal') THEN
    IF NEW.value_string IS NULL THEN
      RAISE EXCEPTION 'Expected value_string for data_type=% (attribute_def_id=%)', v_type_name, NEW.attribute_def_id;
    END IF;
  ELSIF v_type_name IN ('number', 'int', 'float', 'integer', 'decimal') THEN
    IF NEW.value_number IS NULL THEN
      RAISE EXCEPTION 'Expected value_number for data_type=% (attribute_def_id=%)', v_type_name, NEW.attribute_def_id;
    END IF;
  ELSIF v_type_name IN ('bool', 'boolean') THEN
    IF NEW.value_bool IS NULL THEN
      RAISE EXCEPTION 'Expected value_bool for data_type=% (attribute_def_id=%)', v_type_name, NEW.attribute_def_id;
    END IF;
  ELSIF v_type_name IN ('json', 'list_string', 'int_tuple', 'dict', 'list_int') THEN
    IF NEW.value_json IS NULL THEN
      RAISE EXCEPTION 'Expected value_json for data_type=% (attribute_def_id=%)', v_type_name, NEW.attribute_def_id;
    END IF;
  ELSE
    IF NEW.value_json IS NULL THEN
      RAISE EXCEPTION 'Expected value_json for data_type=% (attribute_def_id=%)', v_type_name, NEW.attribute_def_id;
    END IF;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # build_node_conditions
 # ------------------------------
FN_BUILD_NODE_CONDITIONS_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7.s7_build_node_conditions(
  p_node_id   UUID,
  OUT o_show_if    JSONB,
  OUT o_enabled_if JSONB
)
AS $$
BEGIN
  WITH cg AS (
         SELECT n.id
         FROM s7.schema_nodes n
         JOIN s7.schema_node_types nt ON nt.id = n.node_type_id
         WHERE n.parent_id = p_node_id
           AND nt.name IN ('condition_group', 'sdui_show_if')
       ),
       cg_attrs AS (
         SELECT
           na.node_id,
           ad.name,
           na.value_string,
           na.value_json
         FROM s7.schema_node_attributes na
         JOIN s7.schema_attribute_defs ad ON ad.id = na.attribute_def_id
         JOIN cg ON cg.id = na.node_id
       ),
       condition_groups AS (
         SELECT
           n.id,
           -- Use stored 'usage' attribute when available, fall back to node.key
           -- (sdui_show_if nodes store the slot in node.key, not as an attribute)
           COALESCE(
             MAX(CASE WHEN ca.name = 'usage' THEN ca.value_string END),
             n.key
           ) AS usage,
           MAX(CASE WHEN ca.name = 'logic' THEN ca.value_string END) AS logic
         FROM cg
         JOIN s7.schema_nodes n ON n.id = cg.id
         LEFT JOIN cg_attrs ca ON ca.node_id = n.id
         GROUP BY n.id, n.key
       ),
       conditions AS (
         SELECT
           n.id,
           n.parent_id,
           MAX(CASE WHEN ad.name = 'left_kind'       THEN na.value_string END) AS left_kind,
           MAX(CASE WHEN ad.name = 'left_field_key'  THEN na.value_string END) AS left_field_key,
           MAX(CASE WHEN ad.name = 'op'              THEN na.value_string END) AS op,
           MAX(CASE WHEN ad.name = 'right_kind'      THEN na.value_string END) AS right_kind,
           MAX(CASE WHEN ad.name = 'right_field_key' THEN na.value_string END) AS right_field_key,
           MAX(CASE WHEN ad.name = 'left_value'
                    THEN s7_attribute_value_to_jsonb(na.value_string, na.value_number, na.value_bool, na.value_json)::text
                    END)::jsonb AS left_value,
           MAX(CASE WHEN ad.name = 'right_value'
                    THEN s7_attribute_value_to_jsonb(na.value_string, na.value_number, na.value_bool, na.value_json)::text
                    END)::jsonb AS right_value
         FROM s7.schema_nodes n
         JOIN s7.schema_node_types nt ON nt.id = n.node_type_id AND nt.name = 'condition'
         JOIN s7.schema_node_attributes na ON na.node_id = n.id
         JOIN s7.schema_attribute_defs ad ON ad.id = na.attribute_def_id
         WHERE n.parent_id IN (SELECT id FROM cg)
         GROUP BY n.id, n.parent_id
       ),
       condition_group_configs AS (
         SELECT
           cg.id,
           cg.usage,
           jsonb_build_object(
             'logic', COALESCE(cg.logic, 'and'),
             'conditions',
               COALESCE(
                 (
                   SELECT jsonb_agg(
                            jsonb_build_object(
                              'left',  CASE
                                          WHEN c.left_kind = 'field' THEN jsonb_build_object('field', c.left_field_key)
                                          WHEN c.left_kind = 'value' THEN jsonb_build_object('value', COALESCE(c.left_value, 'null'::jsonb))
                                          ELSE NULL::jsonb
                                        END,
                              'op', c.op,
                              'right', CASE
                                          WHEN c.right_kind = 'field' THEN jsonb_build_object('field', c.right_field_key)
                                          WHEN c.right_kind = 'value' THEN jsonb_build_object('value', COALESCE(c.right_value, 'null'::jsonb))
                                          ELSE NULL::jsonb
                                        END
                            )
                            ORDER BY c.id
                          )
                   FROM conditions c
                   WHERE c.parent_id = cg.id
                 ),
                 '[]'::jsonb
               )
           ) AS config_json
         FROM condition_groups cg
       ),
       node_condition_configs AS (
         SELECT
           MAX(CASE WHEN usage = 'show_if'
                    THEN config_json::text END)::jsonb    AS show_if_json,
           MAX(CASE WHEN usage = 'enabled_if'
                    THEN config_json::text END)::jsonb    AS enabled_if_json
         FROM condition_group_configs
       )
  SELECT
    ncc.show_if_json,
    ncc.enabled_if_json
  INTO o_show_if, o_enabled_if
  FROM node_condition_configs ncc;

  IF NOT FOUND THEN
    o_show_if := NULL;
    o_enabled_if := NULL;
  END IF;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # strip_empty_values
 # ------------------------------
FN_STRIP_EMPTY_VALUES_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_strip_empty_values(p_jsonb JSONB)
RETURNS JSONB AS $$
DECLARE
  v_key TEXT;
  v_value JSONB;
  v_result JSONB := '{}'::jsonb;
BEGIN
  FOR v_key, v_value IN SELECT * FROM jsonb_each(p_jsonb) LOOP
    -- Skip null values
    IF v_value IS NULL OR v_value = 'null'::jsonb THEN
      CONTINUE;
    END IF;
    
    -- Skip empty strings
    IF v_value #>> '{}' = '' THEN
      CONTINUE;
    END IF;
    
    -- Skip empty arrays
    IF jsonb_typeof(v_value) = 'array' AND jsonb_array_length(v_value) = 0 THEN
      CONTINUE;
    END IF;
    
    -- Skip empty objects
    IF jsonb_typeof(v_value) = 'object' AND v_value = '{}'::jsonb THEN
      CONTINUE;
    END IF;
    
    -- Recursively process nested objects
    IF jsonb_typeof(v_value) = 'object' THEN
      v_result := v_result || jsonb_build_object(v_key, s7_strip_empty_values(v_value));
    ELSE
      v_result := v_result || jsonb_build_object(v_key, v_value);
    END IF;
  END LOOP;
  
  RETURN v_result;
END;
$$ LANGUAGE plpgsql;
"""


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
  v_show_if         JSONB;
  v_enabled_if      JSONB;
  v_children_json   JSONB;
  v_keyed_children  JSONB;
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

  BEGIN
    SELECT * INTO v_show_if, v_enabled_if
    FROM s7.s7_build_node_conditions(p_node_id);
    
    IF NOT FOUND THEN
      v_show_if := NULL;
      v_enabled_if := NULL;
    END IF;
  EXCEPTION
    WHEN OTHERS THEN
      v_show_if := NULL;
      v_enabled_if := NULL;
      RAISE WARNING 's7_build_node_conditions failed for node_id=%: %', p_node_id, SQLERRM;
  END;

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
  -- Condition-group-like nodes (condition_group / sdui_show_if) are already
  -- handled by s7_build_node_conditions and must be skipped here.
  SELECT jsonb_object_agg(ch.key, s7_build_node_json(ch.id))
  INTO v_keyed_children
  FROM s7.schema_nodes ch
  JOIN s7.schema_node_types ch_nt ON ch_nt.id = ch.node_type_id
  JOIN s7.schema_node_type_compositions ntc
    ON ntc.child_type_id = ch.node_type_id
  JOIN s7.schema_nodes parent ON parent.id = p_node_id
    AND ntc.parent_type_id = parent.node_type_id
  WHERE ch.parent_id = p_node_id
    AND ch_nt.name NOT IN ('condition_group', 'sdui_show_if')
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
    || CASE WHEN v_show_if    IS NOT NULL THEN jsonb_build_object('show_if',     v_show_if)    ELSE '{}'::jsonb END
    || CASE WHEN v_enabled_if IS NOT NULL THEN jsonb_build_object('enabled_if',  v_enabled_if) ELSE '{}'::jsonb END
    )
    || COALESCE(v_children_json, '{}'::jsonb)
    || COALESCE(v_natural_attrs, '{}'::jsonb)
  );

  -- Strip empty objects (e.g., "props": {}, "layout": {}) from the final JSON
  v_result := s7_strip_empty_values(v_result);

  RETURN v_result;
END;
$$ LANGUAGE plpgsql;
"""


 # ------------------------------
 # build_schema
 # ------------------------------
FN_BUILD_SCHEMA_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_build_schema(p_key TEXT, p_version TEXT)
RETURNS JSONB AS $$
DECLARE
  v_root_id UUID;
  v_schema  JSONB;
  v_node_type_name TEXT;
BEGIN
  SELECT root_node_id, node_type_name
  INTO v_root_id, v_node_type_name
  FROM v_schema_published
  WHERE key = p_key
    AND version  = p_version
  LIMIT 1;

  IF v_root_id IS NULL THEN
    RAISE EXCEPTION 'Schema not found for key=% and version=%', p_key, p_version;
  END IF;

  v_schema := s7_build_node_json(v_root_id);
  RETURN jsonb_build_object(v_node_type_name, v_schema);
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # build_schema_text
 # ------------------------------
FN_BUILD_SCHEMA_TEXT_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_build_schema_text(
  p_key TEXT,
  p_version TEXT
)
RETURNS TEXT AS $$
DECLARE
  v_schema JSONB;
BEGIN
  v_schema := s7_build_schema(p_key, p_version);
  RETURN v_schema::TEXT;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # ensure_data_type
 # ------------------------------
FN_ENSURE_DATA_TYPE_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_ensure_data_type(
  p_name TEXT,
  p_description TEXT DEFAULT NULL
)
RETURNS UUID AS $$
DECLARE
  v_id UUID;
BEGIN
  SELECT id INTO v_id FROM schema_data_types WHERE name = p_name LIMIT 1;
  IF v_id IS NOT NULL THEN
    RETURN v_id;
  END IF;

  INSERT INTO schema_data_types (name, description)
  VALUES (p_name, COALESCE(p_description, p_name))
  ON CONFLICT (name) DO NOTHING;

  SELECT id INTO v_id FROM schema_data_types WHERE name = p_name LIMIT 1;
  RETURN v_id;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # ensure_domain
 # ------------------------------
FN_ENSURE_DOMAIN_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_ensure_domain(
  p_domain_name TEXT,
  p_description TEXT DEFAULT NULL
)
RETURNS UUID AS $$
DECLARE
  v_id UUID;
BEGIN
  SELECT id INTO v_id FROM schema_domains WHERE domain_name = p_domain_name LIMIT 1;
  IF v_id IS NULL THEN
    INSERT INTO schema_domains (domain_name, description)
    VALUES (p_domain_name, p_description)
    RETURNING id INTO v_id;
  END IF;

  SELECT id INTO v_id FROM schema_domains WHERE domain_name = p_domain_name LIMIT 1;
  RETURN v_id;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # ensure_domain_item
 # ------------------------------
FN_ENSURE_DOMAIN_ITEM_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_ensure_domain_item(
  p_domain_name TEXT,
  p_value TEXT,
  p_label TEXT DEFAULT NULL
)
RETURNS VOID AS $$
DECLARE
  v_domain_id UUID;
BEGIN
  v_domain_id := s7_ensure_domain(p_domain_name, p_domain_name);
  IF p_value IS NULL THEN
    RETURN;
  END IF;

  INSERT INTO schema_domain_items (domain_id, value, label)
  VALUES (v_domain_id, p_value, COALESCE(p_label, p_value))
  ON CONFLICT (domain_id, value) DO NOTHING;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # attribute_value_to_jsonb
 # ------------------------------
FN_ATTRIBUTE_VALUE_TO_JSONB_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_attribute_value_to_jsonb(
  p_value_string TEXT,
  p_value_number NUMERIC,
  p_value_bool BOOLEAN,
  p_value_json JSONB
)
RETURNS JSONB AS $$
BEGIN
  IF p_value_bool IS NOT NULL THEN
    RETURN p_value_bool::text::jsonb;
  ELSIF p_value_number IS NOT NULL THEN
    RETURN p_value_number::text::jsonb;
  ELSIF p_value_json IS NOT NULL THEN
    RETURN p_value_json;
  ELSIF p_value_string IS NOT NULL THEN
    RETURN to_jsonb(p_value_string);
  ELSE
    RETURN NULL::jsonb;
  END IF;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # ensure_node_type
 # ------------------------------
FN_ENSURE_NODE_TYPE_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_ensure_node_type(
  p_name TEXT,
  p_label TEXT,
  p_is_container BOOLEAN,
  p_is_root BOOLEAN,
  p_json_scope TEXT DEFAULT NULL
)
RETURNS UUID AS $$
DECLARE
  v_id UUID;
BEGIN
  SELECT id INTO v_id FROM schema_node_types WHERE name = p_name LIMIT 1;
  IF v_id IS NULL THEN
    INSERT INTO schema_node_types (name, label, is_container, is_root, json_scope)
    VALUES (p_name, p_label, p_is_container, p_is_root, p_json_scope)
    RETURNING id INTO v_id;
  END IF;

  SELECT id INTO v_id FROM schema_node_types WHERE name = p_name LIMIT 1;
  RETURN v_id;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # ensure_node_type_composition
 # ------------------------------
FN_ENSURE_NODE_TYPE_COMPOSITION_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_ensure_node_type_composition(
  p_parent_type_name TEXT,
  p_child_type_name  TEXT,
  p_collection_key   TEXT DEFAULT NULL
)
RETURNS VOID AS $$
DECLARE
  v_parent_id UUID;
  v_child_id  UUID;
BEGIN
  SELECT id INTO v_parent_id FROM schema_node_types WHERE name = p_parent_type_name LIMIT 1;
  SELECT id INTO v_child_id  FROM schema_node_types WHERE name = p_child_type_name  LIMIT 1;
  IF v_parent_id IS NULL OR v_child_id IS NULL THEN
    RETURN;
  END IF;

  INSERT INTO schema_node_type_compositions (parent_type_id, child_type_id, collection_key)
  VALUES (v_parent_id, v_child_id, p_collection_key)
  ON CONFLICT (parent_type_id, child_type_id)
  DO UPDATE SET collection_key = EXCLUDED.collection_key;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # infer_data_type_name_from_json
 # ------------------------------
FN_INFER_DATA_TYPE_NAME_FROM_JSON_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_infer_data_type_name_from_json(p_value JSONB)
RETURNS TEXT AS $$
DECLARE
  v_t TEXT;
BEGIN
  IF p_value IS NULL OR p_value = 'null'::jsonb THEN
    RETURN 'json';
  END IF;

  v_t := jsonb_typeof(p_value);
  IF v_t = 'string' THEN
    RETURN 'string';
  ELSIF v_t = 'number' THEN
    RETURN 'number';
  ELSIF v_t = 'boolean' THEN
    RETURN 'bool';
  ELSE
    RETURN 'json';
  END IF;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # infer_variant_key
 # ------------------------------
FN_INFER_VARIANT_KEY_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_infer_variant_key(
  p_node_type_name TEXT,
  p_json_key TEXT,
  p_field_type TEXT DEFAULT NULL
)
RETURNS TEXT AS $$
BEGIN
  -- Only fields have variant_keys based on their type
  IF p_node_type_name != 'field' THEN
    RETURN NULL;
  END IF;

  -- Specific json_keys that should use the field type as variant_key
  IF p_json_key IN (
    'accepted_file_types', 'currency', 'custom_mask', 'decimal_digits', 'depends_of',
    'focus_frame_caption', 'focus_frame', 'format', 'height', 'input_mask', 'integer_only',
    'marker_color', 'marker_type', 'max_file_size_mb', 'max_image_size_mb', 'max_items',
    'max_length', 'max_lines', 'max_photos', 'max_video_duration_seconds', 'max_videos',
    'max', 'min_length', 'min_lines', 'min', 'no_label', 'options_url', 'options',
    'pattern', 'picture_source', 'quality', 'style', 'yes_label', 'zoom'
  ) THEN
    RETURN p_field_type;
  END IF;

  RETURN NULL;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # ensure_attribute_def
 # ------------------------------
FN_ENSURE_ATTRIBUTE_DEF_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_ensure_attribute_def(
  p_node_type_name TEXT,
  p_json_key       TEXT,
  p_data_type_name TEXT,
  p_domain_name    TEXT DEFAULT NULL,
  p_variant_key    TEXT DEFAULT NULL
)
RETURNS UUID AS $$
DECLARE
  v_node_type_id UUID;
  v_data_type_id UUID;
  v_domain_id    UUID;
  v_attr_id      UUID;
  v_variant_key_eff TEXT;
  v_is_collection_key BOOLEAN;
BEGIN
  -- Do not create AttributeDefs for json_keys that correspond to collection_keys
  -- These are child nodes defined by NodeTypeComposition, not properties
  SELECT EXISTS (
    SELECT 1 FROM schema_node_type_compositions
    WHERE parent_type_id = (
      SELECT id FROM schema_node_types WHERE name = p_node_type_name LIMIT 1
    )
    AND collection_key = p_json_key
  ) INTO v_is_collection_key;

  IF v_is_collection_key THEN
    RETURN NULL;
  END IF;

  v_node_type_id := s7_ensure_node_type(p_node_type_name, p_node_type_name, TRUE, FALSE, NULL);
  v_data_type_id := s7_ensure_data_type(p_data_type_name, p_data_type_name);
  v_domain_id := NULL;
  IF p_domain_name IS NOT NULL THEN
    v_domain_id := s7_ensure_domain(p_domain_name, p_domain_name);
  END IF;

  -- Normalize variant_key: treat NULL and empty string the same
  v_variant_key_eff := NULLIF(p_variant_key, '');

  -- Check if attribute_def already exists with the given variant_key
  SELECT id INTO v_attr_id
  FROM schema_attribute_defs
  WHERE node_type_id = v_node_type_id
    AND json_key = p_json_key
    AND variant_key IS NOT DISTINCT FROM v_variant_key_eff
  LIMIT 1;

  IF v_attr_id IS NOT NULL THEN
    RETURN v_attr_id;
  END IF;

  INSERT INTO schema_attribute_defs (node_type_id, name, data_type_id, domain_id, json_key, is_required, variant_key)
  VALUES (v_node_type_id, p_json_key, v_data_type_id, v_domain_id, p_json_key, FALSE, v_variant_key_eff);

  SELECT id INTO v_attr_id
  FROM schema_attribute_defs
  WHERE node_type_id = v_node_type_id
    AND json_key = p_json_key
    AND variant_key IS NOT DISTINCT FROM v_variant_key_eff
  LIMIT 1;

  RETURN v_attr_id;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # set_node_attribute_from_json
 # ------------------------------
FN_SET_NODE_ATTRIBUTE_FROM_JSON_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_set_node_attribute_from_json(
  p_node_id         UUID,
  p_node_type_name  TEXT,
  p_json_key        TEXT,
  p_value           jsonb,
  p_domain_name     TEXT DEFAULT NULL,
  p_variant_key     TEXT DEFAULT NULL
)
RETURNS VOID AS $$
DECLARE
  v_attribute_def_id UUID;
  v_type_name        TEXT;
  v_defined_type_name TEXT;
  v_value_jsonb      JSONB;
  v_value_string     TEXT;
BEGIN
  v_value_jsonb := p_value;

  -- Infer type from value for initial attribute_def creation
  v_type_name := s7_infer_data_type_name_from_json(v_value_jsonb);
  v_attribute_def_id := s7_ensure_attribute_def(p_node_type_name, p_json_key, v_type_name, p_domain_name, p_variant_key);

  -- Get the defined data type from attribute_def to respect the schema definition
  SELECT dt.name INTO v_defined_type_name
  FROM schema_attribute_defs ad
  JOIN schema_data_types dt ON dt.id = ad.data_type_id
  WHERE ad.id = v_attribute_def_id;

  -- Use the defined type if available, otherwise fall back to inferred type
  IF v_defined_type_name IS NOT NULL THEN
    v_type_name := v_defined_type_name;
  END IF;

  -- natural_uuid maps to schema_nodes.id (primary key) — always read-only, never stored.
  IF v_type_name = 'natural_uuid' THEN
    RETURN;
  END IF;

  -- natural_key maps to schema_nodes.key column — update it directly, do not store a row.
  IF v_type_name = 'natural_key' THEN
    v_value_string := v_value_jsonb #>> '{}';
    IF v_value_string IS NOT NULL AND v_value_string <> '' THEN
      UPDATE schema_nodes SET key = v_value_string WHERE id = p_node_id;
    END IF;
    RETURN;
  END IF;

  -- natural_version maps to schema_nodes.version column — update it directly, do not store a row.
  -- Also propagates to the root ancestor when called on a metadata child node.
  IF v_type_name = 'natural_version' THEN
    v_value_string := v_value_jsonb #>> '{}';
    IF v_value_string IS NOT NULL AND v_value_string <> '' THEN
      UPDATE schema_nodes SET version = v_value_string WHERE id = p_node_id;
      -- Propagate up to root if this node has a parent with parent_id IS NULL
      UPDATE schema_nodes SET version = v_value_string
      WHERE id = (SELECT parent_id FROM schema_nodes WHERE id = p_node_id)
        AND parent_id IS NULL;
    END IF;
    RETURN;
  END IF;

  -- natural_order maps to schema_nodes.sort_order column — update it directly, do not store a row.
  IF v_type_name = 'natural_order' THEN
    UPDATE schema_nodes SET sort_order = (v_value_jsonb #>> '{}')::int WHERE id = p_node_id;
    RETURN;
  END IF;

  -- display_order maps to schema_nodes.sort_order column — update it directly, do not store a row.
  -- Converts from 1-based (human-readable) to 0-based (database).
  IF v_type_name = 'display_order' THEN
    UPDATE schema_nodes SET sort_order = ((v_value_jsonb #>> '{}')::int - 1) WHERE id = p_node_id;
    RETURN;
  END IF;

  -- Auto-generate UUID for auto_uuid type if value is null or missing
  IF v_type_name = 'auto_uuid' THEN
    IF v_value_jsonb IS NULL OR v_value_jsonb = 'null'::jsonb THEN
      v_value_jsonb := to_jsonb(gen_random_uuid()::text);
    END IF;
    -- Treat auto_uuid as string for storage
    v_type_name := 'string';
  END IF;

  -- Skip NULL, null, empty strings, empty arrays, empty objects, and zero numbers
  IF v_value_jsonb IS NULL OR v_value_jsonb = 'null'::jsonb THEN
    RETURN;
  END IF;

  -- Allow booleans (they are never empty)
  IF jsonb_typeof(v_value_jsonb) = 'boolean' THEN
    -- Boolean values are valid, continue processing
  -- Skip zero numbers (0 has no semantic effect for min/max constraints)
  ELSIF jsonb_typeof(v_value_jsonb) = 'number' THEN
    IF (v_value_jsonb #>> '{}')::numeric = 0 THEN
      RETURN;
    END IF;
  -- Check for empty string
  ELSIF jsonb_typeof(v_value_jsonb) = 'string' THEN
    v_value_string := v_value_jsonb #>> '{}';
    IF v_value_string IS NULL OR v_value_string = '' THEN
      RETURN;
    END IF;
  -- Check for empty array
  ELSIF jsonb_typeof(v_value_jsonb) = 'array' AND jsonb_array_length(v_value_jsonb) = 0 THEN
    RETURN;
  -- Check for empty object
  ELSIF jsonb_typeof(v_value_jsonb) = 'object' AND v_value_jsonb = '{}'::jsonb THEN
    RETURN;
  END IF;

  -- Infer type from value for initial attribute_def creation
  v_type_name := s7_infer_data_type_name_from_json(v_value_jsonb);
  v_attribute_def_id := s7_ensure_attribute_def(p_node_type_name, p_json_key, v_type_name, p_domain_name, p_variant_key);

  -- Get the defined data type from attribute_def to respect the schema definition
  SELECT dt.name INTO v_defined_type_name
  FROM schema_attribute_defs ad
  JOIN schema_data_types dt ON dt.id = ad.data_type_id
  WHERE ad.id = v_attribute_def_id;

  -- Use the defined type if available, otherwise fall back to inferred type
  IF v_defined_type_name IS NOT NULL THEN
    v_type_name := v_defined_type_name;
  END IF;

  -- natural_* types are handled above; guard here in case of direct call after null check
  IF v_type_name LIKE 'natural_%' THEN
    RETURN;
  END IF;

  -- Auto-generate UUID for auto_uuid type if value is null or missing
  IF v_type_name = 'auto_uuid' THEN
    IF v_value_jsonb IS NULL OR v_value_jsonb = 'null'::jsonb THEN
      v_value_jsonb := to_jsonb(gen_random_uuid()::text);
    END IF;
    -- Treat auto_uuid as string for storage
    v_type_name := 'string';
  END IF;

  IF p_domain_name IS NOT NULL AND v_type_name = 'string' THEN
    PERFORM s7_ensure_domain_item(p_domain_name, v_value_jsonb #>> '{}', v_value_jsonb #>> '{}');
  END IF;

  IF v_type_name IN ('string', 'date', 'color') THEN
    INSERT INTO schema_node_attributes (node_id, attribute_def_id, value_string)
    VALUES (p_node_id, v_attribute_def_id, v_value_jsonb #>> '{}')
    ON CONFLICT ON CONSTRAINT uq_snode_attrs_node_attr
    DO UPDATE SET value_string = EXCLUDED.value_string,
                  value_number = NULL,
                  value_bool   = NULL,
                  value_json   = NULL;
  ELSIF v_type_name IN ('number', 'int', 'float') THEN
    INSERT INTO schema_node_attributes (node_id, attribute_def_id, value_number)
    VALUES (p_node_id, v_attribute_def_id, (v_value_jsonb #>> '{}')::numeric)
    ON CONFLICT ON CONSTRAINT uq_snode_attrs_node_attr
    DO UPDATE SET value_string = NULL,
                  value_number = EXCLUDED.value_number,
                  value_bool   = NULL,
                  value_json   = NULL;
  ELSIF v_type_name = 'bool' THEN
    INSERT INTO schema_node_attributes (node_id, attribute_def_id, value_bool)
    VALUES (p_node_id, v_attribute_def_id, (v_value_jsonb #>> '{}')::boolean)
    ON CONFLICT ON CONSTRAINT uq_snode_attrs_node_attr
    DO UPDATE SET value_string = NULL,
                  value_number = NULL,
                  value_bool   = EXCLUDED.value_bool,
                  value_json   = NULL;
  ELSE
    INSERT INTO schema_node_attributes (node_id, attribute_def_id, value_json)
    VALUES (p_node_id, v_attribute_def_id, v_value_jsonb)
    ON CONFLICT ON CONSTRAINT uq_snode_attrs_node_attr
    DO UPDATE SET value_string = NULL,
                  value_number = NULL,
                  value_bool   = NULL,
                  value_json   = EXCLUDED.value_json;
  END IF;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # find_schema_node_id
 # ------------------------------
FN_FIND_SCHEMA_NODE_ID_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_find_schema_node_id(
  p_key TEXT,
  p_version TEXT
)
RETURNS UUID AS $$
DECLARE
  v_root_id UUID;
BEGIN
  SELECT n.id
  INTO v_root_id
  FROM schema_nodes n
  JOIN schema_node_types nt ON nt.id = n.node_type_id AND nt.is_root = TRUE
  WHERE n.key = p_key
    AND n.version = p_version
    AND n.parent_id IS NULL
  LIMIT 1;

  RETURN v_root_id;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # ensure_schema_build_state
 # ------------------------------
FN_ENSURE_SCHEMA_BUILD_STATE_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_ensure_schema_build_state(
  p_key TEXT,
  p_version TEXT,
  p_project_id UUID
)
RETURNS VOID AS $$
BEGIN
  INSERT INTO schema_build_state (key, version, current_build, last_cached_build, dirty, project_id)
  VALUES (p_key, p_version, 1, NULL, FALSE, p_project_id)
  ON CONFLICT (key, version, project_id) DO NOTHING;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


# ------------------------------
# schema_build_state updated_at defaults + trigger
# ------------------------------
SCHEMA_BUILD_STATE_UPDATED_AT_DEFAULT_SQL = r"""
SET search_path TO s7, public;

ALTER TABLE schema_build_state
  ALTER COLUMN updated_at SET DEFAULT NOW();
"""


FN_SET_UPDATED_AT_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_set_updated_at_now()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


TRG_SCHEMA_BUILD_STATE_SET_UPDATED_AT_SQL = r"""
SET search_path TO s7, public;

DROP TRIGGER IF EXISTS trg_schema_build_state_set_updated_at ON schema_build_state;
CREATE TRIGGER trg_schema_build_state_set_updated_at
BEFORE UPDATE ON schema_build_state
FOR EACH ROW
EXECUTE FUNCTION s7_set_updated_at_now();
"""


 # ------------------------------
 # mark_schema_dirty
 # ------------------------------
FN_MARK_SCHEMA_DIRTY_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_mark_schema_dirty(
  p_key TEXT,
  p_version TEXT,
  p_project_id UUID
)
RETURNS VOID AS $$
BEGIN
  PERFORM s7_ensure_schema_build_state(p_key, p_version, p_project_id);

  -- Mark as dirty instead of incrementing counter
  UPDATE schema_build_state
  SET dirty = TRUE,
      updated_at = NOW()
  WHERE key = p_key
    AND version  = p_version
    AND project_id = p_project_id;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # increment_build
 # ------------------------------
FN_INCREMENT_BUILD_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_increment_build(
  p_key TEXT,
  p_version TEXT,
  p_project_id UUID
)
RETURNS VOID AS $$
BEGIN
  PERFORM s7_ensure_schema_build_state(p_key, p_version, p_project_id);

  -- Increment build counter and mark as clean without rebuilding cache
  UPDATE schema_build_state
  SET current_build = current_build + 1,
      dirty = FALSE,
      updated_at = NOW()
  WHERE key = p_key
    AND version  = p_version
    AND project_id = p_project_id;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # get_schema_key_version
 # ------------------------------
FN_GET_SCHEMA_KEY_VERSION_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_get_schema_key_version(
  p_node_id UUID
)
RETURNS TABLE(key TEXT, version TEXT) AS $$
DECLARE
  v_root_id UUID;
  v_current_id UUID;
BEGIN
  v_current_id := p_node_id;

  -- Traverse up the parent hierarchy to find the root node
  WHILE v_current_id IS NOT NULL LOOP
    SELECT n.id
    INTO v_root_id
    FROM schema_nodes n
    JOIN schema_node_types nt ON nt.id = n.node_type_id AND nt.is_root = TRUE
    WHERE n.id = v_current_id
      AND n.parent_id IS NULL
    LIMIT 1;

    IF v_root_id IS NOT NULL THEN
      EXIT;
    END IF;

    -- Move to parent
    SELECT parent_id
    INTO v_current_id
    FROM schema_nodes
    WHERE id = v_current_id;
  END LOOP;

  IF v_root_id IS NULL THEN
    RETURN;
  END IF;

  -- Read key and version directly from the native columns
  SELECT n.key, n.version
  INTO key, version
  FROM schema_nodes n
  WHERE n.id = v_root_id;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # mark_schema_dirty_by_node
 # ------------------------------
FN_MARK_SCHEMA_DIRTY_BY_NODE_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_mark_schema_dirty_by_node(
  p_node_id UUID
)
RETURNS VOID AS $$
DECLARE
  v_key TEXT;
  v_version  TEXT;
  v_project_id UUID;
  v_root_id UUID;
  v_current_id UUID;
BEGIN
  v_current_id := p_node_id;
  
  -- Traverse up to find the root node with key+version natively
  WHILE v_current_id IS NOT NULL LOOP
    SELECT n.id, n.key, n.version, n.project_id
    INTO v_root_id, v_key, v_version, v_project_id
    FROM schema_nodes n
    JOIN schema_node_types nt ON nt.id = n.node_type_id AND nt.is_root = TRUE
    WHERE n.id = v_current_id
      AND n.parent_id IS NULL
      AND n.key IS NOT NULL
      AND n.version IS NOT NULL;
    
    IF v_root_id IS NOT NULL THEN
      EXIT;
    END IF;
    
    -- Move to parent
    SELECT parent_id
    INTO v_current_id
    FROM schema_nodes
    WHERE id = v_current_id;
  END LOOP;

  -- If we have both key and version, mark as dirty with project_id
  IF v_key IS NOT NULL AND v_version IS NOT NULL AND v_project_id IS NOT NULL THEN
    PERFORM s7_mark_schema_dirty(v_key, v_version, v_project_id);
  END IF;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # trg_mark_schema_dirty_nodes
 # ------------------------------
FN_TRG_MARK_SCHEMA_DIRTY_NODES_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_trg_mark_schema_dirty_nodes()
RETURNS TRIGGER AS $$
BEGIN
  IF pg_trigger_depth() > 1 THEN
    RETURN COALESCE(NEW, OLD);
  END IF;

  PERFORM s7_mark_schema_dirty_by_node(COALESCE(NEW.id, OLD.parent_id, OLD.id));
  RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # trg_mark_schema_dirty_node_attributes
 # ------------------------------
FN_TRG_MARK_SCHEMA_DIRTY_NODE_ATTRIBUTES_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_trg_mark_schema_dirty_node_attributes()
RETURNS TRIGGER AS $$
BEGIN
  IF pg_trigger_depth() > 1 THEN
    RETURN COALESCE(NEW, OLD);
  END IF;

  PERFORM s7_mark_schema_dirty_by_node(COALESCE(NEW.node_id, OLD.node_id));
  RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # build_schema_cached
 # ------------------------------
FN_BUILD_SCHEMA_CACHED_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_build_schema_cached(
  p_key TEXT,
  p_version TEXT,
  p_schema_type TEXT DEFAULT NULL
)
RETURNS JSONB AS $$
DECLARE
  v_root_id       UUID;
  v_project_id    UUID;
  v_current_build BIGINT;
  v_last_cached   BIGINT;
  v_json_cache    JSONB;
  v_json_fresh    JSONB;
  v_json_scope    TEXT;
  v_schema_type   TEXT;
BEGIN
  -- Find root node directly by native key+version columns
  SELECT f.id, f.project_id, nt.json_scope
  INTO v_root_id, v_project_id, v_json_scope
  FROM schema_nodes f
  JOIN schema_node_types nt ON nt.id = f.node_type_id AND nt.is_root = TRUE
  WHERE f.key = p_key
    AND f.version = p_version
    AND f.parent_id IS NULL
  LIMIT 1;

  IF v_root_id IS NULL THEN
    RAISE EXCEPTION 'Schema not found for key=%, version=%', p_key, p_version;
  END IF;

  -- Derive schema_type from actual json_scope if not explicitly provided
  v_schema_type := COALESCE(p_schema_type, REPLACE(v_json_scope, '_root', ''));

  PERFORM s7_ensure_schema_build_state(p_key, p_version, v_project_id);

  SELECT current_build, last_cached_build
  INTO v_current_build, v_last_cached
  FROM schema_build_state
  WHERE key = p_key
    AND version  = p_version
    AND project_id = v_project_id
  LIMIT 1;

  SELECT schema_json
  INTO v_json_cache
  FROM schema_cache
  WHERE key = p_key
    AND version  = p_version
    AND project_id = v_project_id
    AND (schema_type = v_schema_type OR schema_type IS NULL)
  LIMIT 1;

  IF v_json_cache IS NOT NULL AND v_last_cached IS NOT DISTINCT FROM v_current_build THEN
    RETURN v_json_cache;
  END IF;

  v_json_fresh := jsonb_build_object(v_schema_type, s7_build_node_json(v_root_id));

  INSERT INTO schema_cache (key, version, schema_json, generated_at, schema_type, project_id)
  VALUES (p_key, p_version, v_json_fresh, NOW(), v_schema_type, v_project_id)
  ON CONFLICT (key, version, project_id)
  DO UPDATE SET
    schema_json  = EXCLUDED.schema_json,
    generated_at = NOW(),
    schema_type = EXCLUDED.schema_type;

  -- Increment build counter and mark as clean
  UPDATE schema_build_state
  SET current_build = current_build + 1,
      last_cached_build = current_build + 1,
      dirty = FALSE,
      cached_at = NOW()
  WHERE key = p_key
    AND version  = p_version
    AND project_id = v_project_id;

  RETURN v_json_fresh;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # publish_schema
 # ------------------------------
FN_PUBLISH_SCHEMA_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_publish_schema(p_key TEXT, p_version TEXT)
RETURNS VOID AS $$
DECLARE
  v_root_id UUID;
  v_status_def_id UUID;
  v_root_type_id UUID;
  v_json_scope TEXT;
BEGIN
  -- Find the root node directly via native key+version columns
  SELECT f.id, f.node_type_id, nt.json_scope
  INTO v_root_id, v_root_type_id, v_json_scope
  FROM schema_nodes AS f
  JOIN schema_node_types AS nt ON nt.id = f.node_type_id AND nt.is_root = TRUE
  WHERE f.key = p_key
    AND f.version = p_version
    AND f.parent_id IS NULL
  LIMIT 1;

  IF v_root_id IS NULL THEN
    RAISE EXCEPTION 'Schema not found for key=% and version=%', p_key, p_version;
  END IF;

  SELECT id
  INTO v_status_def_id
  FROM schema_attribute_defs
  WHERE node_type_id = v_root_type_id
    AND json_key = 'status'
  LIMIT 1;

  IF v_status_def_id IS NULL THEN
    RAISE EXCEPTION 'Attribute definition for json_key="status" not found';
  END IF;

  UPDATE schema_node_attributes AS na
  SET value_string = 'archived'
  FROM schema_nodes AS f
  JOIN schema_node_types AS nt ON nt.id = f.node_type_id AND nt.id = v_root_type_id
  WHERE na.node_id = f.id
    AND na.attribute_def_id = v_status_def_id
    AND f.key = p_key
    AND f.id <> v_root_id
    AND f.parent_id IS NULL
    AND na.value_string = 'published';

  INSERT INTO schema_node_attributes (node_id, attribute_def_id, value_string)
  VALUES (v_root_id, v_status_def_id, 'published')
  ON CONFLICT ON CONSTRAINT uq_snode_attrs_node_attr
  DO UPDATE SET value_string = EXCLUDED.value_string;

  -- Build cache — schema_type derived automatically from json_scope
  PERFORM s7_build_schema_cached(p_key, p_version);
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # import_schema (text)
 # ------------------------------
FN_IMPORT_SCHEMA_TEXT_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_import_schema(
  p_schema_text TEXT,
  p_key   TEXT DEFAULT NULL,
  p_version    TEXT DEFAULT NULL,
  p_status     TEXT DEFAULT NULL,
  p_overwrite  BOOLEAN DEFAULT FALSE
)
RETURNS UUID AS $$
DECLARE
  v_clean TEXT;
  v_schema JSONB;
BEGIN
  IF p_schema_text IS NULL OR btrim(p_schema_text) = '' THEN
    RAISE EXCEPTION 'schema is required';
  END IF;

  v_clean := regexp_replace(p_schema_text, ',\s*([\]}])', '\1', 'g');
  v_schema := v_clean::jsonb;

  RETURN s7_import_schema(v_schema, p_key, p_version, p_status, p_overwrite);
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


 # ------------------------------
 # import_schema (json)
 # ------------------------------
FN_IMPORT_SCHEMA_JSON_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_import_schema(
  p_schema     JSONB,
  p_key        TEXT DEFAULT NULL,
  p_version    TEXT DEFAULT NULL,
  p_status     TEXT DEFAULT NULL,
  p_overwrite  BOOLEAN DEFAULT FALSE,
  p_project_id UUID DEFAULT NULL,
  p_organization_id UUID DEFAULT NULL
)
RETURNS UUID AS $$
DECLARE
  v_root_type_id UUID;
  v_existing_root_id UUID;
  v_root_id UUID;
  v_key_eff TEXT;
  v_version_eff TEXT;
  v_status_eff TEXT;
  v_root_key TEXT;
BEGIN
  RAISE NOTICE 's7_import_schema called with p_schema: %, p_key: %, p_version: %, p_status: %, p_project_id: %, p_organization_id: %', 
    p_schema, p_key, p_version, p_status, p_project_id, p_organization_id;
  
  IF p_schema IS NULL OR p_schema = 'null'::jsonb THEN
    RAISE EXCEPTION 'schema is required';
  END IF;

  v_key_eff := COALESCE(p_key, p_schema->>'key');
  IF v_key_eff IS NULL OR v_key_eff = '' THEN
    v_key_eff := 'schema_' || substr(replace(gen_random_uuid()::text,'-',''), 1, 12);
  END IF;

  v_version_eff := COALESCE(p_version, p_schema->>'version');
  IF v_version_eff IS NULL OR v_version_eff = '' THEN
    v_version_eff := '1';
  END IF;

  v_status_eff := COALESCE(p_status, p_schema->>'status');
  IF v_status_eff IS NULL OR v_status_eff = '' THEN
    v_status_eff := 'draft';
  END IF;

  -- Detect root node type from JSON root key
  -- The JSON should have a root key that maps to a NodeType (name or json_scope)
  -- Example: {"schema_type": {...}} where "schema_type" matches NodeType.name or NodeType.json_scope
  SELECT jsonb_object_keys(p_schema) INTO v_root_key;
  IF v_root_key IS NULL THEN
    RAISE EXCEPTION 'Schema JSON must have a root key that maps to a NodeType';
  END IF;
  
  -- Get the root node type by matching the JSON root key to node_type name or json_scope
  -- The JSON root key is NOT the schema type itself, but a key that identifies which NodeType to use
  SELECT id INTO v_root_type_id FROM schema_node_types 
  WHERE (name = v_root_key OR json_scope = v_root_key) AND is_root = true;
  IF v_root_type_id IS NULL THEN
    RAISE EXCEPTION 'Root node type not found for JSON root key: %', v_root_key;
  END IF;

  -- Check for existing schema with matching key/version OR key=NULL (edge case from manual creation)
  v_existing_root_id := s7_find_schema_node_id(v_key_eff, v_version_eff);
  
  -- Also check for nodes with key=NULL of the same node_type and project
  IF v_existing_root_id IS NULL THEN
    SELECT id INTO v_existing_root_id FROM schema_nodes
    WHERE key IS NULL
      AND version = v_version_eff
      AND node_type_id = v_root_type_id
      AND project_id = p_project_id
      AND parent_id IS NULL
    LIMIT 1;
  END IF;
  
  IF v_existing_root_id IS NOT NULL THEN
    IF NOT p_overwrite THEN
      RAISE EXCEPTION 'Schema already exists for key=% version=%', v_key_eff, v_version_eff;
    END IF;
    
    -- Delete all nodes in the schema tree (bottom-up to avoid FK violations)
    -- Use recursive CTE to find ALL root nodes with matching key/version and their descendants
    -- This handles the case where multiple root nodes exist with the same key/version
    
    -- First delete attributes (they reference nodes)
    WITH RECURSIVE tree_nodes AS (
      -- Base case: ALL root nodes with matching key and version
      -- Also include nodes with key=NULL of the same node_type and project (edge case from manual creation)
      SELECT id, parent_id, 0 AS depth
      FROM schema_nodes
      WHERE (key = v_key_eff OR (key IS NULL AND node_type_id = v_root_type_id AND project_id = p_project_id))
        AND version = v_version_eff 
        AND parent_id IS NULL
      
      UNION ALL
      
      -- Recursive case: all children of all matched root nodes
      SELECT n.id, n.parent_id, tn.depth + 1
      FROM schema_nodes n
      INNER JOIN tree_nodes tn ON n.parent_id = tn.id
    )
    DELETE FROM schema_node_attributes
    WHERE node_id IN (SELECT id FROM tree_nodes);
    
    -- Then delete nodes (bottom-up order)
    WITH RECURSIVE tree_nodes AS (
      -- Base case: ALL root nodes with matching key and version
      -- Also include nodes with key=NULL of the same node_type and project (edge case from manual creation)
      SELECT id, parent_id, 0 AS depth
      FROM schema_nodes
      WHERE (key = v_key_eff OR (key IS NULL AND node_type_id = v_root_type_id AND project_id = p_project_id))
        AND version = v_version_eff 
        AND parent_id IS NULL
      
      UNION ALL
      
      -- Recursive case: all children of all matched root nodes
      SELECT n.id, n.parent_id, tn.depth + 1
      FROM schema_nodes n
      INNER JOIN tree_nodes tn ON n.parent_id = tn.id
    )
    DELETE FROM schema_nodes
    WHERE id IN (
      SELECT id FROM tree_nodes
      ORDER BY depth DESC
    );
  END IF;

  -- Create root node with project_id and organization_id
  INSERT INTO schema_nodes (node_type_id, parent_id, sort_order, key, name, version, project_id, organization_id)
  VALUES (v_root_type_id, NULL, 1, v_key_eff, v_key_eff, v_version_eff, p_project_id, p_organization_id)
  RETURNING id INTO v_root_id;

  -- Note: The schema structure should be built by domain-specific import functions
  -- This function only creates the root node with basic metadata
  RAISE NOTICE 'Schema root node created with id=%, node_type_id=%, root_key=%, project_id=%, organization_id=%', 
    v_root_id, v_root_type_id, v_root_key, p_project_id, p_organization_id;

  RETURN v_root_id;
EXCEPTION
  WHEN OTHERS THEN
    RAISE NOTICE 'Error in s7_import_schema: SQLSTATE=%, SQLERRM=%', SQLSTATE, SQLERRM;
    RAISE EXCEPTION 'Schema import failed: % (SQLSTATE: %)', SQLERRM, SQLSTATE;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


# ------------------------------
# Trigger to prevent cycles
# ------------------------------
TRG_PREVENT_CYCLES_SQL = r"""
SET search_path TO s7, public;

DROP TRIGGER IF EXISTS trg_prevent_cycles ON schema_nodes;
CREATE TRIGGER trg_prevent_cycles
AFTER INSERT OR UPDATE OF parent_id ON schema_nodes
FOR EACH ROW EXECUTE FUNCTION s7_prevent_node_cycles();
"""


# ------------------------------
# Trigger to validate value type
# ------------------------------
TRG_VALIDATE_VALUE_TYPE_SQL = r"""
SET search_path TO s7, public;

DROP TRIGGER IF EXISTS trg_validate_value_type ON schema_node_attributes;
-- Active trigger: validates that value_* column matches data_type
CREATE TRIGGER trg_validate_value_type
BEFORE INSERT OR UPDATE ON schema_node_attributes
FOR EACH ROW EXECUTE FUNCTION s7_validate_value_type();
"""

# ------------------------------
# Trigger to mark schema dirty when nodes change
# ------------------------------
TRG_MARK_SCHEMA_DIRTY_NODES_SQL = r"""
SET search_path TO s7, public;

DROP TRIGGER IF EXISTS trg_s7_mark_schema_dirty_nodes ON schema_nodes;
CREATE TRIGGER trg_s7_mark_schema_dirty_nodes
AFTER INSERT OR UPDATE OR DELETE ON schema_nodes
FOR EACH ROW EXECUTE FUNCTION s7_trg_mark_schema_dirty_nodes();
"""

# ------------------------------
# Trigger to mark schema dirty when schema_node_attributes change
# ------------------------------
TRG_MARK_SCHEMA_DIRTY_NODE_ATTRIBUTES_SQL = r"""
SET search_path TO s7, public;

DROP TRIGGER IF EXISTS trg_s7_mark_schema_dirty_node_attributes ON schema_node_attributes;
CREATE TRIGGER trg_s7_mark_schema_dirty_node_attributes
AFTER INSERT OR UPDATE OR DELETE ON schema_node_attributes
FOR EACH ROW EXECUTE FUNCTION s7_trg_mark_schema_dirty_node_attributes();
"""


# ------------------------------
# get_node_tree
# ------------------------------
FN_GET_NODE_TREE_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_get_node_tree(p_root_id UUID)
RETURNS TABLE (
  node_id UUID,
  parent_id UUID,
  sort_order INT,
  name TEXT,
  node_type_name TEXT
) AS $$
BEGIN
  RETURN QUERY
  WITH RECURSIVE t AS (
    SELECT n.id, n.parent_id, n.sort_order, n.name, n.node_type_id
    FROM schema_nodes n
    WHERE n.id = p_root_id
    UNION ALL
    SELECT n.id, n.parent_id, n.sort_order, n.name, n.node_type_id
    FROM schema_nodes n
    JOIN t ON n.parent_id = t.id
  )
  SELECT t.id, t.parent_id, t.sort_order, t.name::text, nt.name::text
  FROM t
  JOIN schema_node_types nt ON nt.id = t.node_type_id
  ORDER BY t.parent_id NULLS FIRST, t.sort_order;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


# ------------------------------
# delete_node_tree
# ------------------------------
FN_DELETE_NODE_TREE_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_delete_node_tree(p_node_id UUID)
RETURNS VOID AS $$
BEGIN
  WITH RECURSIVE t AS (
    SELECT id
    FROM schema_nodes
    WHERE id = p_node_id
    UNION ALL
    SELECT n.id
    FROM schema_nodes n
    JOIN t ON n.parent_id = t.id
  )
  DELETE FROM schema_node_attributes
  WHERE node_id IN (SELECT id FROM t);
  
  WITH RECURSIVE t AS (
    SELECT id
    FROM schema_nodes
    WHERE id = p_node_id
    UNION ALL
    SELECT n.id
    FROM schema_nodes n
    JOIN t ON n.parent_id = t.id
  )
  DELETE FROM schema_nodes
  WHERE id IN (SELECT id FROM t);
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


# ------------------------------
# check_key_version_unique
# ------------------------------
FN_CHECK_KEY_VERSION_UNIQUE_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_check_key_version_unique(
  p_key TEXT,
  p_version TEXT,
  p_exclude_node_id UUID DEFAULT NULL
)
RETURNS BOOLEAN AS $$
DECLARE
  v_count INT;
BEGIN
  IF p_exclude_node_id IS NOT NULL THEN
    SELECT COUNT(*)
    INTO v_count
    FROM schema_nodes f
    JOIN schema_node_types nt ON nt.id = f.node_type_id AND nt.is_root = TRUE
    WHERE f.key = p_key
      AND f.version = p_version
      AND f.parent_id IS NULL
      AND f.id != p_exclude_node_id;
  ELSE
    SELECT COUNT(*)
    INTO v_count
    FROM schema_nodes f
    JOIN schema_node_types nt ON nt.id = f.node_type_id AND nt.is_root = TRUE
    WHERE f.key = p_key
      AND f.version = p_version
      AND f.parent_id IS NULL;
  END IF;
  
  RETURN v_count = 0;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


# ------------------------------
# check_key_version_exists
# ------------------------------
FN_CHECK_KEY_VERSION_EXISTS_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_check_key_version_exists(
  p_key TEXT,
  p_version TEXT
)
RETURNS BOOLEAN AS $$
BEGIN
  RETURN EXISTS (
    SELECT 1
    FROM schema_nodes f
    JOIN schema_node_types nt ON nt.id = f.node_type_id AND nt.is_root = TRUE
    WHERE f.key = p_key
      AND f.version = p_version
      AND f.parent_id IS NULL
  );
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""


# ------------------------------
# Procedures
# ------------------------------
# PROCEDURES_SQL = r"""
# SET search_path TO s7, public;
# -- No procedures defined in the source schema.
# """


class Migration(migrations.Migration):
    dependencies = [
        ("schemas", "0001_s7_structure"),
    ]

    operations = [
        # Functions
        migrations.RunSQL(FN_PREVENT_NODE_CYCLES_SQL),
        migrations.RunSQL(FN_VALIDATE_VALUE_TYPE_SQL),
        migrations.RunSQL(FN_ATTRIBUTE_VALUE_TO_JSONB_SQL),
        migrations.RunSQL(FN_BUILD_NODE_CONDITIONS_SQL),
        migrations.RunSQL(FN_STRIP_EMPTY_VALUES_SQL),
        migrations.RunSQL(FN_BUILD_NODE_JSON_SQL),
        migrations.RunSQL(FN_BUILD_SCHEMA_SQL),
        migrations.RunSQL(FN_BUILD_SCHEMA_TEXT_SQL),
        migrations.RunSQL(FN_ENSURE_DATA_TYPE_SQL),
        migrations.RunSQL(FN_ENSURE_DOMAIN_SQL),
        migrations.RunSQL(FN_ENSURE_DOMAIN_ITEM_SQL),
        migrations.RunSQL(FN_ENSURE_NODE_TYPE_SQL),
        migrations.RunSQL(FN_ENSURE_NODE_TYPE_COMPOSITION_SQL),
        migrations.RunSQL(FN_INFER_DATA_TYPE_NAME_FROM_JSON_SQL),
        migrations.RunSQL(FN_ENSURE_ATTRIBUTE_DEF_SQL),
        migrations.RunSQL(FN_SET_NODE_ATTRIBUTE_FROM_JSON_SQL),
        migrations.RunSQL(FN_FIND_SCHEMA_NODE_ID_SQL),
        migrations.RunSQL(SCHEMA_BUILD_STATE_UPDATED_AT_DEFAULT_SQL),
        migrations.RunSQL(FN_ENSURE_SCHEMA_BUILD_STATE_SQL),
        migrations.RunSQL(FN_SET_UPDATED_AT_SQL),
        migrations.RunSQL(TRG_SCHEMA_BUILD_STATE_SET_UPDATED_AT_SQL),
        migrations.RunSQL(FN_MARK_SCHEMA_DIRTY_SQL),
        migrations.RunSQL(FN_INCREMENT_BUILD_SQL),
        migrations.RunSQL(FN_GET_SCHEMA_KEY_VERSION_SQL),
        migrations.RunSQL(FN_MARK_SCHEMA_DIRTY_BY_NODE_SQL),
        migrations.RunSQL(FN_TRG_MARK_SCHEMA_DIRTY_NODES_SQL),
        migrations.RunSQL(FN_GET_NODE_TREE_SQL),
        migrations.RunSQL(FN_DELETE_NODE_TREE_SQL),
        migrations.RunSQL(FN_CHECK_KEY_VERSION_UNIQUE_SQL),
        migrations.RunSQL(FN_CHECK_KEY_VERSION_EXISTS_SQL),
        migrations.RunSQL(FN_TRG_MARK_SCHEMA_DIRTY_NODE_ATTRIBUTES_SQL),
        migrations.RunSQL(FN_BUILD_SCHEMA_CACHED_SQL),
        migrations.RunSQL(FN_PUBLISH_SCHEMA_SQL),
        migrations.RunSQL(FN_IMPORT_SCHEMA_TEXT_SQL),
        migrations.RunSQL(FN_IMPORT_SCHEMA_JSON_SQL),

        # Triggers
        migrations.RunSQL(TRG_PREVENT_CYCLES_SQL),
        migrations.RunSQL(TRG_VALIDATE_VALUE_TYPE_SQL),
        migrations.RunSQL(TRG_MARK_SCHEMA_DIRTY_NODES_SQL),
        migrations.RunSQL(TRG_MARK_SCHEMA_DIRTY_NODE_ATTRIBUTES_SQL),
        
        # Procedures
        # migrations.RunSQL(PROCEDURES_SQL),
    ]
