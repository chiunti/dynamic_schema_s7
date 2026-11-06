import django.contrib.postgres.indexes
import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q

# MCP feature batch — consolidated feature migration (feature tag: mcp).
# feature: mcp
#
# Single migration for the whole MCP subsystem wave. Sections, in apply
# order:
#
# 1. Catalog hardening — partial unique constraint on root NodeType
#    json_scope (NULLS NOT DISTINCT) guarded by a readable pre-check, plus
#    a backfill copying domain_id onto stale variant-scoped AttributeDefs.
# 2. Per-user MCP tokens — UserMCPToken L1/L2 bearer credentials storing
#    only SHA-256 digests.
# 3. Root key+version scope — per-project uniqueness (NULLS NOT DISTINCT),
#    scoped lookup/build routine redefinitions, v_schema_published gains
#    project_id.
# 4. Build-path hardening — display_order exclusion in
#    s7_build_node_json's v_attrs, dead-variable removal from the same
#    routine, effective-variant resolution so variant-scoped
#    column-mapped defs emit in v_natural_attrs, structural list key
#    backfill, integral value_number emission as a JSON integer, and
#    build-state invalidation.
# 5. Governed changesets — CatalogChangeSet audit model (proposer XOR,
#    scopes/base_revision/validation_report/post_apply_report, status
#    lifecycle) plus the UserMCPToken propose capability
#    (can_propose_changeset flag and the allowed_scopes allowlist). The
#    scopes GIN index serializes the canonical django.contrib.postgres
#    GinIndex (the contrib app is installed by this batch's feature
#    commit). post_apply_report persists the post-apply revalidation
#    outcome of the affected published schemas on the changeset itself.

# --- catalog hardening -------------------------------------------------------
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


# --- per-user MCP tokens ----------------------------------------------------
#
# UserMCPToken is a per-user bearer credential for the MCP channel. Unlike
# project API credentials — a B2B contract scoped by boolean capability
# flags — each row belongs to one user and carries an mcp_level
# (1 = schema authoring, 2 = catalog authoring) that the server evaluates to
# gate MCP-only capabilities; the same user may hold several tokens at
# different levels and each can be revoked independently.
#
# The token secret is never persisted: only its SHA-256 hex digest is stored
# in token_digest, so a leaked row cannot be replayed as a bearer credential.
# Tokens expire on expires_at and are revoked by stamping revoked_at; the
# (user, revoked_at) index supports listing a user's active tokens.


# --- root key+version scope --------------------------------------------------
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


