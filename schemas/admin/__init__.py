"""
Dynamic Schema Admin Package

Organized into modules by responsibility:
- base: Base classes, inlines and shared utilities
- definitions: Admin for definition models (NodeType, AttributeDef, DataType, etc.)
- node_editor: NodeEditorMixin - API endpoints for visual editor
- node: NodeAdmin and NodeAttributeAdmin
- schema: SchemaAdmin with import, publish, archive, draft, build
- build_state: BuildStateAdmin with dynamic tabs
- schema_cache: SchemaCacheAdmin with dynamic tabs
"""

from .base import (
    NodeAttributeInline,
    NodeCompositionInline,
    NodeCompositionReverseInline,
    BaseNodeAdmin,
)

from .definitions import (
    DataTypeAdmin,
    DomainAdmin,
    DomainItemAdmin,
    AttributeDefAdmin,
    NodeTypeCompositionInline,
    NodeTypeAdmin,
    NodeTypeCompositionAdmin,
    ComponentPropertiesAdmin,
    ComponentPropertiesProxy,
)

from .node import (
    NodeAdmin,
    NodeAttributeAdmin,
)

from .schema import SchemaAdmin

from .build_state import BuildStateAdmin
from .schema_cache import SchemaCacheAdmin

from .organization import OrganizationAdmin, OrganizationMemberInline
from .project import ProjectAdmin
from .project_credential import ProjectAPICredentialAdmin

__all__ = [
    # Base
    'NodeAttributeInline',
    'NodeCompositionInline',
    'NodeCompositionReverseInline',
    'BaseNodeAdmin',
    # Definitions
    'DataTypeAdmin',
    'DomainAdmin',
    'DomainItemAdmin',
    'AttributeDefAdmin',
    'NodeTypeCompositionInline',
    'NodeTypeAdmin',
    'NodeTypeCompositionAdmin',
    'ComponentPropertiesAdmin',
    'ComponentPropertiesProxy',
    # Node & Schema
    'NodeAdmin',
    'NodeAttributeAdmin',
    'SchemaAdmin',
    # Build State & Cache
    'BuildStateAdmin',
    'SchemaCacheAdmin',
    # Multi-tenancy
    'OrganizationAdmin',
    'OrganizationMemberInline',
    'ProjectAdmin',
    'ProjectAPICredentialAdmin',
]
