from django.db import migrations, models


# MCP feature batch — root key+version scope (feature tag: mcp).
# feature: mcp
#
# --- root key+version uniqueness re-scoped per project ----------------------
#
# uq_snodes_key_version_root enforced a GLOBAL unique (key, version) over root
# nodes while the rest of the model is already per project: schema_build_state
# and schema_cache unique on (key, version, project_id), the *_scoped routine
# variants filter by project, and the API accepts project_id. Two projects
# could therefore never hold schemas that share key+version. The index is
# re-created over (key, version, project_id) with NULLS NOT DISTINCT
# (PostgreSQL >= 15, already the project floor — precedent:
# uq_snode_types_root_scope) so project-less roots share a single bucket and
# stay unique among themselves.
#
# The routines that resolve roots by key+version are redefined to match:
#
# - s7_find_schema_node_id, s7_check_key_version_exists and
#   s7_check_key_version_unique gain a trailing p_project_id parameter
#   (DEFAULT NULL) and filter with IS NOT DISTINCT FROM, so existing
#   positional callers keep working and NULL resolves the project-less
#   bucket. The old signatures are dropped first: CREATE OR REPLACE cannot
#   change an argument list — it would silently add an overload that
#   outranks the defaulted parameter and keep the global semantics alive.
#
# - s7_import_schema (JSONB) resolves the existing root and overwrite-deletes
#   only inside the caller's project bucket — previously the delete CTEs
#   removed roots of other projects sharing key+version. The TEXT overload
#   still delegates with p_project_id=NULL. s7_import_schema_scoped is
#   re-derived from the new base text through a local copy of the _derive()
#   mechanism — never maintain two copies.
#
# - The non-scoped publish/build routines are pinned to the project-less
#   bucket: s7_publish_schema, s7_build_schema and s7_build_schema_cached
#   filter project_id IS NULL. Their *_scoped counterparts already carry the
#   project filter and are not redefined here. schema_build_state and
#   schema_cache require a project row, so project-less roots skip cache
#   persistence entirely and always build fresh; the build_state/cache
#   predicates switch to IS NOT DISTINCT FROM so the project scope is
#   compared uniformly.
#
# - The satellite uniques uq_schema_build_state_key_version_project and
#   uq_schema_cache_key_version_project are recreated with NULLS NOT
#   DISTINCT via ORM constraint ops — same bucket semantics as the root
#   index (their project column is NOT NULL, so this documents intent and
#   can never fail on existing rows).
#
# - v_schema_published gains a trailing project_id column (CREATE OR REPLACE
#   VIEW may append columns; all previous columns are kept in order), which
#   s7_build_schema uses for its project_id IS NULL predicate.
#
# Every reverse_sql restores the exact pre-migration definition — original
# index, view and routine texts, including the scoped import re-derived from
# the original base — so a rollback returns the catalog to its previous
# state. Duplicates that only the wider scope allows make rollback fail
# loudly instead of silently dropping rows.


# ------------------------------
# Index: (key, version) -> (key, version, project_id)
# ------------------------------
IDX_ROOT_KEY_VERSION_SCOPE_SQL = r"""
SET search_path TO s7, public;

DROP INDEX IF EXISTS s7.uq_snodes_key_version_root;

CREATE UNIQUE INDEX uq_snodes_key_version_root
  ON s7.schema_nodes (key, version, project_id) NULLS NOT DISTINCT
  WHERE parent_id IS NULL AND version IS NOT NULL;
"""

IDX_ROOT_KEY_VERSION_SCOPE_ROLLBACK_SQL = r"""
SET search_path TO s7, public;

-- Fail loudly before the narrower index rejects rows that only the wider
-- (key, version, project_id) scope permits.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM s7.schema_nodes
    WHERE parent_id IS NULL
      AND version IS NOT NULL
      AND key IS NOT NULL
    GROUP BY key, version
    HAVING COUNT(*) > 1
  ) THEN
    RAISE EXCEPTION 'Cannot restore global root key+version uniqueness: duplicate (key, version) pairs exist across projects';
  END IF;
END $$;

DROP INDEX IF EXISTS s7.uq_snodes_key_version_root;

CREATE UNIQUE INDEX uq_snodes_key_version_root
  ON s7.schema_nodes (key, version)
  WHERE parent_id IS NULL AND version IS NOT NULL;
"""