# --- build-path hardening ----------------------------------------------------
#
# --- display_order build exclusion (routine redefinition) --------------
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
# The derived v_natural_attrs block additionally resolves the node's
# effective variant so that column-mapped AttributeDefs scoped to a
# variant (variant_key non-NULL) emit their derived value on nodes whose
# effective variant matches: the node type's NodeTypeVariant declares
# discriminator_attr (the variant is the node's stored discriminator
# value), or carries it NULL for props node types (the variant is
# inherited one hop from the parent node). Nodes without a variant
# declaration keep the previous behavior — only NULL-scoped defs emit —
# and a json_key declared both base- and variant-scoped dedups through
# the aggregate's own ORDER BY with the exact-variant row winning.
#
# The same redefinition drops two dead declarations inherited from 0002:
# v_key (populated by a per-node SELECT whose result was never
# concatenated into v_result — the emitted 'key' comes from
# v_natural_attrs and the keyed-children channels) and v_slots (declared,
# never used).
#
# The v_attrs ELSE arm additionally restores wire fidelity for numeric
# attributes: value_number is numeric(30,10), so the bare ::text::jsonb
# emission rendered every stored number with decimal scale — a payload
# that imported 26 reconstructed as 26.0. An integral value_number (equal
# to its TRUNC) now emits to_jsonb(TRUNC(value_number)) — the scale-0
# numeric renders the JSON integer literal without a ::bigint cast, so
# even a 20-digit integral inside numeric(30,10) emits instead of raising
# out of the bigint range. The check is generic across the ELSE arm
# (number, float and any future value_number-backed datatype); the NULL
# case still falls through to the remaining COALESCE candidates.
#
# The forward also NULLs last_cached_build in schema_build_state: the
# cache-serving predicate (last_cached_build IS NOT DISTINCT FROM
# current_build) would otherwise keep serving schema_json rows built by
# the previous routine body until the next natural invalidation.
#
# reverse_sql restores the exact pre-migration routine body — the last
# definition applied before this migration.
#
# --- structural list key backfill --------------------------------------
#
# Structural list items imported before the indexed-key write convention
# existed were persisted with the bare slot json_key as node.key — every
# sibling sharing e.g. key='props'. s7_build_node_json's v_keyed_children
# channel emits those children through
# jsonb_object_agg(COALESCE(ch.key, ch_nt.default_json_key, ch_nt.name), ...),
# so the siblings collapse into a single entry on rebuild. The write path
# now stores '{json_key}_{index}' per item; the backfill below repairs the
# existing stock by rekeying every member of an emitted-key collision group
# to '<emitted_key>_<0..N-1>' ordered by sort_order — the same convention,
# with no sibling keeping the bare key.
#
# The scope replicates the v_keyed_children join exactly (as an EXISTS —
# a child enters the channel when at least one matching composition row
# qualifies): non-json_map children under a composition with
# collection_key IS NULL, plus shorthand-dispatch singleton children stored
# under a key other than ntc.collection_key. Outside this scope a shared
# key can be legitimate (is_json_map entries use key as the map entry;
# collection_key-ed children are aggregated as arrays), so it is left
# untouched.
#
# Groups are detected on the EMITTED key — the COALESCE the routine
# aggregates on — rather than the stored key alone. That additionally
# repairs collisions where a NULL-key row falls back to default_json_key
# equal to a sibling's stored key; rekeying such a row materializes its
# previously implicit key, which does not change its emission channel.
#
# name is synced to the new key only when it still equals the old emitted
# key (the write convention assigns the indexed value to both); a name
# that already diverges came from another mapping and is respected.
#
# The statement is idempotent by construction (after rekeying, every
# scoped (parent_id, emitted_key) group has COUNT = 1) and intentionally
# keeps a noop reverse: rekeyed rows are indistinguishable from rows that
# were always stored under the indexed convention, so no selective undo
# is possible.


 # ------------------------------
 # build_node_json
 # ------------------------------
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


REKEY_STRUCTURAL_KEYS_SQL = r"""
WITH scoped AS (
    SELECT
        s.id,
        s.parent_id,
        COALESCE(s.key, snt.default_json_key, snt.name) AS emitted_key,
        s.sort_order
    FROM s7.schema_nodes AS s
    JOIN s7.schema_node_types AS snt ON snt.id = s.node_type_id
    JOIN s7.schema_nodes AS p ON p.id = s.parent_id
    WHERE snt.is_json_map = FALSE
      AND EXISTS (
          SELECT 1
          FROM s7.schema_node_type_compositions AS ntc
          WHERE ntc.child_type_id = s.node_type_id
            AND ntc.parent_type_id = p.node_type_id
            AND (
                ntc.collection_key IS NULL
                OR (ntc.max_children = 1 AND s.key <> ntc.collection_key)
            )
      )
),
dup AS (
    SELECT parent_id, emitted_key
    FROM scoped
    GROUP BY parent_id, emitted_key
    HAVING COUNT(*) > 1
),
plan AS (
    SELECT
        s.id,
        s.emitted_key AS old_key,
        s.emitted_key || '_' || (
            ROW_NUMBER() OVER (
                PARTITION BY s.parent_id, s.emitted_key
                ORDER BY s.sort_order, s.id
            ) - 1
        ) AS new_key
    FROM scoped AS s
    JOIN dup AS d
      ON d.parent_id = s.parent_id
     AND d.emitted_key = s.emitted_key
)
UPDATE s7.schema_nodes AS ch
SET key = p.new_key,
    name = CASE
               WHEN ch.name = p.old_key THEN p.new_key
               ELSE ch.name
           END
FROM plan AS p
WHERE ch.id = p.id;
"""

