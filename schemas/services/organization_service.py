import uuid
from typing import Optional

from django.db import transaction
from django.contrib.auth import get_user_model
from django.utils.text import slugify

from ..repositories.organization_repository import OrganizationRepository
from ..repositories.multi_tenant_repository import MultiTenantRepository
from ..repositories.user_repository import UserRepository
from ..models import Organization, OrganizationMember
from ..constants import (
    ERR_MEMBER_NOT_FOUND,
    ERR_ORGANIZATION_SLUG_EXISTS,
    ERR_ONLY_SUPERUSERS_CAN_CREATE_ORGANIZATIONS,
    ERR_ONLY_SUPERUSERS_CAN_UPDATE_ORGANIZATIONS,
    ERR_ONLY_SUPERUSERS_CAN_DELETE_ORGANIZATIONS,
    ERR_ONLY_SUPERUSERS_CAN_MANAGE_MEMBERS,
    ERR_NO_ACCESS_TO_ORGANIZATION,
    ERR_ORGANIZATION_NOT_FOUND,
    ERR_USER_NOT_FOUND,
    ERR_INVALID_ROLE,
)
from .permission_service import PermissionService

User = get_user_model()


class OrganizationService:
    """Service for organization management business logic."""

    def __init__(self):
        self._repo = OrganizationRepository()
        self._mt_repo = MultiTenantRepository()
        self._perm = PermissionService()
        self._user_repo = UserRepository()

    @transaction.atomic
    def create_organization(self, name: str, description: Optional[str], user, slug: Optional[str] = None) -> Organization:
        if not self._perm.is_superuser(user):
            raise PermissionError(ERR_ONLY_SUPERUSERS_CAN_CREATE_ORGANIZATIONS)
        resolved_slug = slug or slugify(name)
        if self._repo.get_organization_by_slug(resolved_slug):
            raise ValueError(ERR_ORGANIZATION_SLUG_EXISTS.format(slug=resolved_slug))
        return self._repo.create_organization(name=name, slug=resolved_slug, description=description)

    @transaction.atomic
    def update_organization(self, organization_id: uuid.UUID, user, **fields) -> Organization:
        if not self._perm.is_superuser(user):
            raise PermissionError(ERR_ONLY_SUPERUSERS_CAN_UPDATE_ORGANIZATIONS)
        org = self._repo.get_organization_by_id(organization_id)
        if not org:
            raise ValueError(ERR_ORGANIZATION_NOT_FOUND.format(organization_id=organization_id))
        if "name" in fields and "slug" not in fields:
            fields["slug"] = slugify(fields["name"])
        if "slug" in fields and fields["slug"] != org.slug:
            existing = self._repo.get_organization_by_slug(fields["slug"])
            if existing and str(existing.id) != str(organization_id):
                raise ValueError(ERR_ORGANIZATION_SLUG_EXISTS.format(slug=fields["slug"]))
        return self._repo.update_organization(organization_id, **fields)

    def get_organization(self, organization_id: uuid.UUID, user) -> Organization:
        if not self._perm.can_access_organization(user, organization_id):
            raise PermissionError(ERR_NO_ACCESS_TO_ORGANIZATION)
        org = self._repo.get_organization_by_id(organization_id)
        if not org:
            raise ValueError(ERR_ORGANIZATION_NOT_FOUND.format(organization_id=organization_id))
        return org

    def list_organizations(self, user):
        return self._mt_repo.get_accessible_organizations(user)

    @transaction.atomic
    def add_member(
        self,
        organization_id: uuid.UUID,
        user_email: str,
        role: str,
        requesting_user,
    ) -> OrganizationMember:
        if not self._perm.can_admin_organization(requesting_user, organization_id):
            raise PermissionError(ERR_ONLY_SUPERUSERS_CAN_MANAGE_MEMBERS)
        target_user = self._user_repo.get_user_by_email(user_email)
        if not target_user:
            raise ValueError(ERR_USER_NOT_FOUND.format(email=user_email))
        if role not in ("admin", "editor", "viewer"):
            raise ValueError(ERR_INVALID_ROLE.format(role=role))
        return self._repo.add_member(organization_id, target_user.id, role)

    @transaction.atomic
    def remove_member(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        requesting_user,
    ) -> bool:
        if not self._perm.can_admin_organization(requesting_user, organization_id):
            raise PermissionError(ERR_ONLY_SUPERUSERS_CAN_MANAGE_MEMBERS)
        removed = self._repo.remove_member(organization_id, user_id)
        if not removed:
            raise ValueError(ERR_MEMBER_NOT_FOUND)
        return True

    def get_members(self, organization_id: uuid.UUID, user):
        if not self._perm.can_access_organization(user, organization_id):
            raise PermissionError(ERR_NO_ACCESS_TO_ORGANIZATION)
        return self._repo.get_members(organization_id)
