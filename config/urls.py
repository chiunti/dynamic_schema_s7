"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, re_path, include
from django.http import JsonResponse
from django.views.generic import RedirectView
from django.db import connection

from schemas.admin_api_views import (
    api_component_types,
    api_component_properties,
    api_save_component_properties,
    api_variants_by_scope,
    api_create_attribute_def,
    api_update_attribute_common,
    api_update_attribute_specific,
    api_delete_attribute_def,
    api_attributes_by_variant,
)


def health_check(request):
    """Health check endpoint that verifies database connectivity"""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse({"status": "ok", "database": "connected"})
    except Exception as e:
        return JsonResponse({"status": "error", "database": "disconnected", "error": str(e)}, status=503)


urlpatterns = [
    path('', RedirectView.as_view(url='/admin/')),
    path('health/', health_check, name='health_check'),
    # Global API endpoints - must come BEFORE admin URLs
    re_path(r'^admin/schemas/api/component-types/$', api_component_types, name="schemas_api_component_types"),
    re_path(r'^admin/schemas/api/component-properties/(?P<component_type>[^/]+)/$', api_component_properties, name="schemas_api_component_properties"),
    re_path(r'^admin/schemas/api/component-properties/(?P<component_type>[^/]+)/save/$', api_save_component_properties, name="schemas_api_save_component_properties"),
    re_path(r'^admin/schemas/api/variants/$', api_variants_by_scope, name="schemas_api_variants_by_scope"),
    re_path(r'^admin/schemas/api/attributes-by-variant/(?P<variant_key>[^/]+)/$', api_attributes_by_variant, name="schemas_api_attributes_by_variant"),
    re_path(r'^admin/schemas/api/create-attribute-def/$', api_create_attribute_def, name="schemas_api_create_attribute_def"),
    re_path(r'^admin/schemas/api/update-attribute-common/$', api_update_attribute_common, name="schemas_api_update_attribute_common"),
    re_path(r'^admin/schemas/api/update-attribute-specific/$', api_update_attribute_specific, name="schemas_api_update_attribute_specific"),
    re_path(r'^admin/schemas/api/delete-attribute-def/$', api_delete_attribute_def, name="schemas_api_delete_attribute_def"),
    # Admin URLs
    path('admin/', admin.site.urls),
    path('api/', include('schemas.urls')),
]