# ------------------------------
# v_schema_published: expose project_id
# ------------------------------
VW_SCHEMA_PUBLISHED_SQL = r"""
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

# CREATE OR REPLACE VIEW cannot drop the appended column — recreate the view.
VW_SCHEMA_PUBLISHED_ROLLBACK_SQL = r"""
SET search_path TO s7, public;

DROP VIEW IF EXISTS v_schema_published;

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


 # ------------------------------
 # find_schema_node_id
 # ------------------------------
FN_FIND_SCHEMA_NODE_ID_SQL = r"""
SET search_path TO s7, public;

DROP FUNCTION IF EXISTS s7.s7_find_schema_node_id(TEXT, TEXT);

CREATE FUNCTION s7_find_schema_node_id(
  p_key TEXT,
  p_version TEXT,
  p_project_id UUID DEFAULT NULL
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
    AND n.project_id IS NOT DISTINCT FROM p_project_id
  LIMIT 1;

  RETURN v_root_id;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""

FN_FIND_SCHEMA_NODE_ID_ROLLBACK_SQL = r"""
SET search_path TO s7, public;

DROP FUNCTION IF EXISTS s7.s7_find_schema_node_id(TEXT, TEXT, UUID);

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
# check_key_version_unique
# ------------------------------
FN_CHECK_KEY_VERSION_UNIQUE_SQL = r"""
SET search_path TO s7, public;

DROP FUNCTION IF EXISTS s7.s7_check_key_version_unique(TEXT, TEXT, UUID);

CREATE FUNCTION s7_check_key_version_unique(
  p_key TEXT,
  p_version TEXT,
  p_exclude_node_id UUID DEFAULT NULL,
  p_project_id UUID DEFAULT NULL
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
      AND f.project_id IS NOT DISTINCT FROM p_project_id
      AND f.id != p_exclude_node_id;
  ELSE
    SELECT COUNT(*)
    INTO v_count
    FROM schema_nodes f
    JOIN schema_node_types nt ON nt.id = f.node_type_id AND nt.is_root = TRUE
    WHERE f.key = p_key
      AND f.version = p_version
      AND f.parent_id IS NULL
      AND f.project_id IS NOT DISTINCT FROM p_project_id;
  END IF;
  
  RETURN v_count = 0;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""

FN_CHECK_KEY_VERSION_UNIQUE_ROLLBACK_SQL = r"""
SET search_path TO s7, public;

DROP FUNCTION IF EXISTS s7.s7_check_key_version_unique(TEXT, TEXT, UUID, UUID);

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

DROP FUNCTION IF EXISTS s7.s7_check_key_version_exists(TEXT, TEXT);

CREATE FUNCTION s7_check_key_version_exists(
  p_key TEXT,
  p_version TEXT,
  p_project_id UUID DEFAULT NULL
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
      AND f.project_id IS NOT DISTINCT FROM p_project_id
  );
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""

