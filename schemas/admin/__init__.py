"""
Dynamic Schema Admin Package

Organizado en módulos por responsabilidad:
- base: Clases base, inlines y utilidades compartidas
- definitions: Admin para modelos de definición (NodeType, AttributeDef, DataType, etc.)
- node_editor: NodeEditorMixin - API endpoints for visual editor
- node: NodeAdmin y NodeAttributeAdmin
- schema: SchemaAdmin con import, publish, archive, draft, build
- build_state: BuildStateAdmin con tabs dinámicos
- schema_cache: SchemaCacheAdmin con tabs dinámicos
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
]