# Rekeying changes the output of s7_build_node_json for the affected
# parents; cached schema_json rows built before the repair would keep
# serving the collapsed entries. NULLing last_cached_build drops the
# cache-serving predicate (last_cached_build IS NOT DISTINCT FROM
# current_build) so the next read rebuilds — the routine redefinition
# above already NULLs it for its own change, and this statement keeps the
# backfill's invalidation explicit and independent.
INVALIDATE_BUILD_STATE_SQL = r"""
UPDATE s7.schema_build_state SET last_cached_build = NULL;
"""

class Migration(migrations.Migration):

    dependencies = [
        ("schemas", "0006_conditional_expression_datatype"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
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
        migrations.CreateModel(
            name="UserMCPToken",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        db_default=models.Func(function="gen_random_uuid"),
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("token_digest", models.CharField(max_length=64, unique=True)),
                (
                    "mcp_level",
                    models.PositiveSmallIntegerField(
                        choices=[(1, "Schema authoring"), (2, "Catalog authoring")],
                        default=1,
                    ),
                ),
                ("expires_at", models.DateField()),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mcp_tokens",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "User MCP token",
                "verbose_name_plural": "User MCP tokens",
                "db_table": "schema_user_mcp_tokens",
                "indexes": [
                    models.Index(
                        fields=["user", "revoked_at"], name="idx_user_mcp_tok_active"
                    )
                ],
            },
        ),
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
        migrations.RunSQL(
            FN_BUILD_NODE_JSON_SQL,
            reverse_sql=FN_BUILD_NODE_JSON_ROLLBACK_SQL,
        ),
        migrations.RunSQL(
            sql=REKEY_STRUCTURAL_KEYS_SQL,
            # Intentional noop: rekeyed rows cannot be told apart from rows
            # that always carried the indexed key convention.
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql=INVALIDATE_BUILD_STATE_SQL,
            # Intentional noop: un-invalidating would resurrect stale JSON.
            reverse_sql=migrations.RunSQL.noop,
        ),
        # --- governed changesets -------------------------------------------
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
        migrations.CreateModel(
            name="CatalogChangeSet",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        db_default=models.Func(function="gen_random_uuid"),
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("proposed", "Proposed"),
                            ("validating", "Validating"),
                            ("valid", "Valid"),
                            ("invalid", "Invalid"),
                            ("approved", "Approved"),
                            ("applied", "Applied"),
                            ("rejected", "Rejected"),
                            ("stale", "Stale"),
                            ("superseded", "Superseded"),
                        ],
                        default="draft",
                        max_length=16,
                    ),
                ),
                ("ops", models.JSONField(default=list)),
                ("rationale", models.TextField()),
                ("scopes", models.JSONField(default=list)),
                (
                    "base_revision",
                    models.CharField(blank=True, max_length=64, null=True),
                ),
                ("validation_report", models.JSONField(blank=True, null=True)),
                ("post_apply_report", models.JSONField(blank=True, null=True)),
                ("review_note", models.TextField(blank=True, null=True)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "proposed_by_credential",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="proposed_catalog_changesets",
                        to="schemas.projectapicredential",
                    ),
                ),
                (
                    "proposed_by_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="proposed_catalog_changesets",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reviewed_catalog_changesets",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Catalog changeset",
                "verbose_name_plural": "Catalog changesets",
                "db_table": "schema_catalog_changesets",
                "indexes": [
                    models.Index(
                        fields=["status", "created_at"],
                        name="idx_scatalogchg_status_created",
                    ),
                    django.contrib.postgres.indexes.GinIndex(
                        fields=["scopes"], name="idx_scatalogchg_scopes"
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("proposed_by_credential__isnull", False),
                                ("proposed_by_user__isnull", True),
                            ),
                            models.Q(
                                ("proposed_by_credential__isnull", True),
                                ("proposed_by_user__isnull", False),
                            ),
                            _connector="OR",
                        ),
                        name="chk_scatalogchg_one_proposer",
                        violation_error_message="exactly one of proposed_by_credential / proposed_by_user is required",
                    )
                ],
            },
        ),
    ]