FN_CHECK_KEY_VERSION_EXISTS_ROLLBACK_SQL = r"""
SET search_path TO s7, public;

DROP FUNCTION IF EXISTS s7.s7_check_key_version_exists(TEXT, TEXT, UUID);

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
    AND project_id IS NULL
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

FN_BUILD_SCHEMA_ROLLBACK_SQL = r"""
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
    AND f.project_id IS NULL
  LIMIT 1;

  IF v_root_id IS NULL THEN
    RAISE EXCEPTION 'Schema not found for key=%, version=%', p_key, p_version;
  END IF;

  -- Derive schema_type from actual json_scope if not explicitly provided
  v_schema_type := COALESCE(p_schema_type, REPLACE(v_json_scope, '_root', ''));

  -- schema_build_state and schema_cache require a project row; project-less
  -- roots cannot persist cache state and always build fresh.
  IF v_project_id IS NULL THEN
    RETURN jsonb_build_object(v_schema_type, s7_build_node_json(v_root_id));
  END IF;

  PERFORM s7_ensure_schema_build_state(p_key, p_version, v_project_id);

  SELECT current_build, last_cached_build
  INTO v_current_build, v_last_cached
  FROM schema_build_state
  WHERE key = p_key
    AND version  = p_version
    AND project_id IS NOT DISTINCT FROM v_project_id
  LIMIT 1;

  SELECT schema_json
  INTO v_json_cache
  FROM schema_cache
  WHERE key = p_key
    AND version  = p_version
    AND project_id IS NOT DISTINCT FROM v_project_id
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
    AND project_id IS NOT DISTINCT FROM v_project_id;

  RETURN v_json_fresh;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""

FN_BUILD_SCHEMA_CACHED_ROLLBACK_SQL = r"""
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
    AND project_id IS NOT DISTINCT FROM p_project_id;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""

FN_MARK_SCHEMA_DIRTY_ROLLBACK_SQL = r"""
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
    AND project_id IS NOT DISTINCT FROM p_project_id;
END;
$$ LANGUAGE plpgsql
SET search_path TO s7, public;
"""

FN_INCREMENT_BUILD_ROLLBACK_SQL = r"""
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
    AND f.project_id IS NULL
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
    AND f.project_id IS NULL
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

FN_PUBLISH_SCHEMA_ROLLBACK_SQL = r"""
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
  RAISE NOTICE 's7_import_schema: key=%, version=%, status=%, project=%, org=%',
    p_key, p_version, p_status, p_project_id, p_organization_id;

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
  -- Both lookups are scoped to the caller's project bucket.
  v_existing_root_id := s7_find_schema_node_id(v_key_eff, v_version_eff, p_project_id);
  
  -- Also check for nodes with key=NULL of the same node_type and project
  IF v_existing_root_id IS NULL THEN
    SELECT id INTO v_existing_root_id FROM schema_nodes
    WHERE key IS NULL
      AND version = v_version_eff
      AND node_type_id = v_root_type_id
      AND project_id IS NOT DISTINCT FROM p_project_id
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
      WHERE (key = v_key_eff OR (key IS NULL AND node_type_id = v_root_type_id))
        AND project_id IS NOT DISTINCT FROM p_project_id
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
      WHERE (key = v_key_eff OR (key IS NULL AND node_type_id = v_root_type_id))
        AND project_id IS NOT DISTINCT FROM p_project_id
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

# Original definition — restores the pre-migration body on rollback.
FN_IMPORT_SCHEMA_JSON_ROLLBACK_SQL = r"""
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
  RAISE NOTICE 's7_import_schema: key=%, version=%, status=%, project=%, org=%',
    p_key, p_version, p_status, p_project_id, p_organization_id;

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
# Project-scoped routine variants
#
# Scoped routines are derived from the base SQL text above; if the base SQL
# drifts the derivation fails loudly instead of shipping a silent no-op.
# The same mechanism derives the pre-migration scoped import from the
# original base for the rollback path.
# ------------------------------

def _derive(sql: str, *pairs: tuple[str, str]) -> str:
    """Apply ``str.replace`` pairs that must all match."""
    for old, new in pairs:
        if old not in sql:
            raise RuntimeError(
                "scoped routine derivation failed: fragment not found "
                f"in base SQL: {old[:80]!r}..."
            )
        sql = sql.replace(old, new)
    return sql


