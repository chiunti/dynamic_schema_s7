"""Data access layer for CatalogChangeSet entities.

Catalog changesets are the governed mutation path for the catalog: a
proposer (machine credential or human user) submits ops, a reviewer
approves, and the service layer applies them. This repository is the
thin CRUD boundary — it performs defensive input-shape validation and
maps database errors; lifecycle transition enforcement, ops validation
and scope derivation live in the service layer.
"""

import re
import uuid
from collections import Counter
from datetime import datetime
from statistics import mean, median
from typing import Optional

from django.apps import apps
from django.contrib.admin.models import CHANGE
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from ..constants import (
    CHANGESET_STATUS_APPLIED,
    CHANGESET_STATUS_APPROVED,
    CHANGESET_STATUS_DRAFT,
    CHANGESET_STATUS_REJECTED,
    CHANGESET_STATUS_STALE,
    CHANGESET_STATUS_SUPERSEDED,
    CHANGESET_STATUS_VALID,
    CHANGESET_TRANSITIONS,
    ERR_CHANGESET_FIELDS_NOT_MUTABLE,
    ERR_CHANGESET_MUST_BE_INSTANCE,
    ERR_CHANGESET_OPS_MUST_BE_LIST,
    ERR_CHANGESET_PROPOSER_XOR,
    ERR_CHANGESET_RATIONALE_MUST_BE_STRING,
    ERR_CHANGESET_SCOPES_MUST_BE_LIST_OR_NONE,
    ERR_CHANGESET_PATCH_COUNT_MUST_BE_INT,
    ERR_CHANGESET_SINCE_MUST_BE_DATETIME_OR_NONE,
    ERR_CHANGESET_STATUS_MUST_BE_VALID,
    ERR_PK_MUST_BE_VALID_UUID,
)
from ..models import (
    AttributeDef,
    CatalogChangeSet,
    Domain,
    DomainItem,
    NodeType,
    NodeTypeComposition,
    NodeTypeVariant,
    ProjectAPICredential,
)

User = get_user_model()


