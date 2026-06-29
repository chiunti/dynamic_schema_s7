import uuid
from typing import Optional

from ..models import Project


class ProjectRepository:
    """Repository for project-related database operations."""

    def create_project(
        self,
        name: str,
        slug: str,
        organization_id: Optional[uuid.UUID],
        created_by_id: Optional[uuid.UUID],
        description: Optional[str] = None,
    ) -> Project:
        return Project.objects.create(
            name=name,
            slug=slug,
            description=description,
            organization_id=organization_id,
            created_by_id=created_by_id,
        )

    def get_project_by_id(self, project_id: uuid.UUID) -> Optional[Project]:
        try:
            return Project.objects.select_related("organization", "created_by").get(id=project_id)
        except Project.DoesNotExist:
            return None

    def get_projects_by_organization(self, organization_id: uuid.UUID):
        return (
            Project.objects.filter(organization_id=organization_id)
            .select_related("organization", "created_by")
            .order_by("name")
        )

    def get_projects_by_user(self, user_id: uuid.UUID):
        return (
            Project.objects.filter(created_by_id=user_id)
            .select_related("organization", "created_by")
            .order_by("name")
        )

    def get_projects_accessible_to_user(self, user_id: uuid.UUID, organization_ids):
        return (
            Project.objects.filter(organization_id__in=organization_ids)
            .select_related("organization", "created_by")
            .order_by("name")
        )

    def update_project(self, project_id: uuid.UUID, **fields) -> Optional[Project]:
        updated = Project.objects.filter(id=project_id).update(**fields)
        if not updated:
            return None
        return self.get_project_by_id(project_id)

    def delete_project(self, project_id: uuid.UUID) -> bool:
        deleted, _ = Project.objects.filter(id=project_id).delete()
        return deleted > 0

    def slug_exists_in_organization(
        self,
        slug: str,
        organization_id: Optional[uuid.UUID],
        exclude_project_id: Optional[uuid.UUID] = None,
    ) -> bool:
        qs = Project.objects.filter(slug=slug, organization_id=organization_id)
        if exclude_project_id:
            qs = qs.exclude(id=exclude_project_id)
        return qs.exists()

    def has_schemas(self, project_id: uuid.UUID) -> bool:
        from ..models import Node
        return Node.objects.filter(project_id=project_id, parent__isnull=True).exists()

    def get_all_projects_ordered(self):
        """
        Get all projects ordered by name.

        Returns:
            QuerySet of Project instances ordered by name
        """
        return Project.objects.all().order_by('name')
