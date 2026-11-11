import uuid

from django.db import migrations


# MCP feature — declared node `name` re-emission channel (feature tag: mcp).
# feature: mcp
#
# Adds the `natural_name` DataType to the base catalog and redefines
# s7_build_node_json so catalogs can opt in to re-emitting the node's
# display name (schema_nodes.name) in the built JSON.
#
# The import write path already lands on the column: `name` is an
# author-settable node field, so a payload key named `name` maps to
# schema_nodes.name at import time. The missing half was the read channel:
# the v_natural_attrs aggregate only derived natural_uuid / natural_key /
# natural_version / natural_order / display_order from schema_nodes
# columns. A new CASE arm maps natural_name to n.name, so an AttributeDef
# declared with the natural_name datatype emits "name": <node.name> on
# rebuild. Emission stays opt-in per catalog — node types without such a
# def produce byte-identical output (the datatype filter already accepts
# the row via the 'natural_%' LIKE, and no arm fires without a declared
# def either way). The v_attrs SELECT likewise already excludes stored
# natural_name rows through the same 'natural_%' filter, so nothing else
# in the routine needs to change.
#
# Datatype seed: fresh installs receive the row through 0004's loaddata of
# the updated fixtures/base_datatypes.json; databases that applied 0004
# before the row existed get it here via get_or_create by natural key
# (name), idempotent on both paths. The pk is the deterministic uuid5 the
# fixture regeneration emits (uuid5(SEED_NS, 'datatype:natural_name')) so a
# later loaddata of the updated fixture updates this row in place instead
# of conflicting on the unique name.
#
# The forward NULLs last_cached_build in schema_build_state for the same
# reason as the previous redefinition: the cache-serving predicate
# (last_cached_build IS NOT DISTINCT FROM current_build) would otherwise
# keep serving schema_json rows built by the previous routine body. The
# reverse restores the exact pre-migration routine body and invalidates
# the same way, since dropping the arm also changes emitted output.


NATURAL_NAME_DATA_TYPE_PK = uuid.UUID('35776621-b460-562a-ad9f-b471ec9705e0')