class CatalogChangeSetRepository:
    """Thin CRUD persistence for catalog changesets.

    Also owns the catalog read that feeds ``base_revision``:
    ``catalog_rows_in_scopes`` returns the rows inside the scopes a
    changeset touches — the snapshot the validator hashes and the apply
    re-verifies.
    """

    # Columns update_fields() will write. The proposer columns
    # (proposed_by_credential / proposed_by_user) are intentionally
    # absent — the proposer is immutable once the row exists (the
    # DB-level XOR CheckConstraint also covers them).
    _MUTABLE_FIELDS = frozenset(
        {
            "status",
            "scopes",
            "validation_report",
            "post_apply_report",
            "base_revision",
            "reviewed_by",
            "review_note",
            "applied_at",
            "ops",
            "rationale",
            "patch_count",
        }
    )

    def create(
        self,
        *,
        ops: list,
        rationale: str,
        proposed_by_credential: Optional[ProjectAPICredential] = None,
        proposed_by_user: Optional[User] = None,
        scopes: Optional[list] = None,
        status: str = CHANGESET_STATUS_DRAFT,
    ) -> CatalogChangeSet:
        """Persist a new changeset row.

        Args:
            ops: ordered op payloads, stored verbatim (the changeset is
                treated as a document — per-op shape validation is the
                validator's concern, not this layer's).
            rationale: proposer-supplied context for the reviewer.
            proposed_by_credential: machine proposer — XOR with
                ``proposed_by_user``.
            proposed_by_user: human proposer — XOR with
                ``proposed_by_credential``.
            scopes: json_scope list the changeset touches; ``None``
                stores ``[]`` — the validator derives and persists the
                real scopes during validation.
            status: initial lifecycle status, draft by default.

        Returns:
            The persisted CatalogChangeSet instance.

        Raises:
            ValueError: exactly one proposer is required
                (``ERR_CHANGESET_PROPOSER_XOR``), ``ops``/``scopes`` is
                not a list, ``status`` is not a lifecycle status, or
                ``IntegrityError`` mapped — contract violation (the
                DB-level proposer XOR CheckConstraint included).
            RuntimeError: ``DatabaseError`` mapped — infrastructure.
        """
        if (proposed_by_credential is None) == (proposed_by_user is None):
            raise ValueError(ERR_CHANGESET_PROPOSER_XOR)
        if not isinstance(ops, list):
            raise ValueError(ERR_CHANGESET_OPS_MUST_BE_LIST)
        if not isinstance(rationale, str):
            raise ValueError(ERR_CHANGESET_RATIONALE_MUST_BE_STRING)
        if scopes is not None and not isinstance(scopes, list):
            raise ValueError(ERR_CHANGESET_SCOPES_MUST_BE_LIST_OR_NONE)
        if status not in CHANGESET_TRANSITIONS:
            raise ValueError(
                ERR_CHANGESET_STATUS_MUST_BE_VALID.format(status=status)
            )
        try:
            return CatalogChangeSet.objects.create(
                status=status,
                ops=ops,
                rationale=rationale,
                scopes=scopes if scopes is not None else [],
                proposed_by_credential=proposed_by_credential,
                proposed_by_user=proposed_by_user,
            )
        except IntegrityError as e:
            raise ValueError(
                f"CatalogChangeSet violates integrity constraints: {e}"
            ) from e
        except DatabaseError as e:
            raise RuntimeError(
                f"Database error during changeset create: {e}"
            ) from e

    def get_by_id(
        self,
        changeset_id: "str | uuid.UUID",
        for_update: bool = False,
    ) -> CatalogChangeSet:
        """Fetch one changeset by primary key.

        Args:
            changeset_id: changeset UUID or its string form.
            for_update: lock the row with ``SELECT ... FOR UPDATE``
                — lifecycle writes (approve/reject/apply/supersede)
                re-read under the lock so the transition gate sees
                the committed status, not the caller's hydrated
                copy.

        Returns:
            The CatalogChangeSet, with proposer/reviewer FKs
            select_related (changelist-style consumers read them).

        Raises:
            ValueError: ``changeset_id`` is not a valid UUID, or
                ``IntegrityError`` mapped — contract violation.
            CatalogChangeSet.DoesNotExist: no row with that id —
                propagated per the ``get_*_by_id`` convention of the
                sibling repositories (e.g. NodeRepository.get_node_by_id).
            RuntimeError: ``DatabaseError`` mapped — infrastructure.
        """
        if not isinstance(changeset_id, (str, uuid.UUID)):
            raise ValueError(
                ERR_PK_MUST_BE_VALID_UUID.format(pk=changeset_id)
            )
        try:
            canonical_id = uuid.UUID(str(changeset_id))
        except (ValueError, AttributeError, TypeError) as e:
            raise ValueError(
                ERR_PK_MUST_BE_VALID_UUID.format(pk=changeset_id)
            ) from e
        queryset = CatalogChangeSet.objects.select_related(
            "proposed_by_credential",
            "proposed_by_user",
            "reviewed_by",
        )
        if for_update:
            # ``of=('self',)`` scopes the lock to the changeset row:
            # the select_related FKs are nullable, and FOR UPDATE on
            # an outer join's nullable side is rejected by Postgres.
            queryset = queryset.select_for_update(of=('self',))
        try:
            return queryset.get(id=canonical_id)
        except IntegrityError as e:
            raise ValueError(
                f"CatalogChangeSet {canonical_id} violates integrity "
                f"constraints: {e}"
            ) from e
        except DatabaseError as e:
            raise RuntimeError(
                f"Database error during changeset get: {e}"
            ) from e

    def update_fields(
        self, changeset: CatalogChangeSet, **fields,
    ) -> CatalogChangeSet:
        """Write allowlisted fields on an existing changeset.

        Only ``_MUTABLE_FIELDS`` columns may be updated — the proposer
        columns are immutable. ``updated_at`` is always appended to the
        ``update_fields`` passed to ``save()``: ``auto_now`` only fires
        through ``pre_save``, which requires the column in
        ``update_fields``.

        Args:
            changeset: the CatalogChangeSet instance to mutate.
            **fields: column → value pairs, all in ``_MUTABLE_FIELDS``.

        Returns:
            The same changeset instance, with the new values set.

        Raises:
            ValueError: ``changeset`` is not a CatalogChangeSet, any
                field falls outside ``_MUTABLE_FIELDS``, or
                ``IntegrityError`` mapped — contract violation.
            RuntimeError: ``DatabaseError`` mapped — infrastructure.
                ``save(update_fields=...)`` raising "did not affect any
                rows" (the instance's pk matches no row) maps here.
        """
        if not isinstance(changeset, CatalogChangeSet):
            raise ValueError(ERR_CHANGESET_MUST_BE_INSTANCE)
        rejected = sorted(set(fields) - self._MUTABLE_FIELDS)
        if rejected:
            raise ValueError(
                ERR_CHANGESET_FIELDS_NOT_MUTABLE.format(rejected=rejected)
            )
        if "status" in fields and fields["status"] not in CHANGESET_TRANSITIONS:
            raise ValueError(
                ERR_CHANGESET_STATUS_MUST_BE_VALID.format(
                    status=fields["status"]
                )
            )
        if "ops" in fields and not isinstance(fields["ops"], list):
            raise ValueError(ERR_CHANGESET_OPS_MUST_BE_LIST)
        if "scopes" in fields and not isinstance(fields["scopes"], list):
            raise ValueError(ERR_CHANGESET_SCOPES_MUST_BE_LIST_OR_NONE)
        if "rationale" in fields and not isinstance(fields["rationale"], str):
            raise ValueError(ERR_CHANGESET_RATIONALE_MUST_BE_STRING)
        if "patch_count" in fields and (
            not isinstance(fields["patch_count"], int)
            or isinstance(fields["patch_count"], bool)
        ):
            raise ValueError(ERR_CHANGESET_PATCH_COUNT_MUST_BE_INT)
        for name, value in fields.items():
            setattr(changeset, name, value)
        changeset.updated_at = timezone.now()
        fields_to_save = set(fields)
        fields_to_save.add("updated_at")
        try:
            changeset.save(update_fields=list(fields_to_save))
        except IntegrityError as e:
            raise ValueError(
                f"CatalogChangeSet {changeset.pk} violates integrity "
                f"constraints: {e}"
            ) from e
        except DatabaseError as e:
            raise RuntimeError(
                f"Database error during changeset {changeset.pk} "
                f"update: {e}"
            ) from e
        return changeset

    def list_changesets(
        self,
        status: Optional[str] = None,
        scopes: Optional[list] = None,
    ) -> QuerySet:
        """Changesets newest-first, optionally filtered.

        Args:
            status: exact lifecycle status filter; must be one of the
                ``CHANGESET_TRANSITIONS`` statuses when given.
            scopes: when given, keeps changesets whose ``scopes`` JSONB
                array intersects this list — ``has_any_keys`` compiles
                to PostgreSQL ``?|``, which on a top-level JSONB array
                checks element presence (array intersection semantics).

        Returns:
            A lazy QuerySet ordered by ``-created_at``, with
            proposer/reviewer FKs select_related. Nothing executes here,
            so evaluation-time ``DatabaseError`` surfaces to the caller
            unmapped — same convention as the lazy listings of the
            sibling repositories.

        Raises:
            ValueError: ``status`` is not a lifecycle status, or
                ``scopes`` is not a list.
        """
        qs = (
            CatalogChangeSet.objects
            .select_related(
                "proposed_by_credential",
                "proposed_by_user",
                "reviewed_by",
            )
            .order_by("-created_at", "-id")
        )
        if status is not None:
            if status not in CHANGESET_TRANSITIONS:
                raise ValueError(
                    ERR_CHANGESET_STATUS_MUST_BE_VALID.format(status=status)
                )
            qs = qs.filter(status=status)
        if scopes is not None:
            if not isinstance(scopes, list):
                raise ValueError(ERR_CHANGESET_SCOPES_MUST_BE_LIST_OR_NONE)
            qs = qs.filter(scopes__has_any_keys=scopes)
        return qs

    def catalog_rows_in_scopes(
        self, scopes: Optional[list],
    ) -> dict[str, list]:
        """Catalog rows belonging to the given ``json_scope`` values.

        Read-side of ``base_revision``: returns, per writable catalog
        model label, the materialized rows the scope snapshot covers —
        the node types carrying one of the scopes plus the rows
        transitively attached to them:

        - ``nodetype`` — ``NodeType.json_scope`` matching an element of
          ``scopes``. A ``None`` element selects scopeless node types
          (``json_scope IS NULL``); ``""`` selects rows storing the
          literal empty string — the two are kept distinct because
          scopes are derived server-side and fidelity matters. A plain
          ``json_scope__in`` lookup cannot express the ``None`` arm:
          Django discards ``None`` elements from ``__in`` lists
          (``NULL IN (...)`` never matches), so the null branch is
          OR'ed in explicitly.
        - ``attributedef`` — defs bound to an in-scope node type
          (``variant_key``-scoped defs included: they still hang off
          the node type). Global defs (``node_type IS NULL``) are
          always excluded — they belong to no scope; the validator
          reports them as scope-agnostic shared grammar when an op
          touches them, outside the scope snapshot.
        - ``nodetypevariant`` — variants whose ``node_type`` is in
          scope.
        - ``nodetypecomposition`` — outgoing compositions only
          (``parent_type`` in scope): the edge is owned by its parent,
          so a composition whose parent lies outside the scope does not
          belong to the child's scope even when the child is in scope.
        - ``domain`` — domains referenced (``AttributeDef.domain``) by
          an in-scope def; a domain is of the scope when a def of an
          in-scope node type binds it.
        - ``domainitem`` — items of an in-scope domain.

        The labels mirror ``CATALOG_MODELS`` (and
        ``CatalogUsageRepository._DISPATCH``); all six are always
        present. Each list is ordered by ``pk`` — the hash
        serialization requires a stable row order — and the FKs the
        natural keys traverse are ``select_related`` so serializing
        the result does not N+1.

        Args:
            scopes: ``json_scope`` values selecting the snapshot —
                ``str`` elements, with ``None``/``""`` handled as
                documented above. ``None`` (or ``[]``) as the argument
                means "no scope selected" and returns every label
                empty; elements of other types are not validated and
                simply cannot match (or surface as a mapped
                ``DatabaseError``).

        Returns:
            ``{label: [model instances]}`` — lists materialized here so
            query errors are mapped in this layer rather than at the
            caller's iteration (unlike the lazy ``list_changesets``).

        Raises:
            ValueError: ``scopes`` is neither a list nor ``None``, or
                ``IntegrityError`` mapped — contract violation.
            RuntimeError: ``DatabaseError`` mapped — infrastructure.
        """
        if scopes is None:
            scopes = []
        if not isinstance(scopes, list):
            raise ValueError(ERR_CHANGESET_SCOPES_MUST_BE_LIST_OR_NONE)

        labels = (
            "nodetype", "attributedef", "nodetypevariant",
            "nodetypecomposition", "domain", "domainitem",
        )
        if not scopes:
            return {label: [] for label in labels}

        try:
            concrete_scopes = [s for s in scopes if s is not None]
            scope_q = Q()
            if concrete_scopes:
                scope_q |= Q(json_scope__in=concrete_scopes)
            if len(concrete_scopes) != len(scopes):
                scope_q |= Q(json_scope__isnull=True)
            node_types = list(
                NodeType.objects.filter(scope_q).order_by("pk")
            )
            node_type_ids = [nt.pk for nt in node_types]

            attribute_defs = list(
                AttributeDef.objects
                .select_related("node_type", "data_type", "domain")
                .filter(node_type_id__in=node_type_ids)
                .order_by("pk")
            )
            domain_ids = {
                attribute_def.domain_id
                for attribute_def in attribute_defs
                if attribute_def.domain_id is not None
            }
            return {
                "nodetype": node_types,
                "attributedef": attribute_defs,
                "nodetypevariant": list(
                    NodeTypeVariant.objects
                    .select_related("node_type", "props_node_type")
                    .filter(node_type_id__in=node_type_ids)
                    .order_by("pk")
                ),
                "nodetypecomposition": list(
                    NodeTypeComposition.objects
                    .select_related("parent_type", "child_type")
                    .filter(parent_type_id__in=node_type_ids)
                    .order_by("pk")
                ),
                "domain": list(
                    Domain.objects
                    .filter(pk__in=domain_ids)
                    .order_by("pk")
                ),
                "domainitem": list(
                    DomainItem.objects
                    .select_related("domain")
                    .filter(domain_id__in=domain_ids)
                    .order_by("pk")
                ),
            }
        except IntegrityError as e:
            raise ValueError(
                f"Catalog rows in scopes violate integrity "
                f"constraints: {e}"
            ) from e
        except DatabaseError as e:
            raise RuntimeError(
                f"Database error during catalog rows in scopes "
                f"fetch: {e}"
            ) from e

    def record_changeset_action(
        self, user, changeset: CatalogChangeSet, change_message: str,
    ) -> None:
        """Attribute a changeset lifecycle action to ``user``.

        Audit bookkeeping for the governed mutation path — the
        service calls it inside its own ``atomic`` block (approve,
        reject, apply, supersede, mark-stale), so the LogEntry rides
        the same
        transaction as the lifecycle write it records: the entry
        commits iff the transition commits.

        The changeset itself is the audit subject: the entry is
        anchored to the ``CatalogChangeSet`` content type, carries
        the changeset's pk as ``object_id`` and a short
        ``pk-prefix (status)`` label as ``object_repr``, always with
        ``action_flag=CHANGE`` — lifecycle verbs live in
        ``change_message``.

        Best-effort inside its own savepoint: any failure is
        swallowed so bookkeeping can never break or poison the
        caller's transaction. Users without a pk are skipped
        silently — project principals produce no entry; they are
        audited by the token used.

        Args:
            user: principal performing the action; must expose
                ``pk`` or no entry is written.
            changeset: the changeset the action ran on — its pk and
                current status anchor the entry.
            change_message: audit description of the action.
        """
        user_id = getattr(user, "pk", None)
        if user_id is None:
            return
        try:
            log_entry = apps.get_model("admin", "LogEntry")
            content_type = apps.get_model("contenttypes", "ContentType")
            with transaction.atomic():
                log_entry.objects.create(
                    user_id=user_id,
                    content_type_id=content_type.objects
                    .get_for_model(CatalogChangeSet).pk,
                    object_id=str(changeset.pk),
                    object_repr=(
                        f"changeset {str(changeset.pk)[:8]} "
                        f"({changeset.status})"
                    ),
                    action_flag=CHANGE,
                    change_message=change_message,
                )
        except Exception:
            pass

    # Final-state "was valid" numerator: ``valid`` itself plus the
    # statuses only reachable through it — ``approved`` (``valid →
    # approved``), ``applied`` (``approved → applied``) and ``stale``
    # (``approved → stale`` is its only inbound edge, so a stale row
    # is proven to have passed valid+approved — drift at apply time
    # does not erase that). ``superseded`` and ``rejected`` are
    # excluded — both are reachable from non-valid statuses, so the
    # snapshot cannot tell whether they ever were valid (conservative
    # undercount).
    _METRIC_VALID_OR_BETTER = frozenset(
        {
            CHANGESET_STATUS_VALID,
            CHANGESET_STATUS_APPROVED,
            CHANGESET_STATUS_APPLIED,
            CHANGESET_STATUS_STALE,
        }
    )
    # Review verdicts: the approve arm (approved — with ``applied``
    # folded in as the approved-and-executed end state) versus the
    # reject arm. ``stale``/``superseded`` are operational outcomes,
    # not verdicts — they stay out of this denominator.
    _METRIC_VERDICT_ACCEPT = frozenset(
        {CHANGESET_STATUS_APPROVED, CHANGESET_STATUS_APPLIED}
    )
    # ``updated_at`` last moved with the decision write for these
    # statuses — the "time to verdict" proxy.
    _METRIC_DECIDED = frozenset(
        {
            CHANGESET_STATUS_APPROVED,
            CHANGESET_STATUS_REJECTED,
            CHANGESET_STATUS_APPLIED,
        }
    )

    def changeset_metrics(self, since: Optional[datetime] = None) -> dict:
        """Snapshot metrics over the changeset loop's final state.

        Read-only aggregation feeding the private ``changeset_metrics``
        command — a point-in-time report over the rows' CURRENT status
        plus the audit LogEntry trail, not an event reconstruction.
        JSON blobs (``ops``, ``validation_report``) are aggregated in
        Python at snapshot scale rather than via jsonb operators.

        Report shape::

            {
                "window": {"since": iso|None, "total": int},
                "by_status": {status: int},
                "rates": {
                    "proposed_total": int,          # denominator below
                    "valid_or_better": {"count": int, "ratio": f|None},
                    "approved_or_applied": int,
                    "rejected": int,
                    "verdict_total": int,           # approved_or_applied
                                                  # + rejected
                    "approve_ratio": f|None,
                    "reject_ratio": f|None,
                    "superseded": {"count": int, "ratio": f|None},
                },
                "iterations": {
                    "patch_rounds_per_changeset":
                        {"mean": f|None, "median": f|None, "max": int},
                    "patch_rounds_to_valid":
                        {"mean": f|None, "median": f|None, "max": int},
                    "audit_entries_per_changeset":
                        {"mean": f|None, "median": f|None, "max": int},
                    "audited_changesets": int,
                    "by_verb": {verb: int},
                },
                "tiers_passed": {tier_value|unvalidated|unknown: int},
                "top_ops": [{"op": str, "count": int}],          # <=5
                "top_findings": [{"code": str, "count": int}],   # <=5
                "durations_seconds": {
                    "applied": {"count": int, "mean": f|None,
                                "median": f|None},
                    "decided": {"count": int, "mean": f|None,
                                "median": f|None},
                },
            }

        Denominators, populations and heuristics:

        - ``proposed_total`` = every changeset that left ``draft``
          (``total − draft``): all non-draft resting statuses passed
          through ``proposed``, so it is the honest "was proposed"
          population. Transient ``proposed``/``validating`` rows count
          too — none rest there, but a mid-request snapshot could
          carry them.
        - ``valid_or_better.ratio`` = (valid+approved+applied+stale) /
          proposed_total — see ``_METRIC_VALID_OR_BETTER``: ``stale``
          counts because ``approved → stale`` is its only inbound edge
          (a stale row provably passed valid+approved), while
          ``superseded``/``rejected`` stay out — both are reachable
          from non-valid statuses, so the snapshot cannot tell whether
          they ever were valid (conservative undercount).
        - ``verdict_total`` = approved_or_applied + rejected — the
          shared denominator ``approve_ratio``/``reject_ratio`` divide
          by, exported so consumers never recompute it; both ratios
          are ``None`` when no verdict exists yet. ``stale`` is no
          verdict arm: it is an apply-time drift outcome, not a review
          decision.
        - ``superseded.ratio`` = superseded / proposed_total.
        - ``top_ops`` / ``top_findings`` / ``tiers_passed`` read the
          NON-DRAFT population (the same ``exclude(status='draft')``
          as the patch-round arms): drafts never entered the propose
          loop, so their payloads would report ops and validations
          that never ran. ``by_status`` is the deliberate exception —
          it lists every in-window row, drafts included.
          ``top_findings`` groups by the finding ``code`` alone;
          findings without a ``code`` and non-dict findings land in
          the ``"<missing>"`` bucket — the same sentinel ops without
          an ``op`` key use.
        - Iterations carry two distinct populations:
          ``patch_rounds_per_changeset`` aggregates the persisted
          ``patch_count`` column over ALL NON-DRAFT changesets —
          ``propose()`` writes 1 and each ``patch()`` adds 1 inside
          the same transaction as the changeset write; ``invalid`` /
          ``superseded`` / transient rows count too (their counters
          censor mid-loop), so read it as "rounds spent per changeset
          that left draft". ``patch_rounds_to_valid`` narrows to the
          reached-valid final statuses (``_METRIC_VALID_OR_BETTER``)
          — the "rounds to a valid proposal" distribution; a row
          resting at ``invalid``/``rejected``/``superseded`` cannot be
          proven to have passed ``valid`` and stays out. The counter
          exists because proposer-side actions produce no
          ``LogEntry``: token principals carry no user pk, so the
          audit trail can never count the agent path.
          ``audit_entries_per_changeset``, ``audited_changesets`` and
          ``by_verb`` still describe the reviewer-side
          ``record_changeset_action`` LogEntry trail (changeset
          content type, ``object_id=str(pk)``,
          ``action_flag=CHANGE``, verb = the ``change_message`` head —
          the text before the first ``' — '`` or ``':'``). Their
          population is ALL in-window changesets — drafts included,
          zero-filled — deliberately wider than the patch-round arms
          (non-draft / reached-valid): audit coverage is measured
          over the whole window.
        - ``durations_seconds.applied`` = ``applied_at − created_at``
          over ``applied`` rows with ``applied_at`` set;
          ``decided`` = ``updated_at − created_at`` over
          ``_METRIC_DECIDED`` rows.

        Args:
            since: aware or naive datetime lower bound on
                ``created_at``; ``None`` aggregates all rows.

        Returns:
            The JSON-serializable report dict described above.

        Raises:
            ValueError: ``since`` is neither a datetime nor ``None``.
            RuntimeError: ``DatabaseError`` mapped — infrastructure;
                a missing ``CatalogChangeSet`` content type
                (``ObjectDoesNotExist`` from ``get_for_model``, which
                is no ``DatabaseError``) maps there too.
        """
        if since is not None and not isinstance(since, datetime):
            raise ValueError(
                ERR_CHANGESET_SINCE_MUST_BE_DATETIME_OR_NONE.format(
                    since=since
                )
            )
        try:
            queryset = CatalogChangeSet.objects.all()
            if since is not None:
                queryset = queryset.filter(created_at__gte=since)

            total = queryset.count()
            by_status = {
                row["status"]: row["n"]
                for row in queryset.values("status").annotate(
                    n=Count("id")
                )
            }

            proposed_total = total - by_status.get(
                CHANGESET_STATUS_DRAFT, 0
            )
            valid_or_better = sum(
                by_status.get(status, 0)
                for status in self._METRIC_VALID_OR_BETTER
            )
            approved_or_applied = sum(
                by_status.get(status, 0)
                for status in self._METRIC_VERDICT_ACCEPT
            )
            rejected = by_status.get(CHANGESET_STATUS_REJECTED, 0)
            verdicts = approved_or_applied + rejected
            superseded = by_status.get(CHANGESET_STATUS_SUPERSEDED, 0)

            # Ops/findings/tiers aggregate the non-draft population
            # only — drafts never entered the propose loop, so their
            # raw payloads would count ops and validations that never
            # ran. ``by_status`` above is the deliberate exception:
            # it lists every in-window row, drafts included.
            non_draft = queryset.exclude(status=CHANGESET_STATUS_DRAFT)
            op_counter: Counter = Counter()
            finding_counter: Counter = Counter()
            tiers_counter: Counter = Counter()
            for ops, report in non_draft.values_list(
                "ops", "validation_report"
            ):
                if isinstance(ops, list):
                    for op in ops:
                        name = op.get("op") if isinstance(op, dict) else None
                        op_counter[
                            str(name) if name is not None else "<missing>"
                        ] += 1
                if isinstance(report, dict):
                    tiers = report.get("tiers_passed")
                    tiers_counter[
                        str(tiers) if tiers is not None else "unknown"
                    ] += 1
                    findings = report.get("findings")
                    if isinstance(findings, list):
                        for finding in findings:
                            # Grouped by ``code`` alone; a missing
                            # key — or a non-dict finding — lands in
                            # the ``<missing>`` bucket like ops do.
                            code = (
                                finding.get("code")
                                if isinstance(finding, dict)
                                else None
                            )
                            finding_counter[
                                str(code) if code is not None else "<missing>"
                            ] += 1
                else:
                    tiers_counter["unvalidated"] += 1

            # Audit trail: LogEntry rows anchored to this content type
            # whose object_id points at an in-window changeset.
            log_entry = apps.get_model("admin", "LogEntry")
            content_type = apps.get_model("contenttypes", "ContentType")
            changeset_ids = [
                str(pk) for pk in queryset.values_list("id", flat=True)
            ]
            entries = (
                log_entry.objects.filter(
                    content_type_id=content_type.objects
                    .get_for_model(CatalogChangeSet).pk,
                    action_flag=CHANGE,
                    object_id__in=changeset_ids,
                ).values_list("object_id", "change_message")
            )
            all_counts: Counter = Counter()
            verb_counter: Counter = Counter()
            for object_id, change_message in entries:
                verb = re.split(
                    r"\s—\s|:", change_message or "", maxsplit=1
                )[0].strip()
                verb_counter[verb] += 1
                all_counts[object_id] += 1
            audit_counts = [
                all_counts.get(changeset_id, 0)
                for changeset_id in changeset_ids
            ]

            # Iteration counts come from the persisted patch_count
            # column — the LogEntry trail cannot supply them:
            # proposer-side writes produce no entries for token
            # principals. Two populations:
            # ``patch_rounds_per_changeset`` reads every non-draft row
            # (drafts never entered the feedback loop) — invalid /
            # superseded / transient counters censor mid-loop but
            # still measure rounds spent; ``patch_rounds_to_valid``
            # narrows to the reached-valid final statuses
            # (``_METRIC_VALID_OR_BETTER``) for the honest
            # "rounds to a valid proposal" distribution.
            patch_counts = [
                count if count is not None else 0
                for count in non_draft.values_list(
                    "patch_count", flat=True
                )
            ]
            valid_patch_counts = [
                count if count is not None else 0
                for count in non_draft.filter(
                    status__in=self._METRIC_VALID_OR_BETTER
                ).values_list("patch_count", flat=True)
            ]

            applied_durations = [
                (applied_at - created_at).total_seconds()
                for created_at, applied_at in queryset.filter(
                    status=CHANGESET_STATUS_APPLIED,
                    applied_at__isnull=False,
                ).values_list("created_at", "applied_at")
            ]
            decided_durations = [
                (updated_at - created_at).total_seconds()
                for created_at, updated_at in queryset.filter(
                    status__in=self._METRIC_DECIDED
                ).values_list("created_at", "updated_at")
            ]
        except IntegrityError as e:
            raise ValueError(
                f"CatalogChangeSet metrics violate integrity "
                f"constraints: {e}"
            ) from e
        except ObjectDoesNotExist as e:
            # ``get_for_model`` hits the contenttype table on a cache
            # miss — a missing CatalogChangeSet ContentType row is
            # infrastructure failure (``DoesNotExist`` is no
            # ``DatabaseError``), mapped like one per this layer's
            # convention.
            raise RuntimeError(
                f"CatalogChangeSet content type missing during "
                f"changeset metrics: {e}"
            ) from e
        except DatabaseError as e:
            raise RuntimeError(
                f"Database error during changeset metrics: {e}"
            ) from e

        def _ratio(part: int, whole: int) -> Optional[float]:
            return part / whole if whole else None

        def _distribution(values: list) -> dict:
            return {
                "mean": mean(values) if values else None,
                "median": median(values) if values else None,
                "max": max(values) if values else 0,
            }

        def _duration_stats(values: list) -> dict:
            return {
                "count": len(values),
                "mean": mean(values) if values else None,
                "median": median(values) if values else None,
            }

        return {
            "window": {
                "since": since.isoformat() if since is not None else None,
                "total": total,
            },
            "by_status": by_status,
            "rates": {
                "proposed_total": proposed_total,
                "valid_or_better": {
                    "count": valid_or_better,
                    "ratio": _ratio(valid_or_better, proposed_total),
                },
                "approved_or_applied": approved_or_applied,
                "rejected": rejected,
                "verdict_total": verdicts,
                "approve_ratio": _ratio(approved_or_applied, verdicts),
                "reject_ratio": _ratio(rejected, verdicts),
                "superseded": {
                    "count": superseded,
                    "ratio": _ratio(superseded, proposed_total),
                },
            },
            "iterations": {
                "patch_rounds_per_changeset": _distribution(
                    patch_counts
                ),
                "patch_rounds_to_valid": _distribution(
                    valid_patch_counts
                ),
                "audit_entries_per_changeset": _distribution(audit_counts),
                "audited_changesets": len(all_counts),
                "by_verb": dict(verb_counter.most_common()),
            },
            "tiers_passed": dict(
                sorted(tiers_counter.items(), key=lambda item: item[0])
            ),
            "top_ops": [
                {"op": name, "count": count}
                for name, count in op_counter.most_common(5)
            ],
            "top_findings": [
                {"code": code, "count": count}
                for code, count in finding_counter.most_common(5)
            ],
            "durations_seconds": {
                "applied": _duration_stats(applied_durations),
                "decided": _duration_stats(decided_durations),
            },
        }
