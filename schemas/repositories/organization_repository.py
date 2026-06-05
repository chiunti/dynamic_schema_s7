import uuid
from typing import Optional

from ..models import Organization, OrganizationMember


class OrganizationRepository:
    """Repository for organization-related database operations."""

    def create_organization(
        self,
        name: str,
        slug: str,
        description: Optional[str] = None,
    ) -> Organization:
        return Organization.objects.create(
            name=name,
            slug=slug,
            description=description,
        )

    def get_organization_by_id(self, organization_id: uuid.UUID) -> Optional[Organization]:
        try:
            return Organization.objects.get(id=organization_id)
        except Organization.DoesNotExist:
            return None

    def get_organization_by_slug(self, slug: str) -> Optional[Organization]:
        try:
            return Organization.objects.get(slug=slug)
        except Organization.DoesNotExist:
            return None

    def update_organization(self, organization_id: uuid.UUID, **fields) -> Optional[Organization]:
        updated = Organization.objects.filter(id=organization_id).update(**fields)
        if not updated:
            return None
        return self.get_organization_by_id(organization_id)

    def list_organizations(self, active_only: bool = False):
        qs = Organization.objects.all()
        if active_only:
            qs = qs.filter(is_active=True)
        return qs.order_by("name")

    def add_member(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        role: str,
    ) -> OrganizationMember:
        member, _ = OrganizationMember.objects.get_or_create(
            organization_id=organization_id,
            user_id=user_id,
            defaults={"role": role},
        )
        if member.role != role:
            member.role = role
            member.save(update_fields=["role"])
        return member

    def remove_member(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        deleted, _ = OrganizationMember.objects.filter(
            organization_id=organization_id,
            user_id=user_id,
        ).delete()
        return deleted > 0

    def get_members(self, organization_id: uuid.UUID):
        return (
            OrganizationMember.objects.filter(organization_id=organization_id)
            .select_related("user")
            .order_by("joined_at")
        )

    def get_user_organizations(self, user_id: uuid.UUID):
        org_ids = OrganizationMember.objects.filter(user_id=user_id).values_list(
            "organization_id", flat=True
        )
        return Organization.objects.filter(id__in=org_ids, is_active=True).order_by("name")

    def get_user_role_in_organization(
        self,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> Optional[str]:
        try:
            member = OrganizationMember.objects.get(
                user_id=user_id,
                organization_id=organization_id,
            )
            return member.role
        except OrganizationMember.DoesNotExist:
            return None

    def get_member(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Optional[OrganizationMember]:
        try:
            return OrganizationMember.objects.get(
                organization_id=organization_id,
                user_id=user_id,
            )
        except OrganizationMember.DoesNotExist:
            return None