FN_BUILD_NODE_JSON_SQL = r"""
SET search_path TO s7, public;

CREATE OR REPLACE FUNCTION s7_build_node_json(p_node_id UUID)
RETURNS JSONB AS $$
DECLARE
  v_node_key          TEXT;
  v_node_id_text      TEXT;
  v_json_scope        TEXT;
  v_attrs             JSONB;
  v_natural_attrs     JSONB;
  v_children_json     JSONB;
  v_keyed_children    JSONB;
  v_map_children      JSONB;
  v_result            JSONB;
  v_node_type_name    TEXT;
  v_node_sort_order   INT;
  v_effective_variant TEXT;
  v_disc              TEXT;
  v_node_type_id      UUID;
  v_parent_id         UUID;
BEGIN
  SELECT nt.json_scope, nt.name, n.key, n.id::text, n.sort_order, n.node_type_id, n.parent_id
  INTO v_json_scope, v_node_type_name, v_node_key, v_node_id_text, v_node_sort_order, v_node_type_id, v_parent_id
  FROM s7.schema_nodes n
  JOIN s7.schema_node_types nt ON nt.id = n.node_type_id
  WHERE n.id = p_node_id;

  IF v_node_id_text IS NULL THEN
    RAISE EXCEPTION 'Node not found for id=%', p_node_id;
  END IF;

  -- Collect stored attributes (excluding natural_*, internal and
  -- display_order types). display_order never stores a value row: any
  -- stored row is dead data — the emitted value is derived from
  -- sort_order in the v_natural_attrs block below.
  -- Integral value_number emits as a JSON integer (the wire literal the
  -- payload carried) instead of the scaled numeric form.
  SELECT
    jsonb_object_agg(ad.json_key,
                     CASE dt.name
                       WHEN 'int' THEN to_jsonb(TRUNC(na.value_number)::bigint)
                       ELSE COALESCE(
                         CASE
                           WHEN na.value_number = TRUNC(na.value_number)
                           THEN to_jsonb(TRUNC(na.value_number))
                           ELSE na.value_number::text::jsonb
                         END,
                         na.value_bool::text::jsonb, na.value_json, to_jsonb(na.value_string))
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

  -- Resolve the node's effective variant, mirroring the Python-side
  -- resolution in SchemaService._process_node_attributes:
  --   * the node type's NodeTypeVariant declares discriminator_attr ->
  --     the variant is the stored value of that attribute on this node;
  --   * the node type's NodeTypeVariant rows carry discriminator_attr
  --     NULL (a props node type) -> the variant is inherited from the
  --     parent node, one hop over schema_nodes.parent_id;
  --   * no NodeTypeVariant rows -> no variant (NULL).
  v_effective_variant := NULL;
  SELECT ntv.discriminator_attr INTO v_disc
  FROM s7.schema_node_type_variants ntv
  WHERE ntv.node_type_id = v_node_type_id
    AND ntv.discriminator_attr IS NOT NULL
  LIMIT 1;

  IF v_disc IS NOT NULL THEN
    SELECT na.value_string INTO v_effective_variant
    FROM s7.schema_node_attributes na
    JOIN s7.schema_attribute_defs ad2 ON ad2.id = na.attribute_def_id
    WHERE na.node_id = p_node_id
      AND ad2.node_type_id = v_node_type_id
      AND ad2.json_key = v_disc
      AND ad2.variant_key IS NULL
    LIMIT 1;
  ELSIF EXISTS (
    SELECT 1 FROM s7.schema_node_type_variants ntv
    WHERE ntv.node_type_id = v_node_type_id
      AND ntv.discriminator_attr IS NULL
  ) AND v_parent_id IS NOT NULL THEN
    -- Props node: the effective variant is inherited from the parent
    -- (one hop — the parent of a props node is a normal node with its
    -- own NTV). The LEFT JOIN keeps v_effective_variant NULL when the
    -- parent stores no discriminator value row.
    SELECT pna.value_string INTO v_effective_variant
    FROM s7.schema_nodes parent
    JOIN s7.schema_node_type_variants pntv
      ON pntv.node_type_id = parent.node_type_id
     AND pntv.discriminator_attr IS NOT NULL
    JOIN s7.schema_attribute_defs pad2
      ON pad2.node_type_id = parent.node_type_id
     AND pad2.json_key = pntv.discriminator_attr
     AND pad2.variant_key IS NULL
    LEFT JOIN s7.schema_node_attributes pna
      ON pna.node_id = parent.id
     AND pna.attribute_def_id = pad2.id
    WHERE parent.id = v_parent_id
    LIMIT 1;
  END IF;

  -- Build natural field overrides from schema_nodes columns, driven by AttributeDef definitions.
  -- Only include natural fields that are explicitly defined as AttributeDefs for this node's type.
  -- The variant filter mirrors SchemaService._process_node_attributes: a def applies when its
  -- variant_key IS NULL or matches the node's effective variant resolved above.
  -- Deterministic dedup: a json_key declared both as a base def
  -- (variant_key NULL) and as a def scoped to the effective variant
  -- enters the aggregate twice. The aggregate's own ORDER BY (the
  -- documented-guaranteed ordering) makes the exact-variant row win —
  -- jsonb_object_agg keeps the last value per key; ad.id keeps the order
  -- total among duplicate same-scope defs.
  SELECT jsonb_object_agg(json_key, derived_value ORDER BY is_base_variant DESC, ad_id)
  INTO v_natural_attrs
  FROM (
    SELECT
      ad.json_key,
      (ad.variant_key IS NULL) AS is_base_variant,
      ad.id AS ad_id,
      CASE dt.name
        WHEN 'natural_uuid'    THEN to_jsonb(p_node_id::text)
        WHEN 'natural_key'     THEN to_jsonb(v_node_key)
        WHEN 'natural_name'    THEN to_jsonb(n.name)
        WHEN 'natural_version' THEN to_jsonb(n.version)
        WHEN 'natural_order'   THEN to_jsonb(v_node_sort_order)
        WHEN 'display_order'   THEN to_jsonb(v_node_sort_order + 1)
      END AS derived_value
    FROM s7.schema_attribute_defs ad
    JOIN s7.schema_data_types dt ON dt.id = ad.data_type_id
    JOIN s7.schema_nodes n ON n.id = p_node_id
    WHERE ad.node_type_id = n.node_type_id
      AND (dt.name LIKE 'natural_%' OR dt.name = 'display_order')
      AND (ad.variant_key IS NULL OR ad.variant_key = v_effective_variant)
  ) AS natural_defs;

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
  v_node_key          TEXT;
  v_node_id_text      TEXT;
  v_json_scope        TEXT;
  v_attrs             JSONB;
  v_natural_attrs     JSONB;
  v_children_json     JSONB;
  v_keyed_children    JSONB;
  v_map_children      JSONB;
  v_result            JSONB;
  v_node_type_name    TEXT;
  v_node_sort_order   INT;
  v_effective_variant TEXT;
  v_disc              TEXT;
  v_node_type_id      UUID;
  v_parent_id         UUID;
BEGIN
  SELECT nt.json_scope, nt.name, n.key, n.id::text, n.sort_order, n.node_type_id, n.parent_id
  INTO v_json_scope, v_node_type_name, v_node_key, v_node_id_text, v_node_sort_order, v_node_type_id, v_parent_id
  FROM s7.schema_nodes n
  JOIN s7.schema_node_types nt ON nt.id = n.node_type_id
  WHERE n.id = p_node_id;

  IF v_node_id_text IS NULL THEN
    RAISE EXCEPTION 'Node not found for id=%', p_node_id;
  END IF;

  -- Collect stored attributes (excluding natural_*, internal and
  -- display_order types). display_order never stores a value row: any
  -- stored row is dead data — the emitted value is derived from
  -- sort_order in the v_natural_attrs block below.
  -- Integral value_number emits as a JSON integer (the wire literal the
  -- payload carried) instead of the scaled numeric form.
  SELECT
    jsonb_object_agg(ad.json_key,
                     CASE dt.name
                       WHEN 'int' THEN to_jsonb(TRUNC(na.value_number)::bigint)
                       ELSE COALESCE(
                         CASE
                           WHEN na.value_number = TRUNC(na.value_number)
                           THEN to_jsonb(TRUNC(na.value_number))
                           ELSE na.value_number::text::jsonb
                         END,
                         na.value_bool::text::jsonb, na.value_json, to_jsonb(na.value_string))
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

  -- Resolve the node's effective variant, mirroring the Python-side
  -- resolution in SchemaService._process_node_attributes:
  --   * the node type's NodeTypeVariant declares discriminator_attr ->
  --     the variant is the stored value of that attribute on this node;
  --   * the node type's NodeTypeVariant rows carry discriminator_attr
  --     NULL (a props node type) -> the variant is inherited from the
  --     parent node, one hop over schema_nodes.parent_id;
  --   * no NodeTypeVariant rows -> no variant (NULL).
  v_effective_variant := NULL;
  SELECT ntv.discriminator_attr INTO v_disc
  FROM s7.schema_node_type_variants ntv
  WHERE ntv.node_type_id = v_node_type_id
    AND ntv.discriminator_attr IS NOT NULL
  LIMIT 1;

  IF v_disc IS NOT NULL THEN
    SELECT na.value_string INTO v_effective_variant
    FROM s7.schema_node_attributes na
    JOIN s7.schema_attribute_defs ad2 ON ad2.id = na.attribute_def_id
    WHERE na.node_id = p_node_id
      AND ad2.node_type_id = v_node_type_id
      AND ad2.json_key = v_disc
      AND ad2.variant_key IS NULL
    LIMIT 1;
  ELSIF EXISTS (
    SELECT 1 FROM s7.schema_node_type_variants ntv
    WHERE ntv.node_type_id = v_node_type_id
      AND ntv.discriminator_attr IS NULL
  ) AND v_parent_id IS NOT NULL THEN
    -- Props node: the effective variant is inherited from the parent
    -- (one hop — the parent of a props node is a normal node with its
    -- own NTV). The LEFT JOIN keeps v_effective_variant NULL when the
    -- parent stores no discriminator value row.
    SELECT pna.value_string INTO v_effective_variant
    FROM s7.schema_nodes parent
    JOIN s7.schema_node_type_variants pntv
      ON pntv.node_type_id = parent.node_type_id
     AND pntv.discriminator_attr IS NOT NULL
    JOIN s7.schema_attribute_defs pad2
      ON pad2.node_type_id = parent.node_type_id
     AND pad2.json_key = pntv.discriminator_attr
     AND pad2.variant_key IS NULL
    LEFT JOIN s7.schema_node_attributes pna
      ON pna.node_id = parent.id
     AND pna.attribute_def_id = pad2.id
    WHERE parent.id = v_parent_id
    LIMIT 1;
  END IF;

  -- Build natural field overrides from schema_nodes columns, driven by AttributeDef definitions.
  -- Only include natural fields that are explicitly defined as AttributeDefs for this node's type.
  -- The variant filter mirrors SchemaService._process_node_attributes: a def applies when its
  -- variant_key IS NULL or matches the node's effective variant resolved above.
  -- Deterministic dedup: a json_key declared both as a base def
  -- (variant_key NULL) and as a def scoped to the effective variant
  -- enters the aggregate twice. The aggregate's own ORDER BY (the
  -- documented-guaranteed ordering) makes the exact-variant row win —
  -- jsonb_object_agg keeps the last value per key; ad.id keeps the order
  -- total among duplicate same-scope defs.
  SELECT jsonb_object_agg(json_key, derived_value ORDER BY is_base_variant DESC, ad_id)
  INTO v_natural_attrs
  FROM (
    SELECT
      ad.json_key,
      (ad.variant_key IS NULL) AS is_base_variant,
      ad.id AS ad_id,
      CASE dt.name
        WHEN 'natural_uuid'    THEN to_jsonb(p_node_id::text)
        WHEN 'natural_key'     THEN to_jsonb(v_node_key)
        WHEN 'natural_version' THEN to_jsonb(n.version)
        WHEN 'natural_order'   THEN to_jsonb(v_node_sort_order)
        WHEN 'display_order'   THEN to_jsonb(v_node_sort_order + 1)
      END AS derived_value
    FROM s7.schema_attribute_defs ad
    JOIN s7.schema_data_types dt ON dt.id = ad.data_type_id
    JOIN s7.schema_nodes n ON n.id = p_node_id
    WHERE ad.node_type_id = n.node_type_id
      AND (dt.name LIKE 'natural_%' OR dt.name = 'display_order')
      AND (ad.variant_key IS NULL OR ad.variant_key = v_effective_variant)
  ) AS natural_defs;

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

-- Same cache invalidation as the forward: removing the natural_name arm
-- changes emitted output for catalogs that declared the def, so cached
-- schema_json rows must rebuild under the restored body as well.
UPDATE s7.schema_build_state SET last_cached_build = NULL;
"""


