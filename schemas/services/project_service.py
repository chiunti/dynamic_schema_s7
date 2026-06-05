import uuid
from typing import Optional

from django.db import transaction
from django.utils.text import slugify

from ..repositories.project_repository import ProjectRepository
from ..repositories.multi_tenant_repository import MultiTenantRepository
from ..models import Project
from ..constants import (
    ERR_CANNOT_DELETE_PROJECT_WITH_SCHEMAS,
    ERR_PROJECT_SLUG_EXISTS,
    ERR_NO_PERMISSION_TO_CREATE_PROJECTS,
    ERR_NO_PERMISSION_TO_EDIT_PROJECT,
    ERR_NO_PERMISSION_TO_DELETE_PROJECT,
    ERR_NO_ACCESS_TO_PROJECT,
    ERR_PROJECT_NOT_FOUND,
)
from .permission_service import PermissionService


class ProjectService:
    """Service for project management business logic."""

    def __init__(self):
        self._repo = ProjectRepository()
        self._mt_repo = MultiTenantRepository()
        self._perm = PermissionService()

    @transaction.atomic
    def create_project(
        self,
        name: str,
        description: Optional[str],
        organization_id: Optional[uuid.UUID],
        user,
        slug: Optional[str] = None,
    ) -> Project:
        if organization_id and not self._perm.can_edit_organization(user, organization_id):
            raise PermissionError(ERR_NO_PERMISSION_TO_CREATE_PROJECTS)
        resolved_slug = slug or slugify(name)
        if self._repo.slug_exists_in_organization(resolved_slug, organization_id):
            raise ValueError(ERR_PROJECT_SLUG_EXISTS.format(slug=resolved_slug))
        return self._repo.create_project(
            name=name,
            slug=resolved_slug,
            description=description,
            organization_id=organization_id,
            created_by_id=user.id,
        )

    @transaction.atomic
    def update_project(self, project_id: uuid.UUID, user, **fields) -> Project:
        if not self._perm.can_edit_project(user, project_id):
            raise PermissionError(ERR_NO_PERMISSION_TO_EDIT_PROJECT)
        project = self._repo.get_project_by_id(project_id)
        if not project:
            raise ValueError(ERR_PROJECT_NOT_FOUND.format(project_id=project_id))
        if "slug" in fields:
            org_id = fields.get("organization_id", project.organization_id)
            if self._repo.slug_exists_in_organization(fields["slug"], org_id, exclude_project_id=project_id):
                raise ValueError(ERR_PROJECT_SLUG_EXISTS.format(slug=fields["slug"]))
        return self._repo.update_project(project_id, **fields)

    @transaction.atomic
    def delete_project(self, project_id: uuid.UUID, user) -> bool:
        if not self._perm.can_edit_project(user, project_id):
            raise PermissionError(ERR_NO_PERMISSION_TO_DELETE_PROJECT)
        if self._repo.has_schemas(project_id):
            raise ValueError(ERR_CANNOT_DELETE_PROJECT_WITH_SCHEMAS)
        return self._repo.delete_project(project_id)

    def get_project(self, project_id: uuid.UUID, user) -> Project:
        if not self._perm.can_access_project(user, project_id):
            raise PermissionError(ERR_NO_ACCESS_TO_PROJECT)
        project = self._repo.get_project_by_id(project_id)
        if not project:
            raise ValueError(ERR_PROJECT_NOT_FOUND.format(project_id=project_id))
        return project

    def list_projects(self, user, organization_id: Optional[uuid.UUID] = None):
        return self._mt_repo.get_accessible_projects(user, organization_id=organization_id)
