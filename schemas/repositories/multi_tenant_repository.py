import uuid
from typing import Optional

from ..models import Organization, OrganizationMember, Project


class MultiTenantRepository:
    """Helper repository for multi-tenant queryset filtering."""

    def get_accessible_organizations(self, user) -> list:
        if user.is_superuser:
            return list(Organization.objects.filter(is_active=True).order_by("name"))
        org_ids = OrganizationMember.objects.filter(user=user).values_list(
            "organization_id", flat=True
        )
        return list(Organization.objects.filter(id__in=org_ids, is_active=True).order_by("name"))

    def get_accessible_organization_ids(self, user) -> list:
        if user.is_superuser:
            return list(Organization.objects.filter(is_active=True).values_list("id", flat=True))
        return list(
            OrganizationMember.objects.filter(user=user).values_list("organization_id", flat=True)
        )

    def get_accessible_projects(self, user, organization_id: Optional[uuid.UUID] = None):
        if user.is_superuser:
            qs = Project.objects.select_related("organization", "created_by")
            if organization_id:
                qs = qs.filter(organization_id=organization_id)
            return qs.order_by("name")

        accessible_org_ids = self.get_accessible_organization_ids(user)
        qs = Project.objects.filter(
            organization_id__in=accessible_org_ids
        ).select_related("organization", "created_by")
        if organization_id and str(organization_id) in [str(i) for i in accessible_org_ids]:
            qs = qs.filter(organization_id=organization_id)
        return qs.order_by("name")

    def filter_nodes_by_user(self, queryset, user):
        if user.is_superuser:
            return queryset
        accessible_org_ids = self.get_accessible_organization_ids(user)
        return queryset.filter(organization_id__in=accessible_org_ids)

    def user_has_role(self, user, organization_id: uuid.UUID, roles: list) -> bool:
        if user.is_superuser:
            return True
        return OrganizationMember.objects.filter(
            user=user,
            organization_id=organization_id,
            role__in=roles,
        ).exists()

    def is_member_of(self, user, organization_id: uuid.UUID) -> bool:
        """Check if user is a member of an organization."""
        if user.is_superuser:
            return True
        return OrganizationMember.objects.filter(
            user=user,
            organization_id=organization_id,
        ).exists()

    def get_role_in(self, user, organization_id: uuid.UUID) -> Optional[str]:
        """Get user's role in an organization."""
        if user.is_superuser:
            return "admin"
        try:
            member = OrganizationMember.objects.get(
                user=user,
                organization_id=organization_id,
            )
            return member.role
        except OrganizationMember.DoesNotExist:
            return None