SCOPED_IMPORT_SQL = _derive(
    FN_IMPORT_SCHEMA_JSON_SQL,
    (
        'CREATE OR REPLACE FUNCTION s7_import_schema(',
        'CREATE OR REPLACE FUNCTION s7_import_schema_scoped(',
    ),
    (
        "  RAISE NOTICE 's7_import_schema: key=%, version=%, status=%, project=%, org=%',\n"
        "    p_key, p_version, p_status, p_project_id, p_organization_id;",
        '',
    ),
    (
        "  IF p_schema IS NULL OR p_schema = 'null'::jsonb THEN",
        """  IF p_project_id IS NULL OR p_organization_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM schema_projects
    WHERE id = p_project_id AND organization_id = p_organization_id
  ) THEN
    RAISE EXCEPTION 'Project and organization do not match';
  END IF;

  IF p_schema IS NULL OR p_schema = 'null'::jsonb THEN""",
    ),
    # No CTE pair here: the base now scopes both delete CTEs with
    # IS NOT DISTINCT FROM p_project_id, which equals = for the non-null
    # project this variant validates up front.
    (
        '  -- Create root node with project_id and organization_id',
        '''  DELETE FROM schema_cache
  WHERE key = v_key_eff AND version = v_version_eff AND project_id = p_project_id;
  UPDATE schema_build_state SET dirty = TRUE
  WHERE key = v_key_eff AND version = v_version_eff AND project_id = p_project_id;

  -- Create root node with project_id and organization_id''',
    ),
)

# Rollback derives the scoped variant from the original base exactly as it
# was defined before this migration.
SCOPED_IMPORT_ROLLBACK_SQL = _derive(
    FN_IMPORT_SCHEMA_JSON_ROLLBACK_SQL,
    (
        'CREATE OR REPLACE FUNCTION s7_import_schema(',
        'CREATE OR REPLACE FUNCTION s7_import_schema_scoped(',
    ),
    (
        "  RAISE NOTICE 's7_import_schema: key=%, version=%, status=%, project=%, org=%',\n"
        "    p_key, p_version, p_status, p_project_id, p_organization_id;",
        '',
    ),
    (
        "  IF p_schema IS NULL OR p_schema = 'null'::jsonb THEN",
        """  IF p_project_id IS NULL OR p_organization_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM schema_projects
    WHERE id = p_project_id AND organization_id = p_organization_id
  ) THEN
    RAISE EXCEPTION 'Project and organization do not match';
  END IF;

  IF p_schema IS NULL OR p_schema = 'null'::jsonb THEN""",
    ),
    (
        '  v_existing_root_id := s7_find_schema_node_id(v_key_eff, v_version_eff);',
        '''  SELECT id INTO v_existing_root_id FROM schema_nodes
  WHERE key = v_key_eff AND version = v_version_eff
    AND project_id = p_project_id AND parent_id IS NULL
  LIMIT 1;''',
    ),
    (
        '        AND version = v_version_eff \n        AND parent_id IS NULL',
        '        AND version = v_version_eff \n        AND project_id = p_project_id\n        AND parent_id IS NULL',
    ),
    (
        '  -- Create root node with project_id and organization_id',
        '''  DELETE FROM schema_cache
  WHERE key = v_key_eff AND version = v_version_eff AND project_id = p_project_id;
  UPDATE schema_build_state SET dirty = TRUE
  WHERE key = v_key_eff AND version = v_version_eff AND project_id = p_project_id;

  -- Create root node with project_id and organization_id''',
    ),
)


