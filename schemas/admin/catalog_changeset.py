import json

from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied
from django.http import (
    Http404,
    HttpResponseNotAllowed,
    HttpResponseRedirect,
)
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join

from ..models import CatalogChangeSet
from ..services.catalog_changeset_service import CatalogChangeSetService


@admin.register(CatalogChangeSet)
class CatalogChangeSetAdmin(admin.ModelAdmin):
    """Governed review admin for catalog changesets.

    Changesets are created through the governed propose flow (API/service),
    never by hand — this admin is where a human reviewer inspects the ops
    diff and the validation report, then drives the lifecycle through the
    approve/reject/apply/supersede POST endpoints wired in ``get_urls``.
    Every
    transition is executed by ``CatalogChangeSetService``; the admin stays
    a thin presentation/delegation shell.
    """

    change_form_template = "admin/schemas/catalogchangeset/change_form.html"

    list_display = ("short_id", "status", "proposer", "created_at", "applied_at")
    list_filter = ("status",)
    ordering = ("-created_at",)
    search_fields = (
        "id",
        "rationale",
        "base_revision",
        "proposed_by_credential__name",
        "proposed_by_user__email",
    )
    readonly_fields = (
        "id",
        "status",
        "proposed_by_credential",
        "proposed_by_user",
        "rationale",
        "ops",
        "ops_diff",
        "ops_pretty",
        "scopes",
        "patch_count",
        "base_revision",
        "validation_report",
        "report_findings",
        "report_pretty",
        "reviewed_by",
        "review_note",
        "applied_at",
        "created_at",
        "updated_at",
        "post_apply_report",
        "post_apply_findings",
        "post_apply_pretty",
    )

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            # The add view is unreachable (has_add_permission -> False); this
            # minimal layout only keeps the hook well-formed.
            return (
                (None, {
                    "fields": ("status", "rationale", "ops"),
                }),
            )
        return (
            ("Identity", {
                "fields": ("id", "status"),
            }),
            ("Proposal", {
                "fields": (
                    "proposed_by_credential",
                    "proposed_by_user",
                    "rationale",
                    "ops_diff",
                    "ops_pretty",
                    "scopes",
                    "patch_count",
                ),
            }),
            ("Validation", {
                "fields": (
                    "base_revision",
                    "report_findings",
                    "report_pretty",
                ),
            }),
            ("Review", {
                "fields": (
                    "reviewed_by",
                    "review_note",
                    "applied_at",
                    "created_at",
                    "updated_at",
                ),
            }),
            # Last chapter of the lifecycle narrative: the revalidation
            # report only exists once the apply recorded in "Review"
            # (``applied_at``) has run — placing it before Review would
            # show a consequence ahead of its cause.
            ("Post-apply revalidation", {
                "fields": (
                    "post_apply_findings",
                    "post_apply_pretty",
                ),
            }),
        )

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ()
        return self.readonly_fields

    # ------------------------------------------------------------------ #
    # Lifecycle actions                                                   #
    # ------------------------------------------------------------------ #

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                f"<path:object_id>/{action}/",
                self.admin_site.admin_view(self._lifecycle_action_view),
                {"action": action},
                name=f"schemas_catalogchangeset_{action}",
            )
            for action in ("approve", "apply", "reject", "supersede")
        ]
        return custom + urls

    def _lifecycle_action_view(self, request, object_id, action):
        """POST endpoint driving one changeset lifecycle transition.

        Shared by the four action routes — each binds its ``action``
        through the pattern kwargs. POST-only (GET answers 405): state
        changes must never be link-followable. The superuser gate mirrors
        the ``has_*_permission`` hooks — the custom URL needs its own
        check because ``admin_view`` only enforces staff access.
        """
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not request.user.is_superuser:
            raise PermissionDenied

        # ``self.get_object`` (rather than a raw get_object_or_404) maps a
        # malformed ``<path:object_id>`` to None — a clean 404 instead of
        # an uncaught ValidationError/500 — and keeps the changelist
        # select_related queryset.
        changeset = self.get_object(request, unquote(object_id))
        if changeset is None:
            raise Http404("Catalog changeset not found")

        service = CatalogChangeSetService()
        try:
            if action == "approve":
                service.approve(changeset, request.user)
                self.message_user(
                    request, "Changeset approved.", messages.SUCCESS,
                )
            elif action == "reject":
                service.reject(
                    changeset,
                    request.user,
                    reason=request.POST.get("reason", ""),
                )
                self.message_user(
                    request, "Changeset rejected.", messages.SUCCESS,
                )
            elif action == "apply":
                changeset = service.apply(changeset, request.user)
                if changeset.status == CatalogChangeSet.Status.APPLIED:
                    self.message_user(
                        request, "Changeset applied.", messages.SUCCESS,
                    )
                elif changeset.status == CatalogChangeSet.Status.STALE:
                    self.message_user(
                        request,
                        "Catalog moved on — changeset marked stale, "
                        "re-validate.",
                        messages.WARNING,
                    )
                else:
                    # Defensive: the service contract only returns
                    # ``applied`` or ``stale`` from a successful apply.
                    self.message_user(
                        request,
                        f"Apply returned unexpected status "
                        f"'{changeset.status}'.",
                        messages.WARNING,
                    )
            elif action == "supersede":
                service.supersede(
                    changeset,
                    request.user,
                    reason=request.POST.get("reason", ""),
                )
                self.message_user(
                    request, "Changeset superseded.", messages.SUCCESS,
                )
            else:
                # Unreachable through the wired routes — guards a future
                # route typo from silently no-op'ing.
                raise Http404(f"Unknown changeset action: {action}")
        except ValueError as e:
            # ChangesetStateConflictError (the 409 signal) and mid-apply
            # failures both arrive as ValueError subclasses — surface the
            # service message verbatim.
            self.message_user(request, str(e), messages.ERROR)

        return HttpResponseRedirect(
            reverse(
                "admin:schemas_catalogchangeset_change",
                args=[changeset.pk],
            )
        )

    def changeform_view(
        self, request, object_id=None, form_url="", extra_context=None
    ):
        """Inject the action flags the change form template renders.

        Recomputed from the live status on every request — after a
        lifecycle POST the redirect lands back here with the new status
        and the buttons re-gate themselves.
        """
        if object_id is not None:
            obj = self.get_object(request, unquote(object_id))
            extra_context = {
                **(extra_context or {}),
                "changeset_can_approve": (
                    obj is not None
                    and obj.status == CatalogChangeSet.Status.VALID
                ),
                "changeset_can_apply": (
                    obj is not None
                    and obj.status == CatalogChangeSet.Status.APPROVED
                ),
                "changeset_can_reject": (
                    obj is not None
                    and obj.status
                    in (
                        CatalogChangeSet.Status.VALID,
                        CatalogChangeSet.Status.PROPOSED,
                    )
                ),
                "changeset_can_supersede": (
                    obj is not None
                    and obj.status == CatalogChangeSet.Status.APPROVED
                ),
            }
        return super().changeform_view(
            request, object_id, form_url, extra_context
        )

    # ------------------------------------------------------------------ #
    # Display helpers                                                     #
    # ------------------------------------------------------------------ #

    _VALUE_LIMIT = 60

    @staticmethod
    def _short_value(value):
        """Human one-liner for an op/report payload value."""
        text = (
            value
            if isinstance(value, str)
            else json.dumps(value, default=str, ensure_ascii=False)
        )
        if len(text) > CatalogChangeSetAdmin._VALUE_LIMIT:
            return text[: CatalogChangeSetAdmin._VALUE_LIMIT - 3] + "..."
        return text

    @admin.display(description="ID", ordering="id")
    def short_id(self, obj):
        return str(obj.id)[:8]

    @admin.display(description="Proposer")
    def proposer(self, obj):
        if obj.proposed_by_credential_id:
            return obj.proposed_by_credential.name
        if obj.proposed_by_user_id:
            return obj.proposed_by_user.email
        return "—"

    @admin.display(description="Ops diff")
    def ops_diff(self, obj):
        """One readable line per proposed op.

        Pure presentation: ``[i] <op name> <target key=value ...>`` with
        update ``set`` overrides spelled out and renames rendered
        ``from → to``. Ops are agent-supplied payloads — every value is
        escaped through ``format_html``.
        """
        if not obj.ops:
            return "—"
        return format_html(
            '<div class="changeset-ops-diff">{}</div>',
            format_html_join(
                "<br>",
                "{}",
                (
                    (self._ops_diff_line(index, op),)
                    for index, op in enumerate(obj.ops)
                ),
            ),
        )

    def _ops_diff_line(self, index, op):
        if not isinstance(op, dict):
            return format_html("[{}] {}", index, self._short_value(op))
        bits = []
        for key, value in op.items():
            if key in ("op", "set"):
                continue
            if key == "to" and "from" in op:
                continue
            if key == "from" and "to" in op:
                bits.append(
                    format_html(
                        "{} → {}",
                        self._short_value(value),
                        self._short_value(op["to"]),
                    )
                )
                continue
            bits.append(
                format_html("{}={}", key, self._short_value(value))
            )
        set_map = op.get("set")
        if isinstance(set_map, dict) and set_map:
            bits.append(
                format_html(
                    "set: {}",
                    format_html_join(
                        ", ",
                        "{}",
                        (
                            (
                                format_html(
                                    "<code>{}</code>={}",
                                    key,
                                    self._short_value(value),
                                ),
                            )
                            for key, value in set_map.items()
                        ),
                    ),
                )
            )
        elif set_map is not None:
            # Malformed `set` (non-dict) still surfaces instead of
            # vanishing — the reviewer should see what was proposed.
            bits.append(format_html("set={}", self._short_value(set_map)))
        return format_html(
            "[{}] <strong>{}</strong> {}",
            index,
            op.get("op", "?"),
            format_html_join(" ", "{}", ((bit,) for bit in bits)),
        )

    _SEVERITY_COLORS = {
        "breaking": "#ba2121",
        "warning": "#b45309",
        "info": "#666666",
    }

    @admin.display(description="Findings")
    def report_findings(self, obj):
        """``validation_report['findings']`` as a severity-badged list."""
        report = obj.validation_report
        if not isinstance(report, dict):
            return "—"
        findings = report.get("findings")
        if not findings:
            # No findings to list — summarize the report shape instead.
            impact = report.get("impact")
            return format_html(
                '<span style="color:#666;">No findings (tiers passed: '
                "{}, impact entries: {}).</span>",
                report.get("tiers_passed", "?"),
                len(impact) if isinstance(impact, list) else "?",
            )
        return format_html(
            '<ul class="changeset-findings" '
            'style="margin:0;padding-left:1.2em;">{}</ul>',
            format_html_join(
                "",
                "{}",
                (
                    (self._finding_line(finding),)
                    for finding in findings
                ),
            ),
        )

    def _finding_line(self, finding):
        if not isinstance(finding, dict):
            return format_html("<li>{}</li>", self._short_value(finding))
        severity = str(finding.get("severity", "info"))
        badge = format_html(
            '<span style="color:{};font-weight:600;'
            'text-transform:uppercase;font-size:0.8em;">{}</span>',
            self._SEVERITY_COLORS.get(severity, "#666666"),
            severity,
        )
        line = format_html(
            "{} <code>{}</code> {}",
            badge,
            finding.get("code", ""),
            finding.get("message", ""),
        )
        context_bits = []
        if finding.get("op_index") is not None:
            context_bits.append(
                format_html("op #{}", finding["op_index"])
            )
        entity = finding.get("entity")
        if isinstance(entity, dict):
            nk = entity.get("nk")
            context_bits.append(
                format_html(
                    "{}: {}",
                    entity.get("model", "?"),
                    ", ".join(str(v) for v in nk)
                    if isinstance(nk, list)
                    else self._short_value(nk),
                )
            )
        if context_bits:
            line = format_html(
                '{} <span style="color:#666;">({})</span>',
                line,
                format_html_join(
                    " · ", "{}", ((bit,) for bit in context_bits)
                ),
            )
        if finding.get("remediation"):
            line = format_html(
                '{}<br><em style="color:#666;">→ {}</em>',
                line,
                finding["remediation"],
            )
        return format_html("<li>{}</li>", line)

    @admin.display(description="Ops (raw)")
    def ops_pretty(self, obj):
        if obj.ops is None:
            return "—"
        return format_html("<pre>{}</pre>", json.dumps(obj.ops, indent=2, default=str))

    @admin.display(description="Validation report (raw)")
    def report_pretty(self, obj):
        if obj.validation_report is None:
            return "—"
        return format_html(
            "<pre>{}</pre>", json.dumps(obj.validation_report, indent=2, default=str)
        )

    @admin.display(description="Post-apply revalidation")
    def post_apply_findings(self, obj):
        """``post_apply_report`` as a summary line or per-schema failure list.

        Pure presentation of the JSON persisted by the post-apply
        revalidation pass — the admin never re-queries the affected
        schemas. The report derives from agent-supplied data, so every
        value is escaped through ``format_html``.
        """
        report = obj.post_apply_report
        if not isinstance(report, dict):
            return "—"
        failing = report.get("failing")
        if not failing:
            # No failing schemas — summarize the run counts instead.
            return format_html(
                '<span style="color:#666;">{} schema(s) revalidated, '
                "{} non-published skipped, cache invalidated: {} "
                "— no failures.</span>",
                report.get("revalidated", "?"),
                report.get("skipped_unpublished", "?"),
                report.get("cache_invalidated", "?"),
            )
        if not isinstance(failing, list):
            # Malformed shape still surfaces instead of vanishing.
            failing = [failing]
        return format_html(
            '<ul class="changeset-findings" '
            'style="margin:0;padding-left:1.2em;">{}</ul>',
            format_html_join(
                "",
                "{}",
                (
                    (self._post_apply_schema_line(entry),)
                    for entry in failing
                ),
            ),
        )

    def _post_apply_schema_line(self, entry):
        """One ``failing`` entry: badge, ``schema_key:version`` and its findings."""
        if not isinstance(entry, dict):
            return format_html("<li>{}</li>", self._short_value(entry))
        findings = entry.get("findings")
        if findings is None:
            findings_list = []
        elif isinstance(findings, list):
            findings_list = findings
        else:
            # Malformed `findings` renders as a single raw line.
            findings_list = [findings]
        badge = format_html(
            '<span style="color:{};font-weight:600;'
            'text-transform:uppercase;font-size:0.8em;">{}</span>',
            self._SEVERITY_COLORS["breaking"],
            "failing",
        )
        line = format_html(
            "{} <code>{}:{}</code>",
            badge,
            entry.get("schema_key", "?"),
            entry.get("version", "?"),
        )
        context_bits = []
        if entry.get("project_id"):
            context_bits.append(
                format_html("project {}", entry["project_id"])
            )
        context_bits.append(
            format_html("{} finding(s)", len(findings_list))
        )
        line = format_html(
            '{} <span style="color:#666;">({})</span>',
            line,
            format_html_join(
                " · ", "{}", ((bit,) for bit in context_bits)
            ),
        )
        if findings_list:
            line = format_html(
                '{}<ul style="margin:0.2em 0 0;'
                'padding-left:1.2em;">{}</ul>',
                line,
                format_html_join(
                    "",
                    "{}",
                    (
                        (self._post_apply_finding_line(finding),)
                        for finding in findings_list
                    ),
                ),
            )
        return format_html("<li>{}</li>", line)

    def _post_apply_finding_line(self, finding):
        """One post-apply finding: ``code`` + ``message`` + node context."""
        if not isinstance(finding, dict):
            return format_html("<li>{}</li>", self._short_value(finding))
        line = format_html(
            "<code>{}</code> {}",
            finding.get("code", ""),
            finding.get("message", ""),
        )
        context_bits = []
        if finding.get("node_type"):
            context_bits.append(
                format_html("type={}", finding["node_type"])
            )
        if finding.get("node_key"):
            context_bits.append(
                format_html("key={}", finding["node_key"])
            )
        if context_bits:
            line = format_html(
                '{} <span style="color:#666;">({})</span>',
                line,
                format_html_join(
                    " · ", "{}", ((bit,) for bit in context_bits)
                ),
            )
        return format_html("<li>{}</li>", line)

    @admin.display(description="Post-apply report (raw)")
    def post_apply_pretty(self, obj):
        if obj.post_apply_report is None:
            return "—"
        return format_html(
            "<pre>{}</pre>",
            json.dumps(obj.post_apply_report, indent=2, default=str),
        )

    def get_queryset(self, request):
        # Audit listing without tenant scoping: the whole admin is
        # superuser-only (see the *_permission hooks below). select_related
        # keeps the proposer and reviewer columns cheap.
        return super().get_queryset(request).select_related(
            "proposed_by_credential",
            "proposed_by_user",
            "reviewed_by",
        )

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        # Read-only object: denying change permission makes Django serve
        # the detail screen without a submit row — a "Save" would bump
        # ``updated_at`` on a no-op write. Mutation paths (approve /
        # reject / apply / supersede) live behind the POST-only lifecycle
        # endpoints
        # above and run through the service layer.
        return False

    def has_add_permission(self, request):
        # Changesets are born through the propose flow, never by hand.
        return False

    def has_delete_permission(self, request, obj=None):
        # Changesets are an audit trail: they are never deleted.
        return False
