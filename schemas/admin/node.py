"""
NodeAdmin - Basic node management with editor mixin
"""

from django.contrib import admin

from ..models import (
    Node,
    NodeAttribute,
)
from .base import NodeAttributeInline
from .node_editor import NodeEditorMixin


@admin.register(Node)
class NodeAdmin(NodeEditorMixin, admin.ModelAdmin):
    list_display = ("name", "node_type", "parent", "sort_order")
    list_filter = ("node_type__json_scope", "node_type")
    search_fields = ("name", "node_type__name", "parent__name")
    autocomplete_fields = ("node_type", "parent")
    inlines = (NodeAttributeInline,)

    def delete_model(self, request, obj):
        from ..services.node_service import NodeService
        NodeService().delete_node(obj.id)

    def delete_queryset(self, request, queryset):
        from ..services.node_service import NodeService
        svc = NodeService()
        for obj in queryset:
            svc.delete_node(obj.id)


@admin.register(NodeAttribute)
class NodeAttributeAdmin(admin.ModelAdmin):
    list_display = ("node", "attribute_def", "value_string", "value_number", "value_bool")
    list_filter = ("attribute_def",)
    search_fields = ("node__key", "attribute_def__name", "attribute_def__json_key")
    autocomplete_fields = ("node", "attribute_def")