def add_natural_name_data_type(apps, schema_editor):
    """Seed the `natural_name` DataType unless the base fixture already did."""
    DataType = apps.get_model('schemas', 'DataType')
    DataType.objects.get_or_create(
        name='natural_name',
        defaults={
            'id': NATURAL_NAME_DATA_TYPE_PK,
            'description': (
                'Maps to schema_nodes.name column — bidirectional sync '
                'with JSON export'
            ),
            'primary_storage_type': '',
            'editor_extension': '',
        },
    )


def remove_natural_name_data_type(apps, schema_editor):
    """Reverse of add_natural_name_data_type.

    Deletes only the canonical seeded row (matched by its fixture pk): a
    pre-existing `natural_name` row under a different pk was not created by
    this migration — or by the fixture — and is left untouched.
    """
    DataType = apps.get_model('schemas', 'DataType')
    DataType.objects.filter(pk=NATURAL_NAME_DATA_TYPE_PK).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("schemas", "0008_value_string_text"),
    ]

    operations = [
        migrations.RunSQL(
            sql=FN_BUILD_NODE_JSON_SQL,
            reverse_sql=FN_BUILD_NODE_JSON_ROLLBACK_SQL,
        ),
        migrations.RunPython(
            add_natural_name_data_type,
            reverse_code=remove_natural_name_data_type,
        ),
    ]
