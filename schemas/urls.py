from django.urls import path
from .views import (
    CatalogDatatypesView,
    CatalogScopeView,
    SchemaImportView,
    SchemaPublishView,
    SchemaValidateView,
    SchemaView,
)
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
    path('schema/import/', SchemaImportView.as_view(), name='schema-import'),
    path('schema/validate/', SchemaValidateView.as_view(), name='schema-validate'),
    path(
        'schema/<str:node_type>/<str:key>/<str:version>/publish/',
        SchemaPublishView.as_view(),
        name='schema-publish',
    ),
    path('schema/<str:node_type>/<str:key>/<str:version>/', SchemaView.as_view(), name='schema'),
    # Catalog — 'datatypes' must precede '<str:scope>' so it is not captured as a scope
    path('catalog/datatypes/', CatalogDatatypesView.as_view(), name='catalog-datatypes'),
    path('catalog/<str:scope>/', CatalogScopeView.as_view(), name='catalog-scope'),
    # Organizations
    path('organizations/', OrganizationListCreateView.as_view(), name='organization-list'),
    path('organizations/<uuid:org_id>/', OrganizationDetailView.as_view(), name='organization-detail'),
    path('organizations/<uuid:org_id>/members/', OrganizationMemberListView.as_view(), name='organization-members'),
    path('organizations/<uuid:org_id>/members/<uuid:member_id>/', OrganizationMemberDetailView.as_view(), name='organization-member-detail'),
    # Projects
    path('projects/', ProjectListCreateView.as_view(), name='project-list'),
    path('projects/<uuid:project_id>/', ProjectDetailView.as_view(), name='project-detail'),
]
