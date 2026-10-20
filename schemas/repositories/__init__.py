"""Public repository exports.

Convention: every public ``*Repository`` class is re-exported at package
level. Callers may import either from the package
(``from ..repositories import X``) or from the concrete module path
(``from ..repositories.x_repository import X``); both forms are valid.
New repository classes must be added here.
"""

from .attribute_def_repository import AttributeDefRepository
from .composition_repository import CompositionRepository
from .data_type_repository import DataTypeRepository
from .editor_extension_repository import EditorExtensionRepository
from .fixture_repository import FixtureRepository
from .multi_tenant_repository import MultiTenantRepository
from .node_type_repository import NodeTypeRepository
from .organization_repository import OrganizationRepository
from .project_repository import ProjectCredentialRepository, ProjectRepository
from .schema_repository import NodeRepository, SchemaRepository
from .user_repository import UserRepository

__all__ = [
    "AttributeDefRepository",
    "CompositionRepository",
    "DataTypeRepository",
    "EditorExtensionRepository",
    "FixtureRepository",
    "MultiTenantRepository",
    "NodeRepository",
    "NodeTypeRepository",
    "OrganizationRepository",
    "ProjectCredentialRepository",
    "ProjectRepository",
    "SchemaRepository",
    "UserRepository",
]
