import logging
import uuid
from copy import deepcopy
from typing import Any, Optional

from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction

from ..models import AttributeDef, Domain, Node, NodeType, NodeTypeComposition, NodeTypeVariant
from ..repositories.schema_repository import SchemaRepository
from ..repositories.attribute_def_repository import AttributeDefRepository
from ..repositories.composition_repository import CompositionRepository
from ..repositories.data_type_repository import DataTypeRepository
from ..repositories.node_type_repository import NodeTypeRepository
from .permission_service import PermissionService
from ..repositories.project_repository import ProjectRepository
from .conditional_validator import validate_conditional_structure
from .datatype_plugins import (
    get_datatype_doc,
    get_storage_defaults,
    get_storage_type,
    validate_datatype_value,
)
from .exceptions import SchemaValidationError
from .schema_validation_service import SchemaValidationService
from ..constants import (
    ERR_VERSION_NOT_SET,
    ERR_STATUS_ATTRIBUTE_NOT_FOUND,
    ERR_STATUS_ATTRIBUTE_VALUE_NOT_FOUND,
    ERR_VERSION_NOT_AVAILABLE,
    ERR_PERMISSION_DENIED,
    ERR_DATABASE_ERROR_ARCHIVE,
    ERR_UNEXPECTED_ERROR_ARCHIVE,
    ERR_DATABASE_ERROR_DRAFT,
    ERR_UNEXPECTED_ERROR_DRAFT,
    ERR_DATABASE_ERROR_BUILD,
    ERR_UNEXPECTED_ERROR_BUILD,
    ERR_DATABASE_ERROR_CACHE_REBUILD,
    ERR_UNEXPECTED_ERROR_CACHE_REBUILD,
    ERR_PUBLISHED_STATUS_NOT_AVAILABLE,
    ERR_ARCHIVED_STATUS_NOT_AVAILABLE,
    ERR_DRAFT_STATUS_NOT_AVAILABLE,
    ERR_SCHEMA_NOT_IN_DRAFT_STATUS,
    ERR_SCHEMA_NOT_PUBLISHED_STATUS,
    ERR_SCHEMA_NOT_ARCHIVED_STATUS,
    ERR_INCOMPLETE_SCHEMA,
    ERR_INVALID_NODE_ID,
    ERR_NODE_NOT_FOUND,
    ERR_NOT_A_ROOT_NODE,
    ERR_SCHEMA_NOT_FOUND,
    ERR_PROJECT_ID_REQUIRED,
    ERR_INVALID_SCHEMA,
    ERR_UNDECLARED_JSON_KEY,
    ERR_INVALID_COLLECTION_VALUE,
    ERR_INVALID_DOMAIN_VALUE,
    ERR_ROOT_NODE_TYPE_REQUIRED,
    MAX_VERSION_AUTO_INCREMENT_ATTEMPTS,
    SCHEMA_KEY_SUFFIX,
    SCHEMA_METADATA_SUFFIX,
    STATUS_DRAFT,
    STATUS_PUBLISHED,
    STATUS_ARCHIVED,
    WARN_DROPPED_ATTRIBUTE,
)


_EXAMPLE_MAX_DEPTH = 8
"""Hard depth bound for the generated JSON example recursion.

The ancestor guard in _generate_node_example already cuts composition
cycles; this bound additionally caps pathological deep chains so the
few-shot payload stays small.
"""

_EXAMPLE_NODE_FIELD_KEYS = frozenset({"id", "key", "version"})
"""Attribute json_keys the example never emits: they map to Node columns."""

_EXAMPLE_STRING_DATATYPES = frozenset({"string", "natural_key", "internal"})
"""Datatype names whose example value is ``f"example_{json_key}"``."""

_EXAMPLE_STATIC_VALUES: dict = {
    "int": 1,
    "number": 1.0,
    "float": 1.0,
    "bool": True,
    "list_string": ["option_1", "option_2"],
    "list_int": [1, 2],
    "int_tuple": [1, 2],
    "list_dict": [{}],
    "json": {},
    "dict": {},
    "domain_list": [],
    "options_field": [{"label": "Option 1", "value": "option_1"}],
    "date": "2026-01-01",
    "url": "https://example.com/",
    "color": "#000000",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "auto_uuid": "00000000-0000-0000-0000-000000000000",
    "natural_uuid": "00000000-0000-0000-0000-000000000000",
    "natural_version": "1",
    "natural_order": 1,
    "display_order": 1,
}
"""Static example values by DataType name for undocumented datatypes."""