def check_null_key_root_versions(apps, schema_editor):
    """Abort with a readable listing if key-less roots violate the new index.

    The old index compared NULL keys as distinct (the default), so several
    roots may legitimately share (version, project_id) with key=NULL.
    NULLS NOT DISTINCT compares them as equal — the new index would fail
    to build, so the duplicates are listed up front instead.
    """
    Node = apps.get_model("schemas", "Node")
    duplicates = (
        Node.objects.filter(parent__isnull=True, key__isnull=True)
        .exclude(version__isnull=True)
        .values("version", "project_id")
        .annotate(total=models.Count("id"))
        .filter(total__gt=1)
        .order_by("version")
    )
    listing = ", ".join(
        f"(version={row['version']!r}, project_id={row['project_id']})"
        for row in duplicates
    )
    if listing:
        raise RuntimeError(
            "Cannot create index uq_snodes_key_version_root with NULLS NOT "
            "DISTINCT: multiple root nodes share (key=NULL, version, "
            f"project_id): {listing}. Resolve the duplicates before "
            "re-running this migration."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("schemas", "0007_mcp_user_tokens"),
    ]

    operations = [
        migrations.RunPython(
            check_null_key_root_versions,
            reverse_code=migrations.RunPython.noop,
        ),
        # Satellite uniques get the same NULLS NOT DISTINCT bucket semantics
        # as the root index (ORM ops keep the migration state in sync).
        migrations.RemoveConstraint(
            model_name="buildstate",
            name="uq_schema_build_state_key_version_project",
        ),
        migrations.AddConstraint(
            model_name="buildstate",
            constraint=models.UniqueConstraint(
                fields=["key", "version", "project"],
                name="uq_schema_build_state_key_version_project",
                nulls_distinct=False,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="schemacache",
            name="uq_schema_cache_key_version_project",
        ),
        migrations.AddConstraint(
            model_name="schemacache",
            constraint=models.UniqueConstraint(
                fields=["key", "version", "project"],
                name="uq_schema_cache_key_version_project",
                nulls_distinct=False,
            ),
        ),
        migrations.RunSQL(
            sql=IDX_ROOT_KEY_VERSION_SCOPE_SQL,
            reverse_sql=IDX_ROOT_KEY_VERSION_SCOPE_ROLLBACK_SQL,
        ),
        migrations.RunSQL(
            sql=VW_SCHEMA_PUBLISHED_SQL,
            reverse_sql=VW_SCHEMA_PUBLISHED_ROLLBACK_SQL,
        ),
        migrations.RunSQL(
            sql=FN_FIND_SCHEMA_NODE_ID_SQL,
            reverse_sql=FN_FIND_SCHEMA_NODE_ID_ROLLBACK_SQL,
        ),
        migrations.RunSQL(
            sql=FN_CHECK_KEY_VERSION_UNIQUE_SQL,
            reverse_sql=FN_CHECK_KEY_VERSION_UNIQUE_ROLLBACK_SQL,
        ),
        migrations.RunSQL(
            sql=FN_CHECK_KEY_VERSION_EXISTS_SQL,
            reverse_sql=FN_CHECK_KEY_VERSION_EXISTS_ROLLBACK_SQL,
        ),
        migrations.RunSQL(
            sql=FN_BUILD_SCHEMA_SQL,
            reverse_sql=FN_BUILD_SCHEMA_ROLLBACK_SQL,
        ),
        migrations.RunSQL(
            sql=FN_BUILD_SCHEMA_CACHED_SQL,
            reverse_sql=FN_BUILD_SCHEMA_CACHED_ROLLBACK_SQL,
        ),
        migrations.RunSQL(
            sql=FN_MARK_SCHEMA_DIRTY_SQL,
            reverse_sql=FN_MARK_SCHEMA_DIRTY_ROLLBACK_SQL,
        ),
        migrations.RunSQL(
            sql=FN_INCREMENT_BUILD_SQL,
            reverse_sql=FN_INCREMENT_BUILD_ROLLBACK_SQL,
        ),
        migrations.RunSQL(
            sql=FN_PUBLISH_SCHEMA_SQL,
            reverse_sql=FN_PUBLISH_SCHEMA_ROLLBACK_SQL,
        ),
        migrations.RunSQL(
            sql=FN_IMPORT_SCHEMA_JSON_SQL,
            reverse_sql=FN_IMPORT_SCHEMA_JSON_ROLLBACK_SQL,
        ),
        # Project-scoped variant (depends on the base function above)
        migrations.RunSQL(
            sql=SCOPED_IMPORT_SQL,
            reverse_sql=SCOPED_IMPORT_ROLLBACK_SQL,
        ),
    ]
