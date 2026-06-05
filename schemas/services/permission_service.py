import uuid
from typing import Optional

from ..repositories.organization_repository import OrganizationRepository
from ..repositories.project_repository import ProjectRepository


class PermissionService:
    """Service for evaluating user permissions on organizations and projects."""

    EDIT_ROLES = ("admin", "editor")
    ADMIN_ROLES = ("admin",)

    def __init__(
        self,
        org_repo: Optional[OrganizationRepository] = None,
        project_repo: Optional[ProjectRepository] = None,
    ):
        self._org_repo = org_repo or OrganizationRepository()
        self._project_repo = project_repo or ProjectRepository()

    def is_superuser(self, user) -> bool:
        return bool(user.is_superuser)

    def get_user_role_in_organization(self, user, organization_id: uuid.UUID) -> Optional[str]:
        if self.is_superuser(user):
            return "admin"
        return self._org_repo.get_user_role_in_organization(user.id, organization_id)

    def can_access_organization(self, user, organization_id: uuid.UUID) -> bool:
        if self.is_superuser(user):
            return True
        return self.get_user_role_in_organization(user, organization_id) is not None

    def can_edit_organization(self, user, organization_id: uuid.UUID) -> bool:
        if self.is_superuser(user):
            return True
        role = self.get_user_role_in_organization(user, organization_id)
        return role in self.EDIT_ROLES

    def can_admin_organization(self, user, organization_id: uuid.UUID) -> bool:
        if self.is_superuser(user):
            return True
        role = self.get_user_role_in_organization(user, organization_id)
        return role in self.ADMIN_ROLES

    def can_access_project(self, user, project_id: uuid.UUID) -> bool:
        if self.is_superuser(user):
            return True
        project = self._project_repo.get_project_by_id(project_id)
        if not project:
            return False
        return self.can_access_organization(user, project.organization_id)

    def can_edit_project(self, user, project_id: uuid.UUID) -> bool:
        if self.is_superuser(user):
            return True
        project = self._project_repo.get_project_by_id(project_id)
        if not project:
            return False
        return self.can_edit_organization(user, project.organization_id)