class SchemaService:
    """Service for schema-related business logic"""
    
    def __init__(self):
        self.repository = SchemaRepository()
        self.attribute_def_repository = AttributeDefRepository()
        self.composition_repository = CompositionRepository()
        self.data_type_repository = DataTypeRepository()
        self.node_type_repository = NodeTypeRepository()

    def resolve_root_node_type(self, scope: Optional[str]) -> Optional[NodeType]:
        """Resolve the root NodeType for a json_scope.

        Falls back to the scope-less root node type (is_root with
        json_scope IS NULL) when the scope is empty or does not match any
        root type — it never guesses a named catalog's root.
        """
        node_type = self.node_type_repository.get_root_node_type_by_scope(scope) if scope else None
        if not node_type:
            node_type = self.node_type_repository.get_scopeless_root_node_type()
        return node_type

    def generate_json_example(self, schema_type: str) -> Optional[dict]:
        """Generate a JSON example for a root schema type (by json_scope).

        The example contains the root key wrapping a recursively built
        example object: declared attributes plus one representative child
        per composition, bounded by cycle and depth guards.

        Returns:
            Example dict, or None if the schema type does not resolve to a
            root NodeType.
        """
        root_node_type = self.node_type_repository.get_root_node_type_by_scope(schema_type)
        if not root_node_type:
            return None
        return self._generate_json_example(root_node_type)

    def generate_json_example_for_root(self, node_type: NodeType) -> dict:
        """Generate a JSON example from an already-resolved root NodeType.

        Same output as generate_json_example, but skips the json_scope
        lookup for callers that already hold the root instance.

        Raises:
            ValueError: If node_type is None or is not a root NodeType.

        Returns:
            Example dict.
        """
        if node_type is None or not node_type.is_root:
            raise ValueError(ERR_ROOT_NODE_TYPE_REQUIRED)
        return self._generate_json_example(node_type)

    def _generate_json_example(self, node_type: NodeType) -> dict:
        """Generate a JSON example for a root NodeType.

        Emits ``{root_key: example_object}`` where ``root_key`` is the
        json_scope without its ``_root`` suffix (or the node type name) and
        the object is built by ``_generate_node_example``: declared required
        attributes, a bounded set of illustrative optional attributes, one
        representative child per ``collection_key`` (a singleton object when
        ``max_children == 1``, a one-item list otherwise) and each
        structural child under its declared ``default_json_key``. Recursion
        is bounded by an ancestor guard and ``_EXAMPLE_MAX_DEPTH``.

        Returns:
            Example dict keyed by the root JSON key.
        """
        root_key = node_type.json_scope.replace('_root', '') if node_type.json_scope else node_type.name
        return {root_key: self._generate_node_example(node_type)}

    def _generate_node_example(
        self,
        node_type: NodeType,
        active_variant: Optional[str] = None,
        ancestors: frozenset = frozenset(),
    ) -> dict:
        """Build the example attribute-object of a node type, recursively.

        The caller wraps the returned dict under the structural key of the
        slot the node occupies (``collection_key`` for collection members,
        ``default_json_key``/name for structural children).

        Args:
            node_type: NodeType whose example object is generated.
            active_variant: Variant inherited from the parent node; only
                meaningful for props node types (a NodeTypeVariant with
                ``discriminator_attr`` NULL). Other node types resolve
                their own variant from the declared discriminator.
            ancestors: Ids of the node types above this one on the current
                path. A node type already present emits its attributes but
                does not expand compositions — the cycle guard.

        Returns:
            Dict of json_key -> example value / nested example objects.
        """
        compositions = list(
            self.composition_repository.get_compositions_by_node_type(node_type)
        )
        expandable = (
            node_type.id not in ancestors and len(ancestors) < _EXAMPLE_MAX_DEPTH
        )

        if getattr(node_type, 'is_json_map', False):
            # A map node's attribute object IS the keyed map: emit a single
            # example entry (or {} when there is nothing safe to show).
            if not expandable:
                return {}
            return self._generate_map_entry_example(
                node_type, compositions, ancestors
            )

        attr_defs = list(
            self.attribute_def_repository.get_attribute_defs_by_node_type(
                node_type
            ).select_related('data_type', 'domain')
        )
        ntv = self.node_type_repository.get_node_type_variant_by_node_type(
            node_type
        )
        node_variant = self._resolve_example_variant(
            node_type, ntv, active_variant, attr_defs
        )
        example = self._build_example_attributes(attr_defs, node_variant, ntv)

        if expandable:
            child_ancestors = ancestors | {node_type.id}
            collections: dict[str, list] = {}
            structural: list = []
            for composition in compositions:
                if composition.collection_key:
                    collections.setdefault(composition.collection_key, []).append(
                        composition
                    )
                else:
                    structural.append(composition)
            # One representative child type per collection_key, chosen
            # deterministically (first child_type name).
            for collection_key in sorted(collections):
                representative = min(
                    collections[collection_key],
                    key=lambda comp: comp.child_type.name,
                )
                item = self._generate_node_example(
                    representative.child_type, ancestors=child_ancestors
                )
                example[collection_key] = (
                    item if representative.max_children == 1 else [item]
                )
            # Structural children live under the child type's declared key.
            for composition in sorted(
                structural, key=lambda comp: comp.child_type.name
            ):
                child_type = composition.child_type
                json_key = child_type.default_json_key or child_type.name
                # Props node types inherit this node's variant.
                child_variant = (
                    node_variant
                    if self.node_type_repository.is_props_node_type(child_type)
                    else None
                )
                example[json_key] = self._generate_node_example(
                    child_type,
                    active_variant=child_variant,
                    ancestors=child_ancestors,
                )
        return example

    def _build_example_attributes(
        self,
        attr_defs: list,
        active_variant: Optional[str],
        ntv: Optional[NodeTypeVariant],
    ) -> dict:
        """Select and value the attributes emitted by an example node.

        Applicable AttributeDefs are the ``variant_key`` NULL rows plus the
        rows of the active variant; on json_key conflicts the
        variant-scoped def wins (more specific).

        Emission order: every applicable required def (sorted by json_key),
        then all optional defs whose datatype documents an 'example' (e.g.
        the conditional grammar), then the 'required' flag when declared,
        then up to two more optional defs by json_key. When the node type
        declares a variant discriminator and a variant was resolved, the
        discriminator json_key is forced to that variant value.
        """
        applicable: dict[str, AttributeDef] = {}
        for attr_def in attr_defs:
            if (
                attr_def.variant_key is not None
                and attr_def.variant_key != active_variant
            ):
                continue
            existing = applicable.get(attr_def.json_key)
            if existing is None or (
                existing.variant_key is None and attr_def.variant_key is not None
            ):
                applicable[attr_def.json_key] = attr_def

        emittable = [
            attr_def
            for attr_def in applicable.values()
            if self._is_emittable_example_attribute(attr_def)
        ]
        required = sorted(
            (d for d in emittable if d.is_required), key=lambda d: d.json_key
        )
        optional = sorted(
            (d for d in emittable if not d.is_required), key=lambda d: d.json_key
        )

        example = {}
        for attr_def in required:
            example[attr_def.json_key] = self._get_example_value(attr_def)
        for attr_def in (d for d in optional if self._has_documented_example(d)):
            example[attr_def.json_key] = self._get_example_value(attr_def)
        required_flag = next(
            (d for d in optional if d.json_key == 'required'), None
        )
        if required_flag is not None and required_flag.json_key not in example:
            example[required_flag.json_key] = self._get_example_value(required_flag)
        extra_slots = 2
        for attr_def in optional:
            if extra_slots <= 0:
                break
            if attr_def.json_key in example:
                continue
            example[attr_def.json_key] = self._get_example_value(attr_def)
            extra_slots -= 1

        # The discriminator must show the variant the example actually
        # activates: required domain-bound defs already produce this value;
        # the overwrite covers non-domain discriminators too.
        if (
            ntv is not None
            and ntv.discriminator_attr
            and active_variant is not None
            and ntv.discriminator_attr in applicable
        ):
            example[ntv.discriminator_attr] = active_variant
        return example

    @staticmethod
    def _is_emittable_example_attribute(attr_def: AttributeDef) -> bool:
        """Whether an AttributeDef may appear in the generated example.

        Excludes json_keys backed by Node columns ('id', 'key', 'version')
        and the ``natural_*``/``display_order`` datatypes — they map to Node
        columns and are exempt from the required-attribute check.
        """
        if attr_def.json_key in _EXAMPLE_NODE_FIELD_KEYS:
            return False
        data_type_name = attr_def.data_type.name if attr_def.data_type else ''
        return (
            not data_type_name.startswith('natural_')
            and data_type_name != 'display_order'
        )

    @staticmethod
    def _has_documented_example(attr_def: AttributeDef) -> bool:
        """Whether the attr's datatype registers an 'example' in DATATYPE_DOCS."""
        if not attr_def.data_type:
            return False
        doc = get_datatype_doc(attr_def.data_type.name)
        return doc is not None and 'example' in doc

    def _resolve_example_variant(
        self,
        node_type: NodeType,
        ntv: Optional[NodeTypeVariant],
        inherited_variant: Optional[str],
        attr_defs: list,
    ) -> Optional[str]:
        """Resolve the variant an example node activates.

        - No ``NodeTypeVariant`` row: the type has no variants -> None.
        - ``discriminator_attr`` NULL: props/inherited-variant node type —
          keeps the variant received from the parent.
        - Otherwise the example self-discriminates on the first
          ``DomainItem.value`` of the discriminator AttributeDef's domain,
          falling back to the first declared ``variant_key``.
        """
        if ntv is None:
            return None
        if not ntv.discriminator_attr:
            return inherited_variant
        discriminator_def = next(
            (
                attr_def
                for attr_def in attr_defs
                if attr_def.json_key == ntv.discriminator_attr
                and attr_def.domain_id
            ),
            None,
        )
        if discriminator_def is None:
            discriminator_def = (
                self.attribute_def_repository.get_attribute_def_with_domain(
                    node_type, ntv.discriminator_attr
                )
            )
        if discriminator_def is not None and discriminator_def.domain_id:
            first_value = self._first_domain_item_value(discriminator_def.domain)
            if first_value is not None:
                return first_value
        variant_keys = self.node_type_repository.get_variant_keys_by_node_type(
            node_type
        )
        return variant_keys[0] if variant_keys else None

    def _generate_map_entry_example(
        self,
        node_type: NodeType,
        compositions: list[NodeTypeComposition],
        ancestors: frozenset,
    ) -> dict:
        """Emit a single-entry example for an ``is_json_map`` node type.

        Map keys are data, so the example uses the literal key
        ``'example_key'``. The entry child type is the structural
        (``collection_key`` NULL) candidate that is itself a map — the
        nested-map dialect — or the first candidate by name. A scalar
        candidate (``json_scalar_attribute``) emits a scalar entry so the
        example stays inside what the keyed-map pipeline accepts.
        """
        candidates = sorted(
            (comp.child_type for comp in compositions if comp.collection_key is None),
            key=lambda child_type: child_type.name,
        )
        # Prefer candidates that can still expand — the map node type
        # itself or an ancestor only emits the empty/attrs-only fallback.
        viable = [
            c
            for c in candidates
            if c.id not in ancestors and c.id != node_type.id
        ] or candidates
        chosen = next(
            (child_type for child_type in viable if child_type.is_json_map),
            viable[0] if viable else None,
        )
        if chosen is None:
            return {}
        if chosen.json_scalar_attribute:
            scalar_def = next(
                (
                    attr_def
                    for attr_def in self.attribute_def_repository.get_attribute_defs_by_node_type(chosen)
                    if attr_def.json_key == chosen.json_scalar_attribute
                ),
                None,
            )
            value = (
                self._get_example_value(scalar_def)
                if scalar_def is not None
                else 'example_value'
            )
            return {'example_key': value}
        return {
            'example_key': self._generate_node_example(
                chosen, ancestors=ancestors | {node_type.id}
            )
        }

    def _first_domain_item_value(self, domain: Domain) -> Optional[str]:
        """Return the first ``DomainItem.value`` of a domain, sorted by value."""
        items = sorted(
            self.attribute_def_repository.get_domain_items_by_domain(domain),
            key=lambda item: item.value,
        )
        return items[0].value if items else None

    def _get_example_value(self, attr_def: AttributeDef) -> Any:
        """Generate an example value for an AttributeDef.

        Precedence:
          1. Domain-bound defs: the first ``DomainItem.value`` — domain
             membership always wins so the example survives strict-key
             validation (out-of-domain scalars are rejected there).
          2. Datatypes with a ``DATATYPE_DOCS`` entry carrying an
             'example' (e.g. the conditional grammar).
          3. ``_EXAMPLE_STATIC_VALUES`` / ``_EXAMPLE_STRING_DATATYPES``.
          4. The datatype's ``primary_storage_type`` fallback, last resort
             ``f"example_{json_key}"``.
        """
        if attr_def.domain:
            first_value = self._first_domain_item_value(attr_def.domain)
            if first_value is not None:
                return first_value

        data_type = attr_def.data_type
        data_type_name = data_type.name if data_type else 'string'

        doc = get_datatype_doc(data_type_name)
        if doc is not None and 'example' in doc:
            return deepcopy(doc['example'])

        if data_type_name in _EXAMPLE_STATIC_VALUES:
            return deepcopy(_EXAMPLE_STATIC_VALUES[data_type_name])
        if data_type_name in _EXAMPLE_STRING_DATATYPES:
            return f"example_{attr_def.json_key}"

        if data_type is not None:
            storage = get_storage_type(data_type)
            if storage == 'string':
                return f"example_{attr_def.json_key}"
            if storage == 'number':
                return 1
            if storage == 'bool':
                return True
            if storage == 'json':
                return {}
        return f"example_{attr_def.json_key}"

    @transaction.atomic
    def import_schema(
        self,
        validated_schema: dict,
        schema_key: str,
        schema_version: Optional[str],
        schema_status: str,
        overwrite: bool,
        project_id: Optional[uuid.UUID] = None,
        organization_id: Optional[uuid.UUID] = None,
        user=None,
        strict_keys: bool = False,
        path: str = "$",
        dropped_attributes: Optional[list] = None,
    ) -> tuple[uuid.UUID, dict]:
        """Import a schema with validation.

        ``strict_keys`` enables strict mode (credential imports / validate
        dry-run): undeclared JSON keys, non-collection values on declared
        collection keys and out-of-domain scalar values raise
        ``SchemaValidationError`` instead of being silently tolerated.
        ``path`` is the JSON path prefix used to locate errors inside the
        payload; the root schema object is addressed as ``$``.
        ``dropped_attributes`` is an optional out-list collecting
        ``{"code", "path", "message"}`` dicts for attribute values rejected
        by datatype validators in permissive mode; under ``strict_keys``
        those failures raise ``SchemaValidationError`` instead.
        """
        if project_id:
            project = ProjectRepository().get_project_by_id(project_id)
            if not project or project.organization_id != organization_id or not project.organization.is_active:
                raise PermissionError(ERR_PERMISSION_DENIED)
            if user and not PermissionService().can_edit_organization(user, project.organization_id):
                raise PermissionError(ERR_PERMISSION_DENIED)
        
        # Track if schema_version was explicitly specified by user
        # Empty string or None means user didn't specify a version
        form_version_is_explicit = schema_version is not None and schema_version != ""
        
        # Import the root node using s7_import_schema
        # If schema_version is None, repository will use "1" as fallback
        schema_id = self.repository.import_schema(
            validated_schema,
            schema_key,
            schema_version,
            schema_status,
            overwrite,
            project_id=project_id,
            organization_id=organization_id,
        )
        
        # Process the complete JSON structure to create child nodes
        self._process_schema_structure(
            validated_schema,
            schema_id,
            project_id,
            organization_id,
            strict_keys=strict_keys,
            path=path,
            dropped_attributes=dropped_attributes,
        )
        
        # Sync Node.version from child nodes that have natural_version attributes
        # This allows schemas like 'screen' to define their version in metadata.version
        version_warning = self._sync_version_from_metadata(schema_id, validated_schema, schema_version, form_version_is_explicit)
        
        # Ensure key and status are set as attributes on the root node
        self._set_schema_metadata(schema_id, schema_key, schema_status)

        # A schema imported directly as 'published' bypasses the publish routine,
        # so its cache entry must be built here to keep it readable.
        if schema_status == STATUS_PUBLISHED:
            root_node = self.repository.get_node_by_id(schema_id)
            self.build_schema_cached(root_node.key, root_node.version,
                                     project_id=project_id)

        return schema_id, version_warning
    
    def _sync_version_from_metadata(self, schema_id: uuid.UUID, validated_schema: dict, form_version: Optional[str], form_version_is_explicit: bool) -> dict:
        """Sync Node.version from child nodes that have natural_version attributes.

        This allows schemas to define their version in a child node (e.g., metadata.version)
        instead of the root level. The logic is schema-driven:
        1. Find child compositions of the root node type
        2. Check if any child type has a 'version' attribute with natural_version datatype
        3. If the JSON has that child with a version value, update Node.version

        The child may arrive as a plain dict or as a list — singleton
        composition slots are serialized as an array of items. A list is
        normalized to its first dict item; a list with more than one item logs
        a warning and still uses the first item.

        Priority:
        - If user specified version in form: use form version (warn if JSON differs)
        - If form is empty/default: use JSON version

        Args:
            schema_id: Root node ID
            validated_schema: The complete JSON schema
            form_version: Version from the import form (may be None)
            form_version_is_explicit: True if user explicitly specified version in form

        Returns:
            dict with warning information if versions mismatched, empty dict otherwise
        """
        import logging

        logger = logging.getLogger(__name__)

        root_node = self.repository.get_node_by_id(schema_id)
        root_type = root_node.node_type

        # Get all child compositions of the root node type
        compositions = self.composition_repository.get_compositions_by_parent_type(root_type)

        for comp in compositions:
            child_type = comp.child_type

            # Check if this child type has a 'version' attribute with natural_version datatype
            version_attr = self.attribute_def_repository.get_attribute_def_by_node_type_json_key_variant(child_type, 'version', None)
            if version_attr and version_attr.data_type.name != 'natural_version':
                version_attr = None

            if not version_attr:
                continue

            # Get the collection_key to find the child in the JSON
            collection_key = comp.collection_key

            # Extract the root object from the JSON
            root_key = next(iter(validated_schema.keys())) if validated_schema else None
            if not root_key:
                continue

            root_obj = validated_schema[root_key] if isinstance(validated_schema[root_key], dict) else {}

            # Find the child in the JSON by collection_key
            child_obj = None
            if collection_key and collection_key in root_obj:
                child_obj = root_obj[collection_key]
            elif isinstance(root_obj, dict):
                # Fallback: the structural key declared by the child type
                # (default_json_key), then the child type name itself.
                child_obj = root_obj.get(child_type.default_json_key or child_type.name)
                if child_obj is None and child_type.default_json_key:
                    child_obj = root_obj.get(child_type.name)

            # Singleton composition slots are serialized as a list of items;
            # normalize to a single dict before reading the version. A list
            # with more than one item means the slot's singleton contract was
            # not enforced upstream — use the first item and warn, since this
            # is not an import error.
            if isinstance(child_obj, list):
                if len(child_obj) > 1:
                    logger.warning(
                        "Singleton child slot '%s' received %d items during schema import; using the first item.",
                        collection_key or child_type.default_json_key or child_type.name,
                        len(child_obj),
                    )
                child_obj = child_obj[0] if child_obj and isinstance(child_obj[0], dict) else None

            if not child_obj or not isinstance(child_obj, dict):
                continue

            # Extract version from the child object
            json_version = child_obj.get('version')

            if not json_version:
                continue

            # Get the actual form version (may be empty string if not specified)
            actual_form_version = form_version if form_version else "1"

            # Priority: form version > JSON version
            if json_version != actual_form_version:
                # If user didn't explicitly specify a version, use JSON version without warning
                if not form_version_is_explicit:
                    # User didn't specify a version, use JSON version
                    self.repository.update_node_fields(schema_id, version=json_version)
                    # Find and update the metadata child node as well
                    metadata_node = self.repository.get_node_by_parent_and_type(schema_id, child_type)
                    if metadata_node:
                        self.repository.update_node_fields(metadata_node.id, version=json_version)
                    return {}  # No warning, this is expected behavior

                # User specified a version in the form that differs from JSON
                # Log warning but respect user's choice
                warning_msg = (
                    f"Version mismatch for schema {root_node.key}: "
                    f"form version='{actual_form_version}', JSON version='{json_version}'. "
                    f"Using form version as specified by user."
                )
                logger.warning(warning_msg)

                # Update both root and metadata node to form version for consistency
                self.repository.update_node_fields(schema_id, version=actual_form_version)
                # Find and update the metadata child node as well
                metadata_node = self.repository.get_node_by_parent_and_type(schema_id, child_type)
                if metadata_node:
                    self.repository.update_node_fields(metadata_node.id, version=actual_form_version)

                return {
                    'type': 'version_mismatch',
                    'message': warning_msg,
                    'form_version': actual_form_version,
                    'json_version': json_version
                }

            # Form version matches JSON version or form was empty/default
            # Sync from JSON to ensure consistency
            self.repository.update_node_fields(schema_id, version=json_version)
            return {}  # No warning
    
    def _set_schema_metadata(self, schema_id: uuid.UUID, schema_key: str, schema_status: str):
        """Set key and status as attributes on the root node"""
        root_node = self.repository.get_node_by_id(schema_id)
        root_node_type = root_node.node_type

        # Get AttributeDefs for key and status
        key_attr_def = self.attribute_def_repository.get_attribute_def_by_node_type_json_key_variant(root_node_type, 'key', None)
        status_attr_def = self.attribute_def_repository.get_attribute_def_by_node_type_json_key_variant(root_node_type, 'status', None)

        # Set key attribute — create AttributeDef dynamically if the node type lacks one
        if not key_attr_def:
            string_type = self.attribute_def_repository.get_default_data_type()
            if string_type:
                key_attr_def, _ = self.attribute_def_repository.get_or_create_attribute_def(
                    root_node_type,
                    'key',
                    None,
                    {
                        'name': 'key',
                        'is_required': False,
                        'is_common': True,
                        'data_type': string_type,
                    }
                )
        if key_attr_def:
            self.repository.update_or_create_node_attribute(
                schema_id,
                key_attr_def,
                {'value_string': schema_key}
            )

        # Set status attribute
        if status_attr_def:
            self.repository.update_or_create_node_attribute(
                schema_id,
                status_attr_def,
                {'value_string': schema_status}
            )
    
    def _process_schema_structure(self, schema: dict, root_id: uuid.UUID, project_id: Optional[uuid.UUID], organization_id: Optional[uuid.UUID], strict_keys: bool = False, path: str = "$", dropped_attributes: Optional[list] = None):
        """Process the complete schema JSON structure and create child nodes recursively.

        Delegates the entire root object to _process_node_attributes, which contains
        the unified dispatch logic for all cases (scalars, collections, shorthand sections,
        and composition-inferred child nodes). Error paths address the root object's
        members relative to ``path`` (the envelope root key is not part of it).
        ``dropped_attributes`` is forwarded to _process_node_attributes as the
        shared out-list for validator-rejected attribute values.
        """
        root_key = next(iter(schema.keys())) if schema else None
        if not root_key:
            return

        root_obj = schema[root_key] if isinstance(schema[root_key], dict) else {}

        root_node = self.repository.get_node_by_id(root_id)
        root_node_type = root_node.node_type

        self._process_node_attributes(
            root_id,
            root_node_type,
            root_obj,
            project_id,
            organization_id,
            strict_keys=strict_keys,
            path=path,
            dropped_attributes=dropped_attributes,
        )

    def _resolve_child_type_for_item(self, item: dict, compositions):
        """Select the correct child NodeType for a JSON item from a set of candidate compositions.

        When multiple compositions share the same collection_key (e.g. a container
        node type and a widget node type are both valid children under 'children'),
        the item's discriminator attribute is used to discriminate:
          - The discriminator json_key comes from each candidate's NodeTypeVariant
            declaration via NodeTypeRepository.get_discriminator_attr.
          - Only when a candidate child type has NO NodeTypeVariant row at all do
            we fall back to the repository-level discriminator-attribute
            heuristic (get_discriminator_attribute_def).
          - If a child_type has a domain-constrained discriminator AttributeDef
            whose domain contains the item's discriminator value, that
            child_type wins.
          - If only one composition exists, it is used directly.
          - If no domain match is found, fall back to the first composition.
        """
        if len(compositions) == 1:
            return compositions[0].child_type

        for comp in compositions:
            ntv = self.node_type_repository.get_node_type_variant_by_node_type(
                comp.child_type
            )
            if ntv is not None:
                # discriminator_attr=None marks a props/inherited-variant node —
                # it cannot be discriminated from a sibling item.
                if not ntv.discriminator_attr:
                    continue
                discriminator_attr = self.attribute_def_repository.get_attribute_def_with_domain(
                    comp.child_type, ntv.discriminator_attr
                )
            else:
                # No variant configuration — legacy heuristic fallback.
                discriminator_attr = self.attribute_def_repository.get_discriminator_attribute_def(
                    comp.child_type
                )
            if not discriminator_attr or not discriminator_attr.domain_id:
                continue
            item_type_value = item.get(discriminator_attr.json_key)
            if item_type_value and self.attribute_def_repository.domain_item_exists(
                discriminator_attr.domain,
                item_type_value
            ):
                return comp.child_type

        return compositions[0].child_type

    def _process_collection(self, collection_key: str, items: list, parent_id: uuid.UUID, parent_node_type, project_id: Optional[uuid.UUID], organization_id: Optional[uuid.UUID], sort_order_offset: int = 0, strict_keys: bool = False, path: str = "$", dropped_attributes: Optional[list] = None):
        """Process a collection of items as child nodes using NodeTypeComposition.

        ``dropped_attributes`` is forwarded to _process_node_attributes as the
        shared out-list for validator-rejected attribute values.
        """
        # Find ALL valid child types for this parent type matching the collection_key
        compositions = list(
            self.composition_repository.get_compositions_by_parent_type_select_related(parent_node_type)
        )
        # Filter by collection_key if provided
        if collection_key:
            compositions = [c for c in compositions if c.collection_key == collection_key]

        if not compositions:
            # No composition declares this collection_key for the parent type —
            # refuse to guess child types by name (name-mangling is forbidden by
            # the Declarative Variant Model).
            raise SchemaValidationError(
                f"No NodeTypeComposition declares collection_key '{collection_key}' "
                f"for parent node type '{parent_node_type.name}'",
                code=ERR_INVALID_SCHEMA,
                path=f"{path}.{collection_key}",
            )

        # Pre-build field maps per child_type to avoid repeated DB queries
        field_maps = {}
        for comp in compositions:
            ct = comp.child_type
            if ct.id not in field_maps:
                attr_defs = self.attribute_def_repository.get_attribute_defs_by_node_type_variant_key(ct, None)
                field_maps[ct.id] = (ct, self._build_json_key_to_field_map(attr_defs))

        # Get max_children from composition to determine if this is a singleton slot
        max_children = compositions[0].max_children if compositions and hasattr(compositions[0], 'max_children') else None
        
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                if strict_keys:
                    raise self._invalid_collection_value_error(
                        parent_node_type, f"{collection_key}[{idx}]", item, path
                    )
                continue

            item_path = f"{path}.{collection_key}[{idx}]"

            # Validate conditional structures within the item
            for item_key, item_value in item.items():
                if isinstance(item_value, dict) and self._is_conditional_structure(item_value):
                    try:
                        validate_conditional_structure(item_value)
                    except Exception as e:
                        raise SchemaValidationError(
                            f"Invalid conditional structure for '{item_key}' in item {idx} of '{collection_key}': {str(e)}",
                            code=ERR_INVALID_SCHEMA,
                            path=f"{item_path}.{item_key}",
                        ) from e

            child_node_type = self._resolve_child_type_for_item(item, compositions)
            _, json_key_to_field_map = field_maps[child_node_type.id]

            # For singleton slots (max_children=1), use collection_key as node key.
            # This enables the SQL s7_build_node_json to join: ch.key = ntc.collection_key
            # For collections (max_children != 1), generate unique keys from item id or index.
            if max_children == 1:
                # Map JSON properties to Node fields (including natural attributes like version)
                node_fields = self._map_json_to_node_fields(item, json_key_to_field_map, child_node_type, sort_order_offset + idx, collection_key)
                # Override name/key with collection_key for singleton slots
                node_fields['name'] = collection_key
                node_fields['key'] = collection_key
            else:
                node_fields = self._map_json_to_node_fields(item, json_key_to_field_map, child_node_type, sort_order_offset + idx)
                # Collections: prefer explicit 'key' from JSON, then 'id', then generate
                json_key_val = item.get('key')
                json_id = item.get('id')
                if json_key_val:
                    node_fields['key'] = json_key_val
                    node_fields['name'] = json_key_val
                elif json_id:
                    node_fields['key'] = json_id
                    node_fields['name'] = json_id
                elif node_fields['key'] is None:
                    node_fields['key'] = f"{collection_key}_{idx}"
                    node_fields['name'] = f"{collection_key}_{idx}"

            # Extract key from node_fields to avoid duplicate argument
            node_key = node_fields.pop('key')
            child_node = self.repository.create_child_node(
                child_node_type,
                parent_id,
                node_key,
                project_id=project_id,
                organization_id=organization_id,
                **node_fields
            )

            # Process attributes for this node (recursion into children is handled inside)
            self._process_node_attributes(
                child_node.id,
                child_node_type,
                item,
                project_id,
                organization_id,
                strict_keys=strict_keys,
                path=item_path,
                dropped_attributes=dropped_attributes,
            )
    
    def _build_json_key_to_field_map(self, attr_defs):
        """Build a mapping from json_key to Node field names based on AttributeDefs"""
        # Node model fields that can be mapped from JSON
        node_fields = {'sort_order', 'name', 'key', 'version'}
        
        # Build map from AttributeDefs - map based on AttributeDef.name to Node field
        json_key_map = {}
        for attr_def in attr_defs:
            # natural_version AttributeDefs must write to node.version column
            if attr_def.data_type.name == 'natural_version':
                json_key_map[attr_def.json_key] = 'version'
                continue
            # display_order attrs read from node.sort_order at reconstruction time;
            # do NOT map the JSON value to node.sort_order (it would add 1 extra on output).
            if attr_def.data_type.name == 'display_order':
                continue
            if attr_def.name in node_fields:
                # If AttributeDef.name matches a Node field, map the json_key to that field
                json_key_map[attr_def.json_key] = attr_def.name
        
        return json_key_map
    
    def _map_json_to_node_fields(self, item: dict, json_key_map: dict, node_type, idx: int, json_key: Optional[str] = None) -> dict:
        """Map JSON properties to Node fields using the key map"""
        node_fields = {
            'sort_order': idx,
            'name': f'{node_type.name}_{idx}',
            'key': None,  # Will be generated if not in JSON
        }
        
        for json_key, field_name in json_key_map.items():
            if json_key in item and item[json_key] is not None:
                node_fields[field_name] = item[json_key]
        
        # Handle key assignment: if key exists in JSON, assign it to both key and name
        if 'key' in item and item['key']:
            node_fields['key'] = item['key']
            node_fields['name'] = item['key']
        elif node_fields['key'] is None:
            # Use provided json_key for structural matches (e.g., 'props' instead of '<props_type>_0')
            if json_key:
                node_fields['key'] = json_key
                node_fields['name'] = json_key
            elif 'name' in item and item['name']:
                node_fields['key'] = item['name']
            elif 'id' in item and item['id']:
                node_fields['key'] = str(item['id'])
            else:
                # Generate a unique key based on node type and index
                node_fields['key'] = f'{node_type.name}_{idx}'
        
        return node_fields
    
    def _process_single_child_node(self, parent_id: uuid.UUID, json_key: str, value: dict, child_type, project_id: Optional[uuid.UUID] = None, organization_id: Optional[uuid.UUID] = None, index: Optional[int] = None, sort_order: int = 0, inherited_variant: Optional[str] = None, strict_keys: bool = False, path: str = "$", dropped_attributes: Optional[list] = None):
        """Process a single child node from a dict value (composition without collection_key).

        ``dropped_attributes`` is forwarded to _process_node_attributes as the
        shared out-list for validator-rejected attribute values.
        """
        # JSON path of this child node within the payload (indexed when the
        # child comes from a list slot).
        child_path = f"{path}.{json_key}" if index is None else f"{path}.{json_key}[{index}]"

        # Validate conditional structure if value has conditional structure
        if self._is_conditional_structure(value):
            try:
                validate_conditional_structure(value)
            except Exception as e:
                raise SchemaValidationError(
                    f"Invalid conditional structure for '{json_key}': {str(e)}",
                    code=ERR_INVALID_SCHEMA,
                    path=child_path,
                ) from e

        # Generate unique name/key based on index
        if index is not None:
            name = f'{json_key}_{index}'
            key = f'{json_key}_{index}'
        else:
            name = json_key
            key = json_key

        # Build map of natural attributes to node fields
        attr_defs = self.attribute_def_repository.get_attribute_defs_by_node_type(child_type)
        json_key_map = self._build_json_key_to_field_map(attr_defs)
        
        # Map JSON properties to Node fields (including natural attributes like version)
        node_fields = self._map_json_to_node_fields(value, json_key_map, child_type, sort_order, json_key)
        
        # Override name/key with generated values if not set by mapping
        # For structural matches (no collection_key), always use json_key as the node key
        # to preserve original key names like 'props' instead of 'sdui_props_0'
        if 'name' not in node_fields or not node_fields['name']:
            node_fields['name'] = name
        if 'key' not in node_fields or not node_fields['key']:
            node_fields['key'] = key
        node_fields['sort_order'] = sort_order

        # Create child node
        node_key = node_fields.pop('key')
        child_node = self.repository.create_child_node(
            child_type,
            parent_id,
            node_key,
            project_id=project_id,
            organization_id=organization_id,
            **node_fields
        )

        # Generic: if child_type has an AttributeDef with json_key 'usage', set it to the parent json_key
        # This allows mapping show_if -> condition_group with usage='show_if' without hardcoding
        usage_attr_def = self.attribute_def_repository.get_attribute_def_by_node_type_json_key_variant(
            child_type,
            'usage',
            None
        )
        if usage_attr_def:
            self._set_node_attribute(
                child_node.id,
                'usage',
                json_key,
                child_type,
                inherited_variant,
                strict_keys=strict_keys,
                path=child_path,
            )

        # Process the dict as attributes and nested structures.
        # For props nodes, pass inherited_variant to _process_node_attributes
        # so it can resolve variant-scoped AttributeDefs (keyed by the parent's
        # variant). The discriminator key is resolved from the ACTUAL parent's
        # NodeTypeVariant — several parent types may share one props node type
        # with different discriminators.
        if inherited_variant:
            parent_node = self.repository.get_node_by_id_with_node_type(parent_id)
            parent_discriminator = (
                self.node_type_repository.get_discriminator_attr(parent_node.node_type)
                if parent_node is not None else None
            )
            if parent_discriminator and parent_discriminator not in value:
                # Inject discriminator for variant resolution in _process_node_attributes,
                # but it should NOT be stored as a NodeAttribute.
                value = dict(value, **{parent_discriminator: inherited_variant})
        self._process_node_attributes(
            child_node.id,
            child_type,
            value,
            project_id,
            organization_id,
            inherited_variant,
            strict_keys=strict_keys,
            path=child_path,
            dropped_attributes=dropped_attributes,
        )

    def _set_node_attribute(self, node_id: uuid.UUID, json_key: str, value, node_type, variant_key: Optional[str] = None, strict_keys: bool = False, path: str = "$"):
        """Set a node attribute value, creating AttributeDef if needed"""
        # Disable triggers to bypass s7 validation
        self.repository.disable_triggers()

        try:
            # Determine data type from the value shape
            if isinstance(value, bool):
                data_type_name = 'bool'
            elif isinstance(value, (int, float)):
                data_type_name = 'number'
            elif isinstance(value, str):
                data_type_name = 'string'
            elif isinstance(value, (list, dict)):
                data_type_name = 'json'
            else:
                data_type_name = 'string'

            data_type = self.data_type_repository.get_data_type_by_name(data_type_name)
            if not data_type:
                data_type = self.data_type_repository.get_string_data_type()

            if not data_type:
                return

            # Validate conditional structure if value has conditional shape, so we can
            # create a conditional AttributeDef when appropriate.
            if self._is_conditional_structure(value):
                try:
                    validate_conditional_structure(value)
                except Exception as e:
                    raise SchemaValidationError(
                        f"Invalid conditional structure for '{json_key}': {str(e)}",
                        code=ERR_INVALID_SCHEMA,
                        path=f"{path}.{json_key}",
                    ) from e

            # Decide whether to create as universal or variant-specific
            final_variant_key = self._determine_variant_key(node_type, json_key, variant_key)

            # Check if an AttributeDef already exists with the determined variant_key
            attr_def = self.attribute_def_repository.get_attribute_def_by_node_type_json_key_variant(
                node_type,
                json_key,
                variant_key=final_variant_key
            )

            if not attr_def:
                # Strict mode: the key must be declared for this node type and
                # applicable to the node's active variant (variant_key param).
                declaration = None
                if strict_keys:
                    declaration = self.attribute_def_repository.get_attribute_def_declaration(
                        node_type,
                        json_key,
                        variant_key
                    )
                    if declaration is None:
                        raise self._undeclared_key_error(node_type, json_key, variant_key, path)
                # Create AttributeDef with the determined variant_key,
                # inheriting the declaration's domain so it keeps enforcing
                # after materialization.
                is_common = (final_variant_key is None)
                attr_def = self.attribute_def_repository.create_attribute_def(
                    name=json_key,
                    json_key=json_key,
                    is_required=False,
                    is_common=is_common,
                    data_type=data_type,
                    node_type=node_type,
                    variant_key=final_variant_key,
                    domain=declaration.domain if declaration else None,
                )

            if attr_def and value is not None:
                # Run any datatype-specific plugin validators (e.g. conditional).
                try:
                    validate_datatype_value(attr_def.data_type, value)
                except Exception as e:
                    raise SchemaValidationError(
                        f"Invalid value for '{json_key}': {str(e)}",
                        code=ERR_INVALID_SCHEMA,
                        path=f"{path}.{json_key}",
                    ) from e

                # Strict mode: scalar values must belong to the declared domain.
                if strict_keys and isinstance(value, (str, int, float, bool)):
                    check_domain = self._effective_domain(
                        node_type, json_key, variant_key, attr_def
                    )
                    if check_domain and not self.attribute_def_repository.domain_item_exists(
                        check_domain, self._domain_lookup_value(value)
                    ):
                        raise self._invalid_domain_value_error(json_key, check_domain, value, path)

                # Store the value in the column declared by primary_storage_type.
                defaults = get_storage_defaults(attr_def.data_type, value)
                self.repository.update_or_create_node_attribute(node_id, attr_def, defaults)
        finally:
            # Re-enable triggers
            self.repository.enable_triggers()
    
    def _process_keyed_map(self, node_id: uuid.UUID, node_type, entries: dict, compositions, project_id: Optional[uuid.UUID], organization_id: Optional[uuid.UUID], strict_keys: bool = False, path: str = "$", dropped_attributes: Optional[list] = None) -> None:
        """Store arbitrary JSON object keys as individually editable child nodes.

        Map keys are data (map semantics), so strict mode does not reject
        undeclared keys here; strict checks apply to each entry's attributes
        via the recursive _process_node_attributes call.
        ``dropped_attributes`` is forwarded to _process_node_attributes as the
        shared out-list for validator-rejected attribute values.
        """
        candidates = [comp.child_type for comp in compositions if comp.collection_key is None]
        typed_objects = [
            (candidate, {
                attr.json_key for attr in self.attribute_def_repository.get_attribute_defs_by_node_type_variant_key(candidate, None)
                if attr.is_required
            })
            for candidate in candidates if not candidate.is_json_map and not candidate.json_scalar_attribute
        ]
        object_map = next((candidate for candidate in candidates if candidate.is_json_map), None)
        scalar = next((candidate for candidate in candidates if candidate.json_scalar_attribute), None)
        for index, (key, value) in enumerate(entries.items()):
            entry_path = f"{path}.{key}"
            if not isinstance(key, str) or not key or len(key) > 255:
                raise SchemaValidationError(
                    'Map keys must be non-empty strings of at most 255 characters',
                    code=ERR_INVALID_SCHEMA,
                    path=entry_path,
                )
            if isinstance(value, dict):
                child_type = next((candidate for candidate, required in typed_objects if required and required <= value.keys()), object_map)
            else:
                child_type = scalar
            if child_type is None:
                raise SchemaValidationError(
                    f'Unsupported value for key {key!r} in {node_type.name}',
                    code=ERR_INVALID_SCHEMA,
                    path=entry_path,
                )
            child = self.repository.create_child_node(
                child_type, node_id, key, name=key, sort_order=index,
                project_id=project_id, organization_id=organization_id,
            )
            child_attributes = value if isinstance(value, dict) else {child_type.json_scalar_attribute: value}
            self._process_node_attributes(
                child.id,
                child_type,
                child_attributes,
                project_id,
                organization_id,
                strict_keys=strict_keys,
                path=entry_path,
                dropped_attributes=dropped_attributes,
            )

    def _process_node_attributes(self, node_id: uuid.UUID, node_type, attributes: dict, project_id: Optional[uuid.UUID] = None, organization_id: Optional[uuid.UUID] = None, inherited_variant: Optional[str] = None, strict_keys: bool = False, path: str = "$", dropped_attributes: Optional[list] = None):
        """Process attributes for a node, creating AttributeDefs dynamically if needed.

        Dispatch priority for each json_key in attributes:
          1. List value whose key matches a composition collection_key -> _process_collection
             Also: dict value whose key matches a collection_key with max_children=1 -> wrapped as [value]
          2. Dict/list value whose key is a domain-discriminator shorthand for a collection
             (e.g. 'body' -> screen_section with section_key='body') -> create child node
          3. Dict/list value whose key matches a structural child's declared key
             (composition without collection_key; child_type.default_json_key or
             child_type.name) -> _process_single_child_node
          4. Scalar/dict value with no composition match -> store as NodeAttribute

        Args:
            inherited_variant: Variant key inherited from the parent node
                (for child node types declared as a parent's props_node_type)
            strict_keys: Reject undeclared JSON keys / invalid collection and
                domain values with SchemaValidationError (validate dry-run).
            path: JSON path of this node's attributes object within the
                payload; errors are reported as ``{path}.{json_key}``.
            dropped_attributes: Optional out-list collecting
                ``{"code", "path", "message"}`` dicts for attribute values
                rejected by datatype validators in permissive mode; under
                ``strict_keys`` those failures raise instead.
        """
        # Get Node model field names (these are not attributes)
        node_field_names = self.repository.get_node_field_names()

        compositions = self.composition_repository.get_compositions_by_parent_type_select_related(node_type)
        if getattr(node_type, 'is_json_map', False) is True:
            self._process_keyed_map(
                node_id,
                node_type,
                attributes,
                compositions,
                project_id,
                organization_id,
                strict_keys=strict_keys,
                path=path,
                dropped_attributes=dropped_attributes,
            )
            return
        
        # Case 1 — collection keys (list or singleton dict values)
        collection_keys = {comp.collection_key for comp in compositions if comp.collection_key}
        # Map collection_key -> composition for max_children lookup (singleton dict dispatch)
        collection_key_to_comp = {comp.collection_key: comp for comp in compositions if comp.collection_key}
        
        # Case 2 — shorthand discriminator map:
        #   For compositions with collection_key whose child type has a required domain-constrained
        #   AttributeDef, map each domain value -> (child_type, discriminator_json_key).
        #   Example: screen -> screen_section (sections), section_key in ScreenSectionKey
        #   -> {'body': (screen_section, 'section_key'), 'appbar': (screen_section, 'section_key'), ...}
        shorthand_map = {}  # domain_value -> (child_type, discriminator_json_key)
        for comp in compositions:
            if not comp.collection_key:
                continue
            discriminator = self.attribute_def_repository.get_discriminator_attribute_def(comp.child_type)
            if not discriminator:
                continue
            for di in self.attribute_def_repository.get_domain_items_by_domain(discriminator.domain):
                shorthand_map[di.value] = (comp.child_type, discriminator.json_key)
        
        # Case 3 — structural children: compositions without collection_key
        #   (e.g. 'props' -> props node type, 'layout' -> layout node type).
        # Declarative matching only: a json_key dispatches to a structural child
        # type when it equals the child type's declared NodeType.default_json_key
        # or the child type's own name. No name-mangling or shape heuristics —
        # the key→type mapping must be declared in the catalog.
        json_key_to_child_type = {}
        for comp in compositions:
            if comp.collection_key is not None:
                continue
            child_type = comp.child_type
            structural_keys = {child_type.name}
            if child_type.default_json_key:
                structural_keys.add(child_type.default_json_key)
            for json_key, value in attributes.items():
                if json_key in node_field_names or json_key in collection_keys or json_key in shorthand_map:
                    continue
                if json_key not in structural_keys:
                    continue
                if isinstance(value, dict):
                    json_key_to_child_type[json_key] = child_type
                elif isinstance(value, list) and value and isinstance(value[0], dict):
                    json_key_to_child_type[json_key] = child_type
        
        # Disable triggers to bypass s7 validation
        self.repository.disable_triggers()

        try:
            # Determine the node's variant from the NodeTypeVariant declaration.
            # No variant row => the node type has no variants at all (no fallback
            # to a hardcoded discriminator key).
            ntv = self.node_type_repository.get_node_type_variant_by_node_type(node_type)
            if ntv is None:
                node_variant = None
            elif ntv.discriminator_attr is None:
                # Props/inherited-variant node: the variant comes from the
                # parent node that declared this type as its props_node_type.
                node_variant = inherited_variant
            else:
                node_variant = attributes.get(ntv.discriminator_attr)

            # For props children, the actual parent's discriminator key may have
            # been injected for variant resolution — it must never be stored as
            # a NodeAttribute. Resolve it once via the real parent node.
            injected_discriminator = None
            if inherited_variant:
                this_node = self.repository.get_node_by_id_with_parent(node_id)
                if this_node and this_node.parent_id:
                    injected_discriminator = self.node_type_repository.get_discriminator_attr(
                        this_node.parent.node_type
                    )

            # Counter to assign unique, sequential sort_orders to all child nodes
            # created by any dispatch path within this call.
            child_sort_order = 0

            # Process ALL attributes (no filtering of empty/null/zero values)
            for json_key, value in attributes.items():
                try:
                    # Skip Node model fields (handled separately), except sort_order which
                    # may be expressed explicitly in JSON and must be stored as an attribute
                    # when no display_order AttributeDef is present on this node type.
                    if json_key in node_field_names and json_key != 'sort_order':
                        continue
                    # Skip natural type attributes (natural_version, natural_key, etc.)
                    # These are mapped to node columns and handled separately
                    attr_def = self.attribute_def_repository.get_attribute_def_by_node_type_json_key_variant(
                        node_type,
                        json_key,
                        None
                    )
                    if attr_def and attr_def.data_type.name.startswith('natural_'):
                        continue
                    if json_key == 'sort_order':
                        # If a display_order AttributeDef exists, skip: the value is driven by
                        # node.sort_order (loop index) and reconstructed as sort_order+1 in SQL.
                        has_display_order = self.attribute_def_repository.attribute_def_exists_with_display_order(
                            node_type,
                            'sort_order'
                        )
                        if has_display_order:
                            continue

                    # Skip the injected parent-discriminator key on props nodes
                    if injected_discriminator and json_key == injected_discriminator:
                        continue

                    # Case 1: value whose key matches a composition collection_key
                    if json_key in collection_keys:
                        if isinstance(value, list):
                            self._process_collection(json_key, value, node_id, node_type, project_id, organization_id, sort_order_offset=child_sort_order, strict_keys=strict_keys, path=path, dropped_attributes=dropped_attributes)
                            child_sort_order += len(value)
                        elif isinstance(value, dict):
                            # Singleton dict: only dispatch if composition allows exactly one child (max_children=1)
                            comp = collection_key_to_comp.get(json_key)
                            if comp and comp.max_children == 1:
                                # Inject discriminator value if this slot has a shorthand mapping
                                # (e.g. section_key='body' for the 'body' slot of screen_section)
                                if json_key in shorthand_map:
                                    _, discriminator_json_key = shorthand_map[json_key]
                                    item = dict(value)
                                    item.setdefault(discriminator_json_key, json_key)
                                else:
                                    item = value
                                self._process_collection(json_key, [item], node_id, node_type, project_id, organization_id, sort_order_offset=child_sort_order, strict_keys=strict_keys, path=path, dropped_attributes=dropped_attributes)
                                child_sort_order += 1
                            elif strict_keys:
                                raise self._invalid_collection_value_error(node_type, json_key, value, path)
                        elif strict_keys:
                            raise self._invalid_collection_value_error(node_type, json_key, value, path)
                        continue

                    # Case 2: shorthand discriminator (e.g. 'body' dict -> screen_section node)
                    if json_key in shorthand_map and isinstance(value, dict):
                        child_type, discriminator_json_key = shorthand_map[json_key]
                        name = value.get('id') or json_key
                        child_node = self.repository.create_child_node(
                            child_type,
                            node_id,
                            json_key,
                            project_id=project_id,
                            organization_id=organization_id,
                            sort_order=child_sort_order,
                            name=name,
                        )
                        child_sort_order += 1
                        item_with_discriminator = dict(value)
                        item_with_discriminator.setdefault(discriminator_json_key, json_key)
                        self._process_node_attributes(child_node.id, child_type, item_with_discriminator, project_id, organization_id, strict_keys=strict_keys, path=f"{path}.{json_key}", dropped_attributes=dropped_attributes)
                        continue

                    # Case 3: structural match -> composition without collection_key
                    # (e.g. a props child node, or other named sub-structures)
                    if json_key in json_key_to_child_type:
                        child_type = json_key_to_child_type[json_key]
                        # For props node types (declared via NodeTypeVariant.props_node_type),
                        # pass the parent's variant so the child can resolve
                        # variant-scoped AttributeDefs (keyed by the parent's variant).
                        child_inherited_variant = (
                            node_variant
                            if self.node_type_repository.is_props_node_type(child_type)
                            else None
                        )
                        if isinstance(value, dict):
                            self._process_single_child_node(node_id, json_key, value, child_type, project_id, organization_id, index=None, sort_order=child_sort_order, inherited_variant=child_inherited_variant, strict_keys=strict_keys, path=path, dropped_attributes=dropped_attributes)
                            child_sort_order += 1
                        elif isinstance(value, list):
                            for idx, item in enumerate(value):
                                if isinstance(item, dict):
                                    self._process_single_child_node(node_id, json_key, item, child_type, project_id, organization_id, index=idx, sort_order=child_sort_order, inherited_variant=child_inherited_variant, strict_keys=strict_keys, path=path, dropped_attributes=dropped_attributes)
                                    child_sort_order += 1
                        continue

                    # Handle nested objects: if value is a dict with a single key that matches a pattern like {field: "x"} or {value: "y"}
                    # Expand it to flat attributes: {left_kind: "field", left_field_key: "x"}
                    if isinstance(value, dict) and len(value) == 1:
                        kind_key = list(value.keys())[0]
                        kind_value = value[kind_key]
                        # Check if there are AttributeDefs that match the pattern {json_key}_kind and {json_key}_{kind_key}_key or {json_key}_{kind_key}
                        kind_attr_def = self.attribute_def_repository.get_attribute_def_by_node_type_json_key_variant(
                            node_type,
                            f'{json_key}_kind',
                            None
                        )
                        if kind_attr_def:
                            # This is a nested object that should be expanded
                            # Set the kind attribute
                            self._set_node_attribute(node_id, f'{json_key}_kind', kind_key, node_type, node_variant, strict_keys=strict_keys, path=path)
                            # Set the value attribute based on the kind
                            # Try both patterns: {json_key}_{kind_key}_key (for field) and {json_key}_{kind_key} (for value)
                            value_attr_def = self.attribute_def_repository.get_attribute_def_by_node_type_json_key_variant(
                                node_type,
                                f'{json_key}_{kind_key}_key',
                                None
                            )
                            if not value_attr_def:
                                value_attr_def = self.attribute_def_repository.get_attribute_def_by_node_type_json_key_variant(
                                    node_type,
                                    f'{json_key}_{kind_key}',
                                    None
                                )
                            if value_attr_def:
                                self._set_node_attribute(node_id, value_attr_def.json_key, kind_value, node_type, node_variant, strict_keys=strict_keys, path=path)
                            continue

                    # Try to find existing AttributeDef
                    # First, try the variant-specific one (based on 'type' attribute)
                    attr_def = None
                    if node_variant:
                        attr_def = self.attribute_def_repository.get_attribute_def_by_node_type_json_key_variant(
                            node_type,
                            json_key,
                            node_variant
                        )

                    # If not found, try the generic one (variant_key=None, is_common=True only)
                    # Catalog entries (is_common=False) are templates, not real properties
                    if not attr_def:
                        attr_def = self.attribute_def_repository.get_attribute_def_by_node_type_json_key_variant_common(
                            node_type,
                            json_key,
                            None,
                            True
                        )

                    # If not found, create AttributeDef dynamically
                    if not attr_def:
                        attr_def = self._create_attribute_def(node_type, json_key, value, node_variant, strict_keys=strict_keys, path=path)

                    if attr_def:
                        # Validate conditional structures recursively in the value
                        # Do this before the try-except to ensure validation errors propagate
                        if isinstance(value, (list, dict)):
                            try:
                                self._validate_conditional_recursively(value, json_key)
                            except Exception as e:
                                raise ValueError(f"Invalid conditional structure for '{json_key}': {str(e)}")

                        # Strict mode: scalar values must belong to the declared domain.
                        if (strict_keys and value is not None
                                and isinstance(value, (str, int, float, bool))):
                            check_domain = self._effective_domain(
                                node_type, json_key, node_variant, attr_def
                            )
                            if check_domain and not self.attribute_def_repository.domain_item_exists(
                                check_domain, self._domain_lookup_value(value)
                            ):
                                raise self._invalid_domain_value_error(json_key, check_domain, value, path)

                        # Set the attribute value using direct ORM to bypass s7 validation
                        if value is None:
                            # Skip None values (no attribute row needed)
                            continue

                        # Run datatype-specific plugin validators (e.g. conditional
                        # structures). Strict mode turns a rejection into
                        # SchemaValidationError (same shape as _set_node_attribute);
                        # permissive mode records a dropped_attribute warning and
                        # moves on to the next key — nothing is silently dropped.
                        try:
                            validate_datatype_value(attr_def.data_type, value)
                        except (ValidationError, SchemaValidationError) as exc:
                            if strict_keys:
                                raise SchemaValidationError(
                                    f"Invalid value for '{json_key}': {exc}",
                                    code=ERR_INVALID_SCHEMA,
                                    path=f"{path}.{json_key}",
                                ) from exc
                            if dropped_attributes is not None:
                                dropped_attributes.append(
                                    {
                                        "code": WARN_DROPPED_ATTRIBUTE,
                                        "path": f"{path}.{json_key}",
                                        "message": str(exc),
                                    }
                                )
                            else:
                                # Callers without the out-list (e.g. direct
                                # import_schema calls) still get a trace —
                                # nothing is dropped silently.
                                logging.getLogger(__name__).warning(
                                    "Dropped attribute %s.%s: %s",
                                    path, json_key, exc,
                                )
                            continue

                        # Store the value in the column declared by
                        # primary_storage_type. Storage/persistence failures are
                        # real bugs: they propagate to the per-key ValueError
                        # wrapper (or unwrapped for RuntimeError/DatabaseError).
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            from decimal import Decimal
                            value = Decimal(str(value))
                        defaults = get_storage_defaults(attr_def.data_type, value)
                        self.repository.update_or_create_node_attribute(
                            node_id,
                            attribute_def=attr_def,
                            defaults=defaults
                        )
                except SchemaValidationError:
                    raise
                except ValueError as exc:
                    # Any pipeline error inherits the path of the key being
                    # dispatched, even when the raise site is not instrumented.
                    # RuntimeError (infrastructure) propagates unwrapped so
                    # callers keep mapping it to 5xx rather than invalid_schema.
                    raise SchemaValidationError(
                        str(exc),
                        code=ERR_INVALID_SCHEMA,
                        path=f"{path}.{json_key}",
                    ) from exc
        finally:
            # Re-enable triggers
            self.repository.enable_triggers()

    def _create_attribute_def(self, node_type, json_key: str, value, variant_key=None, strict_keys: bool = False, path: str = "$"):
        """Create an AttributeDef dynamically based on the value type

        New properties are created as variant-specific by default (for the current node variant).
        Only create as universal if:
        1. A universal version already exists (is_common=True, variant_key=None)
        2. A catalog template exists (is_common=False, variant_key=None) - then convert to universal

        Under ``strict_keys``, keys with no AttributeDef declaration for the
        node type and the node's active variant are rejected instead of
        auto-creating catalog entries, and non-collection values on declared
        collection keys raise instead of being silently dropped.
        """
        # Check if json_key corresponds to a collection_key in NodeTypeComposition
        composition_exists = self.composition_repository.composition_exists_by_collection_key(
            node_type,
            json_key
        )
        if composition_exists:
            if strict_keys:
                raise self._invalid_collection_value_error(node_type, json_key, value, path)
            return None

        # Strict mode: the key must be declared for this node type and
        # applicable to the node's active variant before auto-creating an
        # AttributeDef in the shared catalog. Node field names (e.g.
        # 'sort_order' — the only Node column allowed to reach attribute
        # processing) count as structurally declared.
        declaration = None
        if strict_keys:
            declaration = self.attribute_def_repository.get_attribute_def_declaration(
                node_type,
                json_key,
                variant_key
            )
            if declaration is None and json_key not in self.repository.get_node_field_names():
                raise self._undeclared_key_error(node_type, json_key, variant_key, path)
            # The resolved declaration may be a catalog template whose domain
            # is enforced here so out-of-domain scalars are rejected before
            # creation; the domain is also copied below so the materialized
            # def keeps enforcing it afterwards.
            if (declaration is not None and declaration.domain
                    and isinstance(value, (str, int, float, bool))
                    and not self.attribute_def_repository.domain_item_exists(
                        declaration.domain, self._domain_lookup_value(value)
                    )):
                raise self._invalid_domain_value_error(json_key, declaration.domain, value, path)

        # Determine data type from value
        data_type_name = self._infer_data_type(value)
        data_type = self.data_type_repository.get_data_type_by_name(data_type_name)

        if not data_type:
            # Fallback to string type
            data_type = self.data_type_repository.get_string_data_type()

        if not data_type:
            return None

        # Validate conditional structure if value has conditional structure
        if self._is_conditional_structure(value):
            try:
                validate_conditional_structure(value)
            except Exception as e:
                raise SchemaValidationError(
                    f"Invalid conditional structure for '{json_key}': {str(e)}",
                    code=ERR_INVALID_SCHEMA,
                    path=f"{path}.{json_key}",
                ) from e

        # Decide whether to create as universal or variant-specific
        final_variant_key = self._determine_variant_key(node_type, json_key, variant_key)

        # Check if an AttributeDef already exists with the determined variant_key
        existing = self.attribute_def_repository.get_attribute_def_by_node_type_json_key_variant(
            node_type,
            json_key,
            final_variant_key
        )

        if existing:
            return existing

        # Create AttributeDef with the determined variant_key, inheriting
        # the declaration's domain so the materialized def keeps enforcing
        # it on subsequent writes.
        is_common = (final_variant_key is None)
        attr_def = self.attribute_def_repository.create_attribute_def(
            name=json_key,
            json_key=json_key,
            is_required=False,
            is_common=is_common,
            data_type=data_type,
            node_type=node_type,
            variant_key=final_variant_key,
            domain=declaration.domain if declaration else None,
        )

        return attr_def

    def _undeclared_key_error(self, node_type, json_key: str, variant_key: Optional[str], path: str) -> SchemaValidationError:
        """Build the structured error for a JSON key not declared on a node type.

        ``variant_key`` is the node's active variant; when the key is declared
        only under other variants, those variants are reported in the message
        to help fix the payload or the catalog.
        """
        expected = f"attribute declared on node type '{node_type.name}'"
        message = f"JSON key '{json_key}' is not declared on node type '{node_type.name}'"
        if variant_key:
            expected += f" for variant '{variant_key}'"
            message += f" for variant '{variant_key}'"
        declared_variants = [
            vk
            for vk in self.attribute_def_repository.get_variant_keys_for_json_key(node_type, json_key)
            if vk is not None and vk != variant_key
        ]
        if declared_variants:
            message += f" (declared for variants: {declared_variants})"
        return SchemaValidationError(
            message,
            code=ERR_UNDECLARED_JSON_KEY,
            path=f"{path}.{json_key}",
            expected=expected,
            received=json_key,
        )

    def _invalid_collection_value_error(self, node_type, json_key: str, value, path: str) -> SchemaValidationError:
        """Build the structured error for a non-collection value on a collection_key."""
        return SchemaValidationError(
            f"Key '{json_key}' is declared as a child collection on node type '{node_type.name}'",
            code=ERR_INVALID_COLLECTION_VALUE,
            path=f"{path}.{json_key}",
            expected="array of objects (or object for a singleton slot)",
            received=type(value).__name__,
        )

    @staticmethod
    def _domain_lookup_value(value) -> str:
        """Coerce a scalar to the string form stored in ``DomainItem.value``.

        ``value`` is a CharField: bools are stored lowercase ('true'/'false'),
        so passing the raw Python value would miss the lookup.
        """
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _effective_domain(self, node_type, json_key: str, variant_key: Optional[str], attr_def):
        """Resolve the domain that governs strict scalar checks for an attribute.

        Materialized defs may carry no ``domain`` (created before the catalog
        template declared one, or via a permissive import), so fall back to
        the domain-bearing AttributeDef declaration for this node type and
        variant. The fallback only considers declarations with a non-NULL
        ``domain``: a stale variant-scoped def without one must not resolve
        as its own declaration and shadow the template's domain.
        """
        if attr_def.domain:
            return attr_def.domain
        declaration = self.attribute_def_repository.get_domain_declaration(
            node_type, json_key, variant_key
        )
        return declaration.domain if declaration else None

    def _invalid_domain_value_error(self, json_key: str, domain, value, path: str) -> SchemaValidationError:
        """Build the structured error for a scalar value outside its declared domain."""
        return SchemaValidationError(
            f"Value {value!r} for '{json_key}' is not a member of domain '{domain.domain_name}'",
            code=ERR_INVALID_DOMAIN_VALUE,
            path=f"{path}.{json_key}",
            expected=f"one of the '{domain.domain_name}' domain values",
            received=value,
        )

    def _determine_variant_key(self, node_type, json_key: str, requested_variant_key) -> Optional[str]:
        """Determine whether a property should be universal (None) or variant-specific

        Logic:
        1. If there's already a universal version with is_common=True -> return None (use universal)
        2. Otherwise -> return requested_variant_key (variant-specific)
           New properties are variant-specific by default to match seed behavior.
           Catalog entries (is_common=False) are just templates, not real properties.
        """
        # Check if a real universal version already exists
        real_universal_exists = self.attribute_def_repository.universal_attribute_def_exists(
            node_type,
            json_key
        )

        if real_universal_exists:
            return None

        # No real universal version exists - create as variant-specific by default
        return requested_variant_key
    
    def _is_conditional_structure(self, value) -> bool:
        """Check if a value has the structure of a conditional expression."""
        if not isinstance(value, dict):
            return False
        return 'logic' in value and 'conditions' in value
    
    def _validate_conditional_recursively(self, value, path: str = ""):
        """Recursively validate all conditional structures in a value."""
        if isinstance(value, dict):
            # Check if this dict itself is a conditional structure
            if self._is_conditional_structure(value):
                try:
                    validate_conditional_structure(value)
                except Exception as e:
                    raise ValueError(f"Invalid conditional structure at '{path}': {str(e)}")
            # Recursively validate nested values
            for key, nested_value in value.items():
                nested_path = f"{path}.{key}" if path else key
                self._validate_conditional_recursively(nested_value, nested_path)
        elif isinstance(value, list):
            # Recursively validate list items
            for idx, item in enumerate(value):
                nested_path = f"{path}[{idx}]"
                self._validate_conditional_recursively(item, nested_path)
    
    def _infer_data_type(self, value) -> str:
        """Infer DataType from value"""
        if isinstance(value, bool):
            return 'bool'
        elif isinstance(value, int):
            return 'int'
        elif isinstance(value, float):
            return 'number'
        elif isinstance(value, str):
            return 'string'
        elif isinstance(value, list):
            return 'list_string'
        elif isinstance(value, dict):
            # Check if this is a conditional structure
            if self._is_conditional_structure(value):
                return 'conditional'
            return 'json'
        else:
            return 'string'
    
    def get_publish_blockers(self, node: Node) -> Optional[dict]:
        """Check whether a schema node can be published.

        Business rule: all required properties must have values before a
        schema may be published. Returns a payload describing the missing
        required properties per node, or None when the schema is complete
        and publishable.
        """
        warnings = SchemaValidationService().collect_required_warnings(node.id)
        if not warnings:
            return None
        warning_details = [
            f"{w['node_name']} ({w['node_type']}): missing {', '.join(w['missing'])}"
            for w in warnings
        ]
        return {
            "error": ERR_INCOMPLETE_SCHEMA,
            "detail": f"Cannot publish: {len(warnings)} node(s) have missing required properties",
            "warnings": warnings,
            "message": "Complete all required properties before publishing. " + "; ".join(warning_details),
        }

    def get_available_actions(self, node: Node) -> dict:
        """Lifecycle transition rules for a schema node.

        Determines which lifecycle actions are available for a root schema
        node based on its status attribute and build state:

        - 'edit': always, unless the schema is archived (read-only)
        - 'build': when the build state reports uncommitted changes (dirty)
        - 'publish': when in draft status with no missing required properties
        - 'publish_blocked': when in draft status but required properties are
          missing (``blocked_nodes`` reports how many nodes are incomplete)
        - 'archive': when in published status
        - 'draft': when in archived status

        Returns:
            dict with keys: actions (list[str]), key, version,
            blocked_nodes (int).
        """
        status = self._get_schema_attribute(node, 'status')
        key = self._get_schema_attribute(node, 'key')
        version = node.version

        actions = []
        if status != STATUS_ARCHIVED:
            actions.append('edit')

        build_state = self.repository.get_build_state(key, version) if key and version else None
        if build_state and build_state.dirty:
            actions.append('build')

        blocked_nodes = 0
        if status == STATUS_DRAFT:
            blockers = self.get_publish_blockers(node)
            if blockers:
                actions.append('publish_blocked')
                blocked_nodes = len(blockers["warnings"])
            else:
                actions.append('publish')
        elif status == STATUS_PUBLISHED:
            actions.append('archive')
        elif status == STATUS_ARCHIVED:
            actions.append('draft')

        return {
            "actions": actions,
            "key": key,
            "version": version,
            "blocked_nodes": blocked_nodes,
        }

    def publish_schema_by_id(self, node_id) -> Optional[dict]:
        """Resolve a node by ID, enforce the publish gates and publish.

        Shared publish orchestration for admin views: coerces the raw ID,
        loads the node with its node_type, applies the missing-required
        rule (get_publish_blockers) and the root-node rule, then delegates
        to publish_schema.

        Args:
            node_id: raw node identifier (str or UUID)

        Returns:
            The blockers payload dict when the schema is incomplete,
            otherwise None (publish succeeded).

        Raises:
            ValueError: node_id is not a valid UUID or the node is not a
                root node.
            LookupError: no node exists for the given ID.
        """
        try:
            node_id = uuid.UUID(node_id) if isinstance(node_id, str) else node_id
            node = self.repository.get_node_by_id_with_node_type(node_id)
        except (ValidationError, ValueError, TypeError) as e:
            raise ValueError(ERR_INVALID_NODE_ID) from e

        if not node:
            raise LookupError(ERR_NODE_NOT_FOUND)

        blockers = self.get_publish_blockers(node)
        if blockers:
            return blockers

        if not node.node_type or not node.node_type.is_root:
            raise ValueError(ERR_NOT_A_ROOT_NODE)

        self.publish_schema(node)
        return None

    @transaction.atomic
    def publish_schema(self, node: Node) -> None:
        """Publish a schema node of any root type"""
        key = self._get_schema_attribute(node, 'key')
        status = self._get_schema_attribute(node, 'status')
        version = node.version

        if not version:
            raise ValueError(ERR_VERSION_NOT_SET)

        # Check if node type has a 'status' attribute with a 'published' value in its domain
        status_def = self.repository.get_attribute_def(node.node_type, 'status')
        if status_def and status_def.domain:
            published_value = self.repository.get_domain_item_by_value(status_def.domain, STATUS_PUBLISHED)
            if not published_value:
                raise ValueError(ERR_PUBLISHED_STATUS_NOT_AVAILABLE)

            draft_value = self.repository.get_domain_item_by_value(status_def.domain, STATUS_DRAFT)
            if draft_value and status != STATUS_DRAFT:
                raise ValueError(ERR_SCHEMA_NOT_IN_DRAFT_STATUS.format(status=status))
        elif status and status != STATUS_DRAFT:
            raise ValueError(ERR_SCHEMA_NOT_IN_DRAFT_STATUS.format(status=status))

        self.repository.publish_schema(key, version, node.project_id)
    
    @transaction.atomic
    def archive_schema(self, node: Node) -> None:
        """Archive a schema node of any root type"""
        status_def = self.repository.get_attribute_def(node.node_type, 'status')
        if not status_def:
            raise ValueError(ERR_STATUS_ATTRIBUTE_NOT_FOUND)

        status_attr = self.repository.get_node_attribute(node, status_def)
        if not status_attr:
            raise ValueError(ERR_STATUS_ATTRIBUTE_VALUE_NOT_FOUND)

        current_status = status_attr.value_string
        
        # Check if 'published' value exists in the domain
        if status_def.domain:
            published_value = self.repository.get_domain_item_by_value(status_def.domain, STATUS_PUBLISHED)
            if published_value and current_status != STATUS_PUBLISHED:
                raise ValueError(ERR_SCHEMA_NOT_PUBLISHED_STATUS.format(current_status=current_status))
        elif current_status != STATUS_PUBLISHED:
            raise ValueError(ERR_SCHEMA_NOT_PUBLISHED_STATUS.format(current_status=current_status))

        # Check if 'archived' value exists in the domain
        archived_value = None
        if status_def.domain:
            archived_value = self.repository.get_domain_item_by_value(status_def.domain, STATUS_ARCHIVED)
        if not archived_value:
            raise ValueError(ERR_ARCHIVED_STATUS_NOT_AVAILABLE)

        try:
            self.repository.set_node_attribute_from_json(
                node.id,
                node.node_type.name,
                status_def.json_key,
                STATUS_ARCHIVED,
                status_def.domain.domain_name if status_def.domain_id else None
            )
            # Delete schema cache to prevent public access after archiving
            if node.key and node.version:
                self.delete_schema_cache(node.key, node.version)
        except DatabaseError as e:
            raise RuntimeError(ERR_DATABASE_ERROR_ARCHIVE.format(error=e)) from e
        except ValueError as e:
            raise  # Re-raise ValueError as-is
        except Exception as e:
            raise RuntimeError(ERR_UNEXPECTED_ERROR_ARCHIVE.format(error=e)) from e
    
    @transaction.atomic
    def draft_schema(self, node: Node) -> None:
        """Move a schema node back to draft status"""
        status_def = self.repository.get_attribute_def(node.node_type, 'status')
        if not status_def:
            raise ValueError(ERR_STATUS_ATTRIBUTE_NOT_FOUND)

        status_attr = self.repository.get_node_attribute(node, status_def)
        if not status_attr:
            raise ValueError(ERR_STATUS_ATTRIBUTE_VALUE_NOT_FOUND)

        current_status = status_attr.value_string
        
        # Check if 'archived' value exists in the domain
        if status_def.domain:
            archived_value = self.repository.get_domain_item_by_value(status_def.domain, STATUS_ARCHIVED)
            if archived_value and current_status != STATUS_ARCHIVED:
                raise ValueError(ERR_SCHEMA_NOT_ARCHIVED_STATUS.format(current_status=current_status))
        elif current_status != STATUS_ARCHIVED:
            raise ValueError(ERR_SCHEMA_NOT_ARCHIVED_STATUS.format(current_status=current_status))

        # Check if 'draft' value exists in the domain
        draft_value = None
        if status_def.domain:
            draft_value = self.repository.get_domain_item_by_value(status_def.domain, STATUS_DRAFT)
        if not draft_value:
            raise ValueError(ERR_DRAFT_STATUS_NOT_AVAILABLE)

        try:
            self.repository.set_node_attribute_from_json(
                node.id,
                node.node_type.name,
                status_def.json_key,
                STATUS_DRAFT,
                status_def.domain.domain_name if status_def.domain_id else None
            )
            # Delete schema cache to prevent public access after moving to draft
            if node.key and node.version:
                self.delete_schema_cache(node.key, node.version)
        except DatabaseError as e:
            raise RuntimeError(ERR_DATABASE_ERROR_DRAFT.format(error=e)) from e
        except ValueError as e:
            raise  # Re-raise ValueError as-is
        except Exception as e:
            raise RuntimeError(ERR_UNEXPECTED_ERROR_DRAFT.format(error=e)) from e
    
    def increment_build_for(self, key: str, version: str) -> None:
        """Resolve the root schema node for key+version and increment its build counter.

        Admin "Build" orchestration: the project_id needed by the s7 function is
        derived from the root node rather than supplied by the view.

        Raises:
            LookupError: no root node exists for key+version.
            ValueError: the schema has no project assigned.
        """
        schema_node = self.repository.get_root_node_by_key_version(key, version)
        if not schema_node:
            raise LookupError(ERR_SCHEMA_NOT_FOUND)
        project_id = schema_node.project_id
        if not project_id:
            raise ValueError(ERR_PROJECT_ID_REQUIRED)
        self.increment_build(key, version, project_id)

    @transaction.atomic
    def increment_build(self, key: str, version: str, project_id: uuid.UUID) -> None:
        """Increment build counter"""
        try:
            self.repository.increment_build(key, version, project_id)
        except DatabaseError as e:
            raise RuntimeError(ERR_DATABASE_ERROR_BUILD.format(error=e)) from e
        except ValueError as e:
            raise  # Re-raise ValueError as-is
        except Exception as e:
            raise RuntimeError(ERR_UNEXPECTED_ERROR_BUILD.format(error=e)) from e

    @transaction.atomic
    def build_schema_cached(self, key: str, version: str, schema_type: Optional[str] = None,
                            project_id: Optional[uuid.UUID] = None) -> None:
        """Build schema cache"""
        try:
            self.repository.build_schema_cached(key, version, schema_type, project_id=project_id)
        except DatabaseError as e:
            raise RuntimeError(ERR_DATABASE_ERROR_CACHE_REBUILD.format(error=e)) from e
        except ValueError as e:
            raise  # Re-raise ValueError as-is
        except Exception as e:
            raise RuntimeError(ERR_UNEXPECTED_ERROR_CACHE_REBUILD.format(error=e)) from e
    
    @transaction.atomic
    def initialize_schema_attributes(self, node: Node, project_id: Optional[uuid.UUID] = None, organization_id: Optional[uuid.UUID] = None) -> None:
        """Initialize default attributes for a new schema node of any root type"""
        key_value = f"{node.name}{SCHEMA_KEY_SUFFIX}"
        version_value = "1"

        # Auto-increment version if key+version already exists
        for attempt in range(MAX_VERSION_AUTO_INCREMENT_ATTEMPTS):
            if not self.repository.check_key_version_exists(key_value, version_value):
                break
            version_value = str(int(version_value) + 1)
        else:
            raise ValueError(
                ERR_VERSION_NOT_AVAILABLE.format(key=key_value, attempts=MAX_VERSION_AUTO_INCREMENT_ATTEMPTS)
            )

        # Stamp project/organization on the root node
        if project_id or organization_id:
            update_fields = {}
            if project_id:
                update_fields["project_id"] = project_id
            if organization_id:
                update_fields["organization_id"] = organization_id
            self.repository.update_node_fields(node.id, **update_fields)

        # Ensure schema_build_state record exists
        self.repository.ensure_schema_build_state(key_value, version_value, project_id)

        # Disable triggers temporarily
        self.repository.disable_triggers()

        try:
            # Default values to bootstrap the root node — applied regardless of is_required flag
            root_defaults = {
                'id':      None,  # auto_uuid will auto-generate
                'key':     key_value,
                'title':   node.name,
            }
            # Only set status if the node type has a 'status' attribute
            status_def = self.repository.get_attribute_def(node.node_type, 'status')
            if status_def and status_def.domain:
                # Try to get default value from domain items, fallback to 'draft'
                default_status = self.repository.get_domain_item_by_value(status_def.domain, STATUS_DRAFT)
                if default_status:
                    root_defaults['status'] = STATUS_DRAFT
            
            # Get attribute defs for the keys we need to set
            attr_defs = self.repository.get_attribute_defs_by_node_type_and_keys(node.node_type, root_defaults.keys())
            for attr_def in attr_defs:
                default_value = root_defaults.get(attr_def.json_key)
                if default_value is not None or attr_def.data_type.name == 'auto_uuid':
                    self.repository.set_node_attribute_from_json(
                        node.id,
                        node.node_type.name,
                        attr_def.json_key,
                        default_value,
                        attr_def.domain.domain_name if attr_def.domain_id else None
                    )
        finally:
            self.repository.enable_triggers()

        # Auto-create the mandatory {root_type}_metadata child node if it exists
        self._initialize_metadata(node, version_value, project_id=project_id, organization_id=organization_id)

        # Mark as dirty since new nodes have uncommitted changes
        self.repository.mark_build_state_dirty(key_value, version_value)

    @transaction.atomic
    def _initialize_metadata(self, root_node: Node, version_value: str, project_id: Optional[uuid.UUID] = None, organization_id: Optional[uuid.UUID] = None) -> None:
        """Create the {root_type}_metadata child node for any root schema type."""
        metadata_type_name = f"{root_node.node_type.name}{SCHEMA_METADATA_SUFFIX}"
        metadata_type = self.repository.get_node_type_by_name(metadata_type_name)
        if not metadata_type:
            return
        if self.repository.node_exists(root_node, metadata_type):
            return
        last = self.repository.get_last_child_node(root_node)
        meta_pos = (last.sort_order + 1) if last else 0
        
        # Get composition to determine collection_key for singleton slots
        composition = self.repository.get_composition_by_parent_child(root_node.node_type, metadata_type)
        node_key = None
        if composition and composition.collection_key and composition.max_children == 1:
            node_key = composition.collection_key
        
        metadata_node = self.repository.create_node(
            parent=root_node,
            node_type=metadata_type,
            sort_order=meta_pos,
            name='metadata',
            key=node_key,
            version=version_value,
            project_id=project_id,
            organization_id=organization_id,
        )
        self.repository.update_node_fields(root_node.id, version=version_value)
        metadata_defaults = {'version': version_value}
        self.repository.disable_triggers()
        try:
            for attr_def in self.repository.get_attribute_defs_by_node_type_and_keys(metadata_type, metadata_defaults.keys()):
                default_value = metadata_defaults.get(attr_def.json_key)
                if default_value is not None:
                    self.repository.set_node_attribute_from_json(
                        metadata_node.id,
                        metadata_type.name,
                        attr_def.json_key,
                        default_value,
                        attr_def.domain.domain_name if attr_def.domain_id else None
                    )
        finally:
            self.repository.enable_triggers()
    
    def is_schema_locked(self, node: Node) -> bool:
        """Lifecycle rule: archived schemas are read-only."""
        return self._get_schema_attribute(node, 'status') == STATUS_ARCHIVED

    def _get_schema_attribute(self, node: Node, attribute_key: str) -> Optional[str]:
        """Get a schema node attribute value by key"""
        if attribute_key == 'key' and node.key:
            return node.key
        attr_def = self.repository.get_attribute_def(node.node_type, attribute_key)
        if not attr_def:
            return None

        attr = self.repository.get_node_attribute(node, attr_def)
        if not attr:
            return None

        return attr.value_string

    @transaction.atomic
    def delete_schema_cache(self, key: str, version: str) -> None:
        """Delete build state and schema cache for a given key and version
        
        This method orchestrates the deletion of both build state and schema cache
        within a single transaction to ensure data consistency.
        """
        self.repository.delete_build_state(key, version)
        self.repository.delete_schema_cache_by_key_version(key, version)
