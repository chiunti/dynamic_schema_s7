from django.urls import path
from .views import schema_view
from .api_views import (
    OrganizationListCreateView,
    OrganizationDetailView,
    OrganizationMemberListView,
    OrganizationMemberDetailView,
    ProjectListCreateView,
    ProjectDetailView,
)

app_name = 'schemas'

urlpatterns = [
    path('schema/<str:node_type>/<str:key>/<str:version>/', schema_view, name='schema'),
    # Organizations
    path('organizations/', OrganizationListCreateView.as_view(), name='organization-list'),
    path('organizations/<uuid:org_id>/', OrganizationDetailView.as_view(), name='organization-detail'),
    path('organizations/<uuid:org_id>/members/', OrganizationMemberListView.as_view(), name='organization-members'),
    path('organizations/<uuid:org_id>/members/<uuid:member_id>/', OrganizationMemberDetailView.as_view(), name='organization-member-detail'),
    # Projects
    path('projects/', ProjectListCreateView.as_view(), name='project-list'),
    path('projects/<uuid:project_id>/', ProjectDetailView.as_view(), name='project-detail'),
]
