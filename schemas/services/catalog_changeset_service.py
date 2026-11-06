"""Three-tier validation for governed catalog changesets.

A ``CatalogChangeSet`` carries an ordered list of mutation ops against
the shared catalog grammar — the same six models the catalog import
endpoint writes (``nodetype``, ``attributedef``, ``domain``,
``domainitem``, ``nodetypevariant``, ``nodetypecomposition``;
datatypes stay excluded, as in the direct import). This service
validates those ops in three tiers of increasing cost and persists the
structured ``validation_report`` the proposer reads to correct and
re-propose:

1. **Syntactic** — every op is a well-formed dict whose ``op`` name is
   known and whose required fields are present; every natural-key
   reference resolves (against the live catalog or rows created by
   sibling ops); no row is targeted twice; adds may not name an
   existing row while updates, removes and renames must name one.
2. **Post-application coherence** — the ops compile to fixture-format
   objects and run through the real apply machinery of the catalog
   import inside a savepoint (no permission gate, no audit record —
   everything rolls back). On top of the import's own validation the
   post-merge catalog is checked for broken or ambiguous variant
   discriminators, dangling ``props_node_type`` references, duplicated
   ``json_key`` positions per (node type, variant key), orphan
   compositions and composition cycles.
3. **Impact (reachable set)** — for every catalog row the ops touch,
   the implementation usage that already exists is reported, so the
   reviewer and the proposer see which schemas realize each row before
   the changeset is applied.

``scopes`` — the ``json_scope`` values the ops touch — is derived
server-side so a proposer can never narrow its own perimeter, and
``base_revision`` — a sha256 over the catalog rows inside those
scopes — is recomputed on every validation for the optimistic
concurrency check at apply time. Ops compile to the fixture format the
import consumes; the governed apply reuses the same translation, so
validation always runs exactly what approval would apply.

The report is structured and classified::

    {
        "findings": [
            {
                "op_index": int | None,
                "severity": "breaking" | "warning" | "info",
                "entity": {"model": str, "nk": [..]} | None,
                "code": str,
                "message": str,
                "remediation": str,       # optional
            },
        ],
        "impact": [
            {
                "entity": {"model": str, "nk": [..]},
                "implemented": bool,
                "usage": {<metric>: int, "projects": [..]},
                "projects": [..],
            },
        ],
        "tiers_passed": int,              # 0..3
    }

Any ``breaking`` finding marks the changeset ``invalid``; a clean run
marks it ``valid``. Destructive ops on implemented rows always produce
breaking findings; mutative ops report their impact as warnings;
additive ops are informational.

The REST-facing contract of the feedback loop lives here too:
``propose()`` walks a fresh row through ``draft → proposed`` into
``validate()`` — validation is synchronous, in-request — ``patch()``
replaces ops/rationale on a re-proposable changeset (every status
carrying a ``→ validating`` edge: ``proposed``, ``invalid``,
``valid``, ``stale``) and re-validates, and ``get_changeset()`` is
the plain fetch. Both mutating entry points accept an injected
``scope_check`` predicate that runs AFTER validation by design:
``scopes`` — the perimeter the capability covers — only exists once
``validate()`` derives it. A scope rejection raises
``PermissionError`` inside the method's ``atomic`` block, so a
refused proposal rolls back and never persists.

The reviewer arm closes the loop — admin-facing only, never
exposed to credentials: ``approve()`` and ``reject()`` move a
reviewable changeset to its verdict with ``reviewed_by``
recorded, and ``apply()`` materializes an approved changeset
through the same compile-and-import path the validator probed,
gated by a ``base_revision`` re-check and a live usage re-measure
that mark the changeset ``stale`` (applying nothing) when the
catalog or its implementation surface drifted under review.
Every lifecycle write is attributed through a best-effort admin
``LogEntry``.
"""

import hashlib
import json
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from django.db import transaction
from django.utils import timezone

from ..constants import (
    CHANGESET_STATUS_APPLIED,
    CHANGESET_STATUS_APPROVED,
    CHANGESET_STATUS_DRAFT,
    CHANGESET_STATUS_INVALID,
    CHANGESET_STATUS_PROPOSED,
    CHANGESET_STATUS_REJECTED,
    CHANGESET_STATUS_STALE,
    CHANGESET_STATUS_SUPERSEDED,
    CHANGESET_STATUS_VALID,
    CHANGESET_STATUS_VALIDATING,
    CHANGESET_TRANSITIONS,
    ERR_CATALOG_OBJECT_IN_USE,
    ERR_CATALOG_OBJECT_IN_USE_MSG,
    ERR_CHANGESET_APPLY_FAILED,
    ERR_CHANGESET_APPLY_FAILED_MSG,
    ERR_CHANGESET_COMPOSITION_CYCLE,
    ERR_CHANGESET_DISCRIMINATOR_AMBIGUOUS,
    ERR_CHANGESET_DISCRIMINATOR_UNRESOLVED,
    ERR_CHANGESET_DUP_JSON_KEY,
    ERR_CHANGESET_INVALID_TRANSITION,
    ERR_CHANGESET_OP_DUP_TARGET,
    ERR_CHANGESET_OP_FIELD_INVALID,
    ERR_CHANGESET_OP_FIELD_MISSING,
    ERR_CHANGESET_OP_MALFORMED,
    ERR_CHANGESET_OP_TARGET_EXISTS,
    ERR_CHANGESET_OP_TARGET_NOT_FOUND,
    ERR_CHANGESET_OP_UNKNOWN,
    ERR_CHANGESET_ORPHAN_COMPOSITION,
    ERR_CHANGESET_PROPS_TYPE_UNRESOLVED,
    ERR_CHANGESET_SCOPES_NOT_COVERED_MSG,
    ERR_CHANGESET_VARIANT_KEY_UNRESOLVED,
    INFO_CHANGESET_ADDITIVE,
    STATUS_PUBLISHED,
)
from ..models import CatalogChangeSet, ProjectAPICredential
from ..repositories.attribute_def_repository import AttributeDefRepository
from ..repositories.catalog_changeset_repository import (
    CatalogChangeSetRepository,
)
from ..repositories.catalog_usage_repository import CatalogUsageRepository
from ..repositories.composition_repository import CompositionRepository
from ..repositories.fixture_repository import FixtureRepository
from ..repositories.node_type_repository import NodeTypeRepository
from ..repositories.schema_repository import SchemaRepository
from .catalog_import_service import CatalogImportService
from .exceptions import (
    ChangesetNotFoundError,
    ChangesetStateConflictError,
)
from .fixture_service import FK_FIELDS, NATURAL_KEY_FIELDS
from .schema_validation_service import SchemaValidationService

if TYPE_CHECKING:
    from ..repositories.user_repository import User

# Path component emitted by the import envelope for per-object errors.
_OBJECT_PATH_RE = re.compile(r'^\$\.objects\[(\d+)\]$')

# Finding severities persisted inside validation_report.
SEVERITY_BREAKING = 'breaking'
SEVERITY_WARNING = 'warning'
SEVERITY_INFO = 'info'

_SEVERITY_RANK = {
    SEVERITY_BREAKING: 0,
    SEVERITY_WARNING: 1,
    SEVERITY_INFO: 2,
}

# Fallback detail when an import failure envelope carries no error
# message — shared by the finding mapper and the governed apply.
_APPLY_ERROR_FALLBACK = 'catalog apply failed'


class _TrialApplyRollback(Exception):
    """Sentinel that forces the tier-2 savepoint to roll back.

    The trial apply runs the real import machinery inside a savepoint
    purely for validation — its writes must never commit. Raising this
    sentinel inside the ``atomic`` block reverts them unconditionally,
    whatever the apply returned.
    """


@dataclass
class _OpContext:
    """Working state of one op through the validation pipeline."""

    index: int
    name: str
    spec: dict
    op: dict
    nk: tuple
    resolved_pk: Optional[str] = None
    live_fields: Optional[dict] = None
    ok: bool = True

    @property
    def label(self) -> str:
        return self.spec['model']

    @property
    def kind(self) -> str:
        return self.spec['kind']

    @property
    def risk(self) -> str:
        return self.spec['risk']

    @property
    def entity(self) -> dict:
        """Report identity: catalog model + natural key of the target."""
        return {'model': self.label, 'nk': list(self.nk)}


class CatalogChangeSetService:
    """Validate governed catalog changesets in three tiers.

    Collaborators are instantiated per the service convention of the
    package — the changeset repository for status/report persistence,
    the usage repository for the tier-3 reachable set, the import
    service for the tier-2 trial apply and the catalog repositories
    for natural-key resolution and post-merge coherence checks.
    """

    # Operation vocabulary over the six writable catalog models.
    # ``kind`` is the write shape (add/update/rename/remove);
    # ``risk`` is the enforcement class of the design — additive ops
    # report info, mutative ops report their impact as warnings,
    # destructive ops may only apply to objects with zero usage.
    # ``required`` lists the mandatory op fields (``set`` on update ops
    # is the non-empty dict of field overrides).
    _OP_SPECS = {
        # Additive — creates only; a target that already exists is
        # rejected (the intent is declared as "new").
        'add_node_type': {
            'model': 'nodetype', 'kind': 'add', 'risk': 'additive',
            'required': ('name', 'label'),
        },
        'add_attribute_def': {
            'model': 'attributedef', 'kind': 'add', 'risk': 'additive',
            'required': ('json_key', 'data_type'),
        },
        'add_domain': {
            'model': 'domain', 'kind': 'add', 'risk': 'additive',
            'required': ('domain_name',),
        },
        'add_domain_item': {
            'model': 'domainitem', 'kind': 'add', 'risk': 'additive',
            'required': ('domain', 'value', 'label'),
        },
        'add_composition': {
            'model': 'nodetypecomposition', 'kind': 'add',
            'risk': 'additive',
            'required': ('parent_type', 'child_type'),
        },
        'add_variant': {
            'model': 'nodetypevariant', 'kind': 'add', 'risk': 'additive',
            'required': ('node_type', 'variant_key'),
        },
        # Mutative — full-field ``set`` overrides on a live row,
        # attribute renames, and domain-item removal (classified
        # mutative by the risk table: the row is content of its domain,
        # not a peer contract).
        'update_node_type': {
            'model': 'nodetype', 'kind': 'update', 'risk': 'mutative',
            'required': ('name', 'set'),
        },
        'update_attribute_def': {
            'model': 'attributedef', 'kind': 'update', 'risk': 'mutative',
            'required': ('json_key', 'set'),
        },
        'update_domain': {
            'model': 'domain', 'kind': 'update', 'risk': 'mutative',
            'required': ('domain_name', 'set'),
        },
        'update_domain_item': {
            'model': 'domainitem', 'kind': 'update', 'risk': 'mutative',
            'required': ('domain', 'value', 'set'),
        },
        'update_composition': {
            'model': 'nodetypecomposition', 'kind': 'update',
            'risk': 'mutative',
            'required': ('parent_type', 'child_type', 'set'),
        },
        'update_variant': {
            'model': 'nodetypevariant', 'kind': 'update',
            'risk': 'mutative',
            'required': ('node_type', 'variant_key', 'set'),
        },
        'rename_attribute': {
            'model': 'attributedef', 'kind': 'rename', 'risk': 'mutative',
            'required': ('node_type', 'from', 'to'),
        },
        'remove_domain_item': {
            'model': 'domainitem', 'kind': 'remove', 'risk': 'mutative',
            'required': ('domain', 'value'),
        },
        # Destructive — only applies to objects with zero measured
        # implementation.
        'remove_node_type': {
            'model': 'nodetype', 'kind': 'remove', 'risk': 'destructive',
            'required': ('name',),
        },
        'remove_attribute_def': {
            'model': 'attributedef', 'kind': 'remove',
            'risk': 'destructive',
            'required': ('json_key',),
        },
        'remove_domain': {
            'model': 'domain', 'kind': 'remove', 'risk': 'destructive',
            'required': ('domain_name',),
        },
        'remove_composition': {
            'model': 'nodetypecomposition', 'kind': 'remove',
            'risk': 'destructive',
            'required': ('parent_type', 'child_type'),
        },
        'remove_variant': {
            'model': 'nodetypevariant', 'kind': 'remove',
            'risk': 'destructive',
            'required': ('node_type', 'variant_key'),
        },
    }

    # Op fields that must carry a string (or null on nullable
    # positions) when present — wrongly-typed values are reported
    # before any resolution runs.
    _SCALAR_OP_FIELDS = frozenset({
        'name', 'label', 'json_key', 'json_scope', 'default_json_key',
        'json_scalar_attribute', 'domain_name', 'description', 'domain',
        'value', 'node_type', 'variant_key', 'discriminator_attr',
        'props_node_type', 'parent_type', 'child_type', 'collection_key',
        'data_type', 'group', 'from', 'to',
    })

    # Reference fields: field name -> (target model label, may resolve
    # to a sibling add op). References travel as plain natural-key
    # scalars in op payloads (``"node_type": "sdui_button"``) and are
    # normalized to fixture FK lists at compile time.
    _REF_TARGETS = {
        'node_type': ('nodetype', True),
        'props_node_type': ('nodetype', True),
        'parent_type': ('nodetype', True),
        'child_type': ('nodetype', True),
        'domain': ('domain', True),
        'data_type': ('datatype', False),
    }

    def __init__(self) -> None:
        self.changeset_repository = CatalogChangeSetRepository()
        self.usage_repository = CatalogUsageRepository()
        self.import_service = CatalogImportService()
        self.node_type_repository = NodeTypeRepository()
        self.attribute_def_repository = AttributeDefRepository()
        self.composition_repository = CompositionRepository()
        self.fixture_repository = FixtureRepository()
        self.schema_repository = SchemaRepository()
        self.validation_service = SchemaValidationService()
        self._model_defaults_cache: dict = {}

    # ------------------------------------------------------------------ #
    # Public contract                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def can_transition(from_status: str, to_status: str) -> bool:
        """Whether ``from_status`` may transition to ``to_status``."""
        return to_status in CHANGESET_TRANSITIONS.get(
            from_status, frozenset(),
        )

    def get_changeset(
        self,
        changeset_id: 'str | uuid.UUID',
        for_update: bool = False,
    ) -> CatalogChangeSet:
        """Fetch one changeset by id — the read arm of the loop.

        Args:
            changeset_id: changeset UUID or its string form.
            for_update: lock the row ``FOR UPDATE`` — lifecycle
                methods call this inside their own atomic block so
                concurrent reviewers serialize on the row instead of
                racing the status gate on a stale hydrated copy.

        Returns:
            The ``CatalogChangeSet`` instance, with proposer/reviewer
            FKs select_related per the repository convention.

        Raises:
            ChangesetNotFoundError: no changeset row with that id —
                ``LookupError`` subclass; ``str(exc)`` is
                ``ERR_CHANGESET_NOT_FOUND`` and ``changeset_id``
                echoes the raw addressed value for the 404 envelope.
            ValueError: ``changeset_id`` is not a valid UUID —
                propagated from the repository unwrapped (the HTTP
                layer maps it to 400).
        """
        try:
            return self.changeset_repository.get_by_id(
                changeset_id, for_update=for_update,
            )
        except CatalogChangeSet.DoesNotExist as e:
            raise ChangesetNotFoundError(changeset_id) from e

    @transaction.atomic
    def propose(
        self,
        ops: list,
        rationale: str,
        *,
        proposed_by_user: 'Optional[User]' = None,
        proposed_by_credential: Optional[ProjectAPICredential] = None,
        scope_check: Optional[Callable[[list], bool]] = None,
    ) -> CatalogChangeSet:
        """Create a changeset and validate it synchronously.

        Lifecycle traversed inside this transaction: ``draft →
        proposed → validating → valid|invalid``. The row enters as
        ``draft``, is immediately marked ``proposed`` — the visible
        state of an unreviewed submission — and runs through
        :meth:`validate`, so the returned instance already carries
        its final ``status``, the ``validation_report``, the derived
        ``scopes`` and the ``base_revision``.

        The ``scope_check`` gate runs AFTER validation by design:
        ``scopes`` — the ``json_scope`` perimeter the ops touch — is
        derived server-side inside ``validate()``, so no scope set
        exists to check before it runs. A rejection raises
        ``PermissionError`` inside the atomic block: the transaction
        rolls back and a proposal outside the proposer's perimeter
        never persists.

        Args:
            ops: ordered mutation ops, stored verbatim.
            rationale: proposer-supplied context for the reviewer.
            proposed_by_user: human proposer — XOR with
                ``proposed_by_credential`` (the repository enforces
                exactly one proposer).
            proposed_by_credential: machine proposer — XOR with
                ``proposed_by_user``.
            scope_check: ``Callable[[list], bool]`` predicate the
                HTTP layer injects (its permission class'
                ``covers_scopes`` bound to the request principal) —
                kept as a parameter so this service never imports
                the authentication layer. ``None`` skips the check.

        Returns:
            The persisted ``CatalogChangeSet`` at ``valid`` or
            ``invalid``.

        Raises:
            ValueError: repository contract violations — exactly one
                proposer required, ``ops`` must be a list,
                ``rationale`` a string.
            PermissionError: ``ERR_CHANGESET_SCOPES_NOT_COVERED_MSG`` —
                the principal's allowed scopes do not cover the
                derived perimeter; the whole proposal rolls back.
        """
        changeset = self.changeset_repository.create(
            ops=ops,
            rationale=rationale,
            proposed_by_user=proposed_by_user,
            proposed_by_credential=proposed_by_credential,
            status=CHANGESET_STATUS_DRAFT,
        )
        self.changeset_repository.update_fields(
            changeset, status=CHANGESET_STATUS_PROPOSED,
        )
        self.validate(changeset)
        self._enforce_scope_check(changeset, scope_check)
        return changeset

    @transaction.atomic
    def patch(
        self,
        changeset_id: 'str | uuid.UUID',
        *,
        ops: list,
        rationale: str,
        scope_check: Optional[Callable[[list], bool]] = None,
    ) -> CatalogChangeSet:
        """Replace ops/rationale on a changeset and re-validate.

        The corrective arm of the feedback loop: the proposer reads
        the ``validation_report``, fixes the ops and patches the same
        changeset — the row keeps its identity, proposer and review
        thread while the payload is replaced wholesale (never
        merged), then re-validated (``→ validating → valid|invalid``)
        refreshing ``scopes`` and ``base_revision``.

        Re-proposability is exactly the set of statuses carrying a
        ``→ validating`` edge — ``proposed``, ``invalid``, ``valid``
        and ``stale`` — so the single ``can_transition`` gate covers
        every case: ``draft`` (never submitted), ``approved``,
        ``applied`` and the terminal states raise
        ``ChangesetStateConflictError`` — the 409 signal. The same
        ``scope_check`` gate as :meth:`propose` runs post-validation
        and rolls the correction back on rejection. Re-proposal also
        clears ``review_note``: a verdict written against the old
        payload must not describe the new one.

        Args:
            changeset_id: changeset UUID or its string form.
            ops: replacement op list.
            rationale: replacement rationale for the reviewer.
            scope_check: same contract as :meth:`propose`.

        Returns:
            The same ``CatalogChangeSet`` instance at ``valid`` or
            ``invalid``.

        Raises:
            ChangesetNotFoundError: no changeset row with that id.
            ChangesetStateConflictError: the current status has no
                ``→ validating`` edge.
            ValueError: malformed ``changeset_id`` or repository
                contract violations.
            PermissionError: ``ERR_CHANGESET_SCOPES_NOT_COVERED_MSG`` —
                same post-validation gate as :meth:`propose`.
        """
        changeset = self.get_changeset(changeset_id, for_update=True)
        if not self.can_transition(
            changeset.status, CHANGESET_STATUS_VALIDATING,
        ):
            raise ChangesetStateConflictError(
                changeset.status, CHANGESET_STATUS_VALIDATING,
            )
        self.changeset_repository.update_fields(
            changeset, ops=ops, rationale=rationale, review_note=None,
        )
        self.validate(changeset)
        self._enforce_scope_check(changeset, scope_check)
        return changeset

    @transaction.atomic
    def validate(self, changeset) -> dict:
        """Run the three validation tiers and persist the report.

        Transitions the changeset ``proposed|invalid|valid|stale →
        validating → valid|invalid`` per ``CHANGESET_TRANSITIONS``,
        derives ``scopes`` and ``base_revision``, and stores the
        structured ``validation_report`` consumed by the proposer's
        feedback loop.

        Args:
            changeset: a ``CatalogChangeSet`` instance or its id —
                ids are resolved through the repository.

        Returns:
            The persisted report dict ``{"findings": [...], "impact":
            [...], "tiers_passed": int}``.

        Raises:
            ValueError: ``ERR_CHANGESET_INVALID_TRANSITION`` — the
                current status has no edge to ``validating`` (e.g.
                draft, approved or a terminal state).
        """
        if not isinstance(changeset, CatalogChangeSet):
            changeset = self.changeset_repository.get_by_id(
                changeset, for_update=True,
            )
        if not self.can_transition(
            changeset.status, CHANGESET_STATUS_VALIDATING,
        ):
            raise ValueError(
                ERR_CHANGESET_INVALID_TRANSITION.format(
                    from_status=changeset.status,
                    to_status=CHANGESET_STATUS_VALIDATING,
                )
            )
        self.changeset_repository.update_fields(
            changeset, status=CHANGESET_STATUS_VALIDATING,
        )

        ops = changeset.ops if isinstance(changeset.ops, list) else []
        contexts, findings, pending_adds = self._validate_ops(ops)

        # Scope derivation and the base revision run even when tier-1
        # produced findings — the derived values describe the changeset
        # as proposed and stay useful for permission filtering.
        scopes = self._derive_scopes(contexts, pending_adds)
        base_revision = self._compute_base_revision(scopes)
        self.changeset_repository.update_fields(
            changeset, scopes=scopes, base_revision=base_revision,
        )

        impact: list = []
        tiers_passed = 0
        if not self._has_breaking(findings):
            tiers_passed = 1
            tier2_findings = self._tier2_apply(contexts)
            findings.extend(tier2_findings)
            # Impact analysis runs even when the trial apply failed —
            # the proposer still learns which rows are implemented.
            tier3_findings = self._tier3_impact(contexts, impact)
            findings.extend(tier3_findings)
            # ``tiers_passed`` counts the consecutive tiers the
            # changeset cleared — the tiers are sequential gates in
            # order of cost, so a failed tier-2 caps the count at 1
            # even though tier-3 still ran for its informational value.
            if not self._has_breaking(tier2_findings):
                tiers_passed = 2
                if not self._has_breaking(tier3_findings):
                    tiers_passed = 3

        report = {
            'findings': self._sort_findings(findings),
            'impact': impact,
            'tiers_passed': tiers_passed,
        }
        self.changeset_repository.update_fields(
            changeset,
            validation_report=report,
            status=(
                CHANGESET_STATUS_INVALID
                if self._has_breaking(findings)
                else CHANGESET_STATUS_VALID
            ),
        )
        return report

    @transaction.atomic
    def approve(
        self,
        changeset: 'CatalogChangeSet | str | uuid.UUID',
        user: 'User',
    ) -> CatalogChangeSet:
        """Approve a validated changeset — the reviewer's accept arm.

        The only lifecycle edge into ``approved`` is ``valid →
        approved``, so the single ``can_transition`` gate rejects
        every other current status with
        ``ChangesetStateConflictError`` — the 409 signal, same
        wording :meth:`patch` raises. Approval records the reviewer
        in ``reviewed_by`` and writes a best-effort admin LogEntry
        anchored to the changeset; the ops themselves are not
        re-checked here — the governed apply re-verifies the base
        revision and the payload before materializing anything.

        Args:
            changeset: a ``CatalogChangeSet`` instance or its id —
                ids resolve through :meth:`get_changeset`.
            user: the reviewing user; stored as ``reviewed_by``
                and attributed the audit LogEntry.

        Returns:
            The same ``CatalogChangeSet`` instance at ``approved``.

        Raises:
            ChangesetNotFoundError: no changeset row with that id.
            ChangesetStateConflictError: the current status has no
                ``→ approved`` edge.
            ValueError: malformed id or repository contract
                violations.
        """
        # Re-read under FOR UPDATE: the status gate below must see
        # the committed row, not the caller's hydrated copy — two
        # concurrent reviewers serialize on the lock instead of both
        # passing ``can_transition`` on a stale status.
        changeset = self.get_changeset(
            changeset.pk
            if isinstance(changeset, CatalogChangeSet)
            else changeset,
            for_update=True,
        )
        if not self.can_transition(
            changeset.status, CHANGESET_STATUS_APPROVED,
        ):
            raise ChangesetStateConflictError(
                changeset.status, CHANGESET_STATUS_APPROVED,
            )
        self.changeset_repository.update_fields(
            changeset,
            status=CHANGESET_STATUS_APPROVED,
            reviewed_by=user,
        )
        self.changeset_repository.record_changeset_action(
            user, changeset, 'approved changeset',
        )
        return changeset

    @transaction.atomic
    def reject(
        self,
        changeset: 'CatalogChangeSet | str | uuid.UUID',
        user: 'User',
        reason: str = '',
    ) -> CatalogChangeSet:
        """Reject a reviewable changeset — the reviewer's decline arm.

        ``proposed`` and ``valid`` carry a ``→ rejected`` edge, so
        the ``can_transition`` gate covers both reviewable states
        while ``approved``, ``applied`` and the terminal states
        raise ``ChangesetStateConflictError``. Rejection is
        terminal — the proposer's corrective arm after it is a
        fresh proposal (before verdict, :meth:`patch` covers the
        re-proposable states). The reviewer is recorded in
        ``reviewed_by`` and the stripped reason persists on
        ``review_note`` — the proposer-facing verdict the API
        serializes — while the audit LogEntry carries a truncated
        copy appended.

        Args:
            changeset: a ``CatalogChangeSet`` instance or its id —
                ids resolve through :meth:`get_changeset`.
            user: the reviewing user; stored as ``reviewed_by``
                and attributed the audit LogEntry.
            reason: free-text verdict context; persisted verbatim
                (stripped) on ``review_note`` for the proposer and
                appended to the LogEntry's ``change_message``
                truncated to a scannable length.

        Returns:
            The same ``CatalogChangeSet`` instance at
            ``rejected``.

        Raises:
            ChangesetNotFoundError: no changeset row with that id.
            ChangesetStateConflictError: the current status has no
                ``→ rejected`` edge.
            ValueError: malformed id or repository contract
                violations.
        """
        # Re-read under FOR UPDATE: the status gate below must see
        # the committed row, not the caller's hydrated copy — two
        # concurrent reviewers serialize on the lock instead of both
        # passing ``can_transition`` on a stale status.
        changeset = self.get_changeset(
            changeset.pk
            if isinstance(changeset, CatalogChangeSet)
            else changeset,
            for_update=True,
        )
        if not self.can_transition(
            changeset.status, CHANGESET_STATUS_REJECTED,
        ):
            raise ChangesetStateConflictError(
                changeset.status, CHANGESET_STATUS_REJECTED,
            )
        reason = (reason or '').strip()
        self.changeset_repository.update_fields(
            changeset,
            status=CHANGESET_STATUS_REJECTED,
            reviewed_by=user,
            review_note=reason or None,
        )
        change_message = 'rejected changeset'
        if reason:
            # Bound the audit line — LogEntry rows stay scannable;
            # the full reasoning belongs to the review thread.
            change_message += f' — {reason[:500]}'
        self.changeset_repository.record_changeset_action(
            user, changeset, change_message,
        )
        return changeset

    @transaction.atomic
    def apply(
        self,
        changeset: 'CatalogChangeSet | str | uuid.UUID',
        user: 'User',
    ) -> CatalogChangeSet:
        """Materialize an approved changeset — the governed write.

        ``approved → applied`` is the only edge in, so
        ``ChangesetStateConflictError`` covers every other status.
        The body runs four gates inside this one transaction, then
        revalidates the affected published schemas:

        1. **Stale re-check** — ``base_revision`` is recomputed
           over the changeset's ``scopes``; drift means the
           catalog moved under review, so the changeset is marked
           ``stale`` (the transition is audited) and returned with
           nothing applied — revalidation is its way back.
        2. **Op re-validation** — the syntactic tier re-resolves
           every op against the live catalog; a breaking finding
           aborts with ``ValueError`` carrying
           ``ERR_CHANGESET_APPLY_FAILED_MSG``, rolling the
           transaction back with the row still ``approved``.
        3. **Impact re-check** — ``base_revision`` hashes catalog
           rows only, so usage drift (a target implemented between
           validation and apply) is invisible to gate 1. The
           impact tier re-measures the live usage tables; a
           breaking finding marks the changeset ``stale`` — the
           approval described a different reality — and nothing
           is applied.
        4. **Real apply** — the ops compile to fixture-format
           objects and run through ``import_objects`` with
           ``enforce_perms=False``: the approval IS the
           authorization, so the direct-import gates (permissions,
           usage, attribution) stay off and the writes COMMIT —
           no savepoint probe like the validator's. A falsy
           ``applied`` in the envelope aborts the same way as a
           breaking finding. On success the changeset lands
           ``applied`` with ``applied_at`` stamped and the audit
           LogEntry attributes the applying user.
        5. **Post-apply revalidation** — the affected published
           schemas (captured pre-write: a ``remove_*`` erases its
           target from the usage tables, so the blast radius only
           exists before the apply commits) are re-checked against
           the new catalog and the report persists on the
           changeset's ``post_apply_report``; findings never roll
           back (consequence, not gate), infrastructure exceptions
           do.

        ``reviewed_by`` was set by :meth:`approve` and is not
        touched here.

        Args:
            changeset: a ``CatalogChangeSet`` instance or its id —
                ids resolve through :meth:`get_changeset`.
            user: the applying user — attributed the audit
                LogEntry; the import itself runs ungated.

        Returns:
            The same ``CatalogChangeSet`` instance at ``applied``
            — or at ``stale`` when the base-revision or the
            usage-drift gate fires (nothing was applied in either
            case).

        Raises:
            ChangesetNotFoundError: no changeset row with that id.
            ChangesetStateConflictError: the current status has no
                ``→ applied`` edge.
            ValueError: ``ERR_CHANGESET_APPLY_FAILED_MSG`` — a
                breaking op finding or a failed apply envelope;
                the transaction rolls back and the changeset stays
                ``approved``.
        """
        # Re-read under FOR UPDATE: the status gate below must see
        # the committed row, not the caller's hydrated copy — two
        # concurrent reviewers serialize on the lock instead of both
        # passing ``can_transition`` on a stale status.
        changeset = self.get_changeset(
            changeset.pk
            if isinstance(changeset, CatalogChangeSet)
            else changeset,
            for_update=True,
        )
        if not self.can_transition(
            changeset.status, CHANGESET_STATUS_APPLIED,
        ):
            raise ChangesetStateConflictError(
                changeset.status, CHANGESET_STATUS_APPLIED,
            )

        # Optimistic-concurrency gate: the catalog inside
        # ``scopes`` must still be exactly what validation hashed.
        if (self._compute_base_revision(changeset.scopes)
                != changeset.base_revision):
            self.changeset_repository.update_fields(
                changeset, status=CHANGESET_STATUS_STALE,
            )
            self.changeset_repository.record_changeset_action(
                user, changeset,
                'marked stale — base revision drifted before apply',
            )
            return changeset

        ops = changeset.ops if isinstance(changeset.ops, list) else []
        contexts, findings, _pending_adds = self._validate_ops(ops)
        if self._has_breaking(findings):
            first_breaking = next(
                finding for finding in self._sort_findings(findings)
                if finding.get('severity') == SEVERITY_BREAKING
            )
            raise ValueError(
                ERR_CHANGESET_APPLY_FAILED_MSG.format(
                    detail=first_breaking.get('message'),
                )
            )

        # Usage-drift gate: ``base_revision`` hashes catalog rows
        # only — a target implemented between validation and apply
        # leaves the digest untouched, so the impact tier re-runs
        # against the live usage tables. A destructive op blocked
        # here means the approval was granted for a different
        # catalog reality: the changeset goes ``stale`` — same
        # "world moved" bucket as gate 1 — so the proposer can
        # re-validate through the corrective loop instead of
        # sitting ``approved`` with no exit edge.
        if self._has_breaking(self._tier3_impact(contexts, [])):
            self.changeset_repository.update_fields(
                changeset, status=CHANGESET_STATUS_STALE,
            )
            self.changeset_repository.record_changeset_action(
                user, changeset,
                'marked stale — usage drifted before apply',
            )
            return changeset

        # Pre-apply capture of the blast radius: the schemas
        # implementing the touched rows must be resolved BEFORE the
        # apply writes — a ``remove_*`` op deletes its target (and
        # the implementing NodeAttribute rows through CASCADE), so
        # post-write the usage tables no longer describe who
        # implemented what.
        affected = self._capture_affected_schemas(contexts)

        objects: list = []
        for ctx in contexts:
            if ctx.ok:
                objects.extend(self._compile_op(ctx))
        result = self.import_service.import_objects(
            objects, user=user, enforce_perms=False,
        )
        if not result.get('applied'):
            errors = result.get('errors') or []
            detail = (
                errors[0].get('message')
                if errors
                else _APPLY_ERROR_FALLBACK
            )
            raise ValueError(
                ERR_CHANGESET_APPLY_FAILED_MSG.format(detail=detail)
            )

        self.changeset_repository.update_fields(
            changeset,
            status=CHANGESET_STATUS_APPLIED,
            applied_at=timezone.now(),
        )
        self.changeset_repository.record_changeset_action(
            user, changeset, 'applied changeset to catalog',
        )
        self._revalidate_affected_schemas(changeset, affected, user)
        return changeset

    @transaction.atomic
    def supersede(
        self,
        changeset: 'CatalogChangeSet | str | uuid.UUID',
        user: 'User',
        reason: str = '',
    ) -> CatalogChangeSet:
        """Abandon a changeset — the audited escape hatch.

        ``superseded`` is the generic abandon terminal: every live
        status carries a ``→ superseded`` edge, so the single
        ``can_transition`` gate admits them all while ``applied``,
        ``rejected`` and ``superseded`` itself raise
        ``ChangesetStateConflictError``. Superseding is not a
        verdict on the proposal — that is what :meth:`reject`
        covers — but an operational abandon: the case it exists
        for is an ``approved`` changeset whose apply fails
        deterministically, which otherwise has no legal exit. The
        abandoning reviewer lands on ``reviewed_by``, the reason
        persists on ``review_note`` for the proposer (same
        visibility contract as :meth:`reject`) and the audit
        LogEntry carries a truncated copy.

        Args:
            changeset: a ``CatalogChangeSet`` instance or its id —
                ids resolve through :meth:`get_changeset`.
            user: the abandoning user; stored as ``reviewed_by``
                and attributed the audit LogEntry.
            reason: free-text abandon context; persisted verbatim
                (stripped) on ``review_note`` and appended to the
                LogEntry's ``change_message`` truncated to a
                scannable length.

        Returns:
            The same ``CatalogChangeSet`` instance at
            ``superseded``.

        Raises:
            ChangesetNotFoundError: no changeset row with that id.
            ChangesetStateConflictError: the current status has no
                ``→ superseded`` edge.
            ValueError: malformed id or repository contract
                violations.
        """
        # Re-read under FOR UPDATE: the status gate below must see
        # the committed row, not the caller's hydrated copy — two
        # concurrent reviewers serialize on the lock instead of both
        # passing ``can_transition`` on a stale status.
        changeset = self.get_changeset(
            changeset.pk
            if isinstance(changeset, CatalogChangeSet)
            else changeset,
            for_update=True,
        )
        if not self.can_transition(
            changeset.status, CHANGESET_STATUS_SUPERSEDED,
        ):
            raise ChangesetStateConflictError(
                changeset.status, CHANGESET_STATUS_SUPERSEDED,
            )
        reason = (reason or '').strip()
        self.changeset_repository.update_fields(
            changeset,
            status=CHANGESET_STATUS_SUPERSEDED,
            reviewed_by=user,
            review_note=reason or None,
        )
        change_message = 'superseded changeset'
        if reason:
            # Bound the audit line — LogEntry rows stay scannable;
            # the full reasoning lives on ``review_note``.
            change_message += f' — {reason[:500]}'
        self.changeset_repository.record_changeset_action(
            user, changeset, change_message,
        )
        return changeset

    # ------------------------------------------------------------------ #
    # Scope capability gate (predicate injected by the HTTP layer)       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _enforce_scope_check(
        changeset: CatalogChangeSet,
        scope_check: Optional[Callable[[list], bool]],
    ) -> None:
        """Run the injected scope-capability predicate post-validation.

        ``scopes`` only exists once :meth:`validate` derives it, so
        the gate can never run earlier in the lifecycle. A ``None``
        predicate skips the check; a rejection raises
        ``PermissionError`` inside the caller's ``atomic`` block,
        rolling the whole proposal back — a refused changeset never
        persists. The message carries the derived scopes; the HTTP
        layer re-envelopes it under the stable
        ``ERR_CHANGESET_SCOPES_NOT_COVERED`` code.
        """
        if scope_check is not None and not scope_check(changeset.scopes):
            raise PermissionError(
                ERR_CHANGESET_SCOPES_NOT_COVERED_MSG.format(
                    scopes=changeset.scopes,
                )
            )

    # ------------------------------------------------------------------ #
    # Tier 1 — syntactic                                                  #
    # ------------------------------------------------------------------ #

    def _validate_ops(self, ops: list) -> tuple:
        """Syntactic tier: shape, required fields, NK resolution, dedup.

        Two passes: the first checks structure and registers every
        well-formed op's target natural key (for duplicate-target
        detection and the forward references sibling ops make); the
        second resolves targets and references against the live
        catalog — plus the rows the payload's own add ops declare.
        """
        findings: list = []
        contexts: list = []
        pending_adds: dict = {}
        seen_targets: dict = {}

        for index, op in enumerate(ops):
            ctx = self._check_op_shape(index, op, findings)
            if ctx is None:
                continue
            key = (ctx.label, ctx.nk)
            if key in seen_targets:
                findings.append(self._finding(
                    ctx.index, SEVERITY_BREAKING, ctx.entity,
                    ERR_CHANGESET_OP_DUP_TARGET,
                    f"ops {seen_targets[key]} and {ctx.index} target "
                    f"the same {ctx.label} row {list(ctx.nk)}",
                    remediation=(
                        'merge the two operations or split them across '
                        'changesets — one row can only be targeted once'
                    ),
                ))
                ctx.ok = False
            else:
                seen_targets[key] = ctx.index
            if ctx.kind == 'add':
                pending_adds.setdefault(key, ctx.index)
            contexts.append(ctx)

        for ctx in contexts:
            self._resolve_op(ctx, pending_adds, findings)
        return contexts, findings, pending_adds

    def _check_op_shape(
        self, index: int, op, findings: list,
    ) -> Optional[_OpContext]:
        """Structural checks for one op; returns its context or None."""
        if not isinstance(op, dict):
            findings.append(self._finding(
                index, SEVERITY_BREAKING, None,
                ERR_CHANGESET_OP_MALFORMED,
                'each op must be a JSON object carrying an "op" name',
            ))
            return None
        name = op.get('op')
        if not isinstance(name, str) or not name:
            findings.append(self._finding(
                index, SEVERITY_BREAKING, None,
                ERR_CHANGESET_OP_MALFORMED,
                'op must declare a non-empty "op" operation name',
            ))
            return None
        spec = self._OP_SPECS.get(name)
        if spec is None:
            findings.append(self._finding(
                index, SEVERITY_BREAKING, None,
                ERR_CHANGESET_OP_UNKNOWN,
                f"unknown operation '{name}'",
                remediation=(
                    'valid operations: '
                    + ', '.join(sorted(self._OP_SPECS))
                ),
            ))
            return None

        missing: list = []
        invalid: list = []
        for field_name in spec['required']:
            if field_name == 'set':
                if 'set' not in op or op['set'] is None:
                    missing.append('set')
                elif not isinstance(op['set'], dict) or not op['set']:
                    invalid.append(
                        "'set' must be a non-empty object of field "
                        'overrides'
                    )
                continue
            value = op.get(field_name)
            if value is None or (
                isinstance(value, str) and not value.strip()
            ):
                missing.append(field_name)
            elif not isinstance(value, str):
                invalid.append(f"'{field_name}' must be a string")
        for field_name, value in op.items():
            if (field_name not in self._SCALAR_OP_FIELDS
                    or field_name in spec['required']):
                continue
            if value is not None and not isinstance(value, str):
                invalid.append(
                    f"'{field_name}' must be a string or null",
                )
            elif isinstance(value, str) and not value.strip():
                invalid.append(f"'{field_name}' must not be blank")
        if (name == 'rename_attribute'
                and op.get('from') is not None
                and op.get('from') == op.get('to')):
            invalid.append("'from' and 'to' are identical")
        if missing:
            findings.append(self._finding(
                index, SEVERITY_BREAKING,
                {'model': spec['model'], 'nk': []},
                ERR_CHANGESET_OP_FIELD_MISSING,
                f"'{name}' is missing required field(s): "
                + ', '.join(missing),
                remediation=(
                    f"'{name}' requires: "
                    + ', '.join(spec['required'])
                ),
            ))
        if invalid:
            findings.append(self._finding(
                index, SEVERITY_BREAKING,
                {'model': spec['model'], 'nk': []},
                ERR_CHANGESET_OP_FIELD_INVALID,
                f"'{name}' has invalid field(s): " + '; '.join(invalid),
            ))
        if missing or invalid:
            return None
        return _OpContext(
            index=index, name=name, spec=spec, op=op,
            nk=self._target_nk(name, spec, op),
        )

    def _target_nk(self, name: str, spec: dict, op: dict) -> tuple:
        """Natural key of the op's primary target row.

        Identity positions an op omits address a NULL component — the
        same convention the catalog usage endpoint documents (``pk``
        XOR natural-key params, omitted param = NULL). On add ops an
        *absent* position takes the model default instead
        (``discriminator_attr`` defaults to ``'type'``), matching the
        loaddata semantics the compiled object inherits — an explicit
        ``null`` stays ``null`` (a props/inherited variant declares
        ``discriminator_attr=NULL``).
        """
        label = spec['model']
        if name == 'rename_attribute':
            return (
                op['node_type'], op['from'], op.get('variant_key'),
            )
        nk = tuple(op.get(f) for f in NATURAL_KEY_FIELDS[label])
        if spec['kind'] == 'add':
            defaults = self._model_defaults(label)
            nk = tuple(
                op[field_name]
                if field_name in op
                else defaults.get(field_name)
                for field_name in NATURAL_KEY_FIELDS[label]
            )
        return nk

    def _resolve_op(
        self, ctx: _OpContext, pending_adds: dict, findings: list,
    ) -> None:
        """Second tier-1 pass: target existence + reference resolution."""
        label = ctx.label
        if ctx.kind in ('update', 'remove', 'rename'):
            ctx.resolved_pk = (
                self.fixture_repository.get_pk_by_natural_key(
                    label, list(ctx.nk),
                )
            )
            if ctx.resolved_pk is None:
                findings.append(self._finding(
                    ctx.index, SEVERITY_BREAKING, ctx.entity,
                    ERR_CHANGESET_OP_TARGET_NOT_FOUND,
                    f"{label} {list(ctx.nk)} does not resolve to a "
                    'live catalog row',
                    remediation=(
                        'verify the natural key — mutation ops must '
                        'target an existing row; use the matching '
                        'add_* op to create it'
                    ),
                ))
                ctx.ok = False
                return
            if ctx.kind in ('update', 'rename'):
                ctx.live_fields = (
                    self.fixture_repository.get_fixture_state(
                        label, ctx.resolved_pk,
                    )
                )
                if ctx.live_fields is None:
                    findings.append(self._finding(
                        ctx.index, SEVERITY_BREAKING, ctx.entity,
                        ERR_CHANGESET_OP_TARGET_NOT_FOUND,
                        f"{label} {list(ctx.nk)} does not resolve to "
                        'a live catalog row',
                    ))
                    ctx.ok = False
                    return
        elif ctx.kind == 'add':
            if self.fixture_repository.get_pk_by_natural_key(
                label, list(ctx.nk),
            ) is not None:
                findings.append(self._finding(
                    ctx.index, SEVERITY_BREAKING, ctx.entity,
                    ERR_CHANGESET_OP_TARGET_EXISTS,
                    f"{label} {list(ctx.nk)} already exists",
                    remediation=(
                        'the add ops only create — use the matching '
                        'update_* or remove_* op for an existing row'
                    ),
                ))
                ctx.ok = False

        # Reference fields: natural-key scalar targets must resolve —
        # against the live catalog, or against the rows sibling add
        # ops create inside the same changeset.
        for field_name, value in self._op_reference_values(ctx):
            target_label, allow_pending = self._REF_TARGETS[field_name]
            if self._reference_resolves(
                target_label, value, allow_pending, pending_adds,
            ):
                continue
            findings.append(self._finding(
                ctx.index, SEVERITY_BREAKING, ctx.entity,
                ERR_CHANGESET_OP_TARGET_NOT_FOUND,
                f"'{field_name}' reference '{value}' does not resolve "
                f'to a {target_label} row',
                remediation=(
                    f'create it first with an add_{target_label} '
                    'op or fix the reference'
                ),
            ))
            ctx.ok = False

        # Variant-bound attribute defs: ``variant_key`` must name a
        # NodeTypeVariant of the same node type — the catalog keeps
        # that invariant today (a def scoped to an undeclared variant
        # would never resolve a discriminator).
        for nt_name, variant_key in self._def_variant_refs(ctx):
            if self._variant_resolves(nt_name, variant_key, pending_adds):
                continue
            findings.append(self._finding(
                ctx.index, SEVERITY_BREAKING, ctx.entity,
                ERR_CHANGESET_OP_TARGET_NOT_FOUND,
                f"variant_key '{variant_key}' does not resolve to a "
                f"variant of node type '{nt_name}'",
                remediation=(
                    'declare it first with an add_variant op or fix '
                    'the key'
                ),
            ))
            ctx.ok = False

        # The rename target must be free: materializing the renamed
        # def onto an occupied (node_type, json_key, variant_key)
        # would silently merge two defs into one.
        if ctx.name == 'rename_attribute':
            to_nk = (
                ctx.op['node_type'], ctx.op['to'],
                ctx.op.get('variant_key'),
            )
            if self._label_nk_resolves(
                'attributedef', to_nk, pending_adds,
            ):
                findings.append(self._finding(
                    ctx.index, SEVERITY_BREAKING, ctx.entity,
                    ERR_CHANGESET_OP_TARGET_EXISTS,
                    f"json_key '{ctx.op['to']}' already exists under "
                    f"node type '{ctx.op['node_type']}' — renaming "
                    'onto it would merge two attribute defs',
                    remediation=(
                        'choose a free json_key or remove the '
                        'existing def first'
                    ),
                ))
                ctx.ok = False

    def _op_reference_values(self, ctx: _OpContext) -> list:
        """(field, value) reference pairs the op must resolve.

        Adds resolve their top-level reference fields; updates resolve
        the reference fields carried inside ``set`` (a ``None`` inside
        ``set`` is a legitimate unbind, not a reference). Removes and
        renames need no extra resolution — their identity fields are
        the target itself.
        """
        if ctx.kind == 'add':
            return [
                (field_name, ctx.op[field_name])
                for field_name in self._REF_TARGETS
                if ctx.op.get(field_name) is not None
            ]
        if ctx.kind == 'update':
            set_fields = ctx.op.get('set') or {}
            return [
                (field_name, set_fields[field_name])
                for field_name in self._REF_TARGETS
                if set_fields.get(field_name) is not None
            ]
        return []

    def _def_variant_refs(self, ctx: _OpContext) -> list:
        """(node_type, variant_key) pairs that must resolve to an NTV.

        Fires for ``add_attribute_def`` ops declaring a top-level
        ``variant_key`` and for ``update_attribute_def`` ops moving a
        def under a variant via ``set.variant_key``.
        """
        if ctx.label != 'attributedef':
            return []
        op = ctx.op
        if ctx.kind == 'add':
            variant_key = op.get('variant_key')
            if variant_key is None:
                return []
            node_type = op.get('node_type')
            # A variant-scoped def requires an owning node type.
            return [(node_type, variant_key)] if node_type else [
                (None, variant_key),
            ]
        if ctx.kind == 'update':
            set_fields = op.get('set') or {}
            variant_key = set_fields.get('variant_key')
            if variant_key is None:
                return []
            node_type = set_fields.get('node_type', op.get('node_type'))
            return [(node_type, variant_key)] if node_type else [
                (None, variant_key),
            ]
        return []

    def _reference_resolves(
        self, target_label: str, value: str,
        allow_pending: bool, pending_adds: dict,
    ) -> bool:
        """Whether a scalar NK reference resolves live or in-payload."""
        # ``set`` values are never type-checked upstream — a dict/list
        # would crash the hash probe below; degrade to a miss so the op
        # reports ``target_not_found`` instead of escaping validate().
        if not isinstance(value, str):
            return False
        if allow_pending and (target_label, (value,)) in pending_adds:
            return True
        return (
            self.fixture_repository.get_pk_by_natural_key(
                target_label, [value],
            )
            is not None
        )

    def _label_nk_resolves(
        self, label: str, nk: tuple, pending_adds: dict,
    ) -> bool:
        """Whether a full natural key resolves live or in-payload."""
        return (
            (label, nk) in pending_adds
            or self.fixture_repository.get_pk_by_natural_key(
                label, list(nk),
            ) is not None
        )

    def _variant_resolves(
        self, nt_name, variant_key: str, pending_adds: dict,
    ) -> bool:
        """Whether ``(node_type, variant_key)`` resolves to an NTV."""
        if nt_name is None:
            return False
        for (label, nk) in pending_adds:
            if (label == 'nodetypevariant'
                    and nk[0] == nt_name and nk[1] == variant_key):
                return True
        node_type = self.node_type_repository.get_node_type_by_name(
            nt_name,
        )
        if node_type is None:
            return False
        return (
            self.node_type_repository
            .get_node_type_variant_by_node_type_and_variant_key(
                node_type, variant_key,
            ) is not None
        )

    def _model_defaults(self, label: str) -> dict:
        """Fixture-format field defaults of a fresh model instance."""
        if label not in self._model_defaults_cache:
            self._model_defaults_cache[label] = (
                self.fixture_repository.get_default_fixture_fields(label)
                or {}
            )
        return self._model_defaults_cache[label]

    # ------------------------------------------------------------------ #
    # Scope derivation (server-side — the proposer cannot declare it)    #
    # ------------------------------------------------------------------ #

    def _derive_scopes(
        self, contexts: list, pending_adds: dict,
    ) -> list:
        """Sorted ``json_scope`` values the ops touch.

        Node types contribute their own ``json_scope``; attribute defs
        and variants their owner type's; compositions both endpoint
        types' (when they differ); domains the scopes of every node
        type whose attribute defs bind them — including bindings the
        ops of this changeset introduce.
        """
        scopes: set = set()
        contexts_by_index = {ctx.index: ctx for ctx in contexts}
        nt_scope_cache: dict = {}

        def nt_scope(name):
            """json_scope of a node type name — live or payload-added."""
            if not isinstance(name, str) or not name:
                return None
            if name in nt_scope_cache:
                return nt_scope_cache[name]
            scope = None
            pending_index = pending_adds.get(('nodetype', (name,)))
            if pending_index is not None:
                scope = (
                    contexts_by_index[pending_index].op.get('json_scope')
                )
            else:
                pk = self.fixture_repository.get_pk_by_natural_key(
                    'nodetype', [name],
                )
                if pk is not None:
                    state = self.fixture_repository.get_fixture_state(
                        'nodetype', pk,
                    )
                    if state:
                        scope = state.get('json_scope')
            nt_scope_cache[name] = scope
            return scope

        domain_map = None
        for ctx in contexts:
            op = ctx.op
            if ctx.label == 'nodetype':
                if ctx.kind == 'add':
                    self._add_scope(scopes, op.get('json_scope'))
                else:
                    self._add_scope(scopes, nt_scope(op.get('name')))
                # An update moving the type across scopes touches both —
                # ``set`` presence counts even when the destination is
                # ``''``/``None`` (falsy scopes are real perimeters).
                set_fields = op.get('set')
                if isinstance(set_fields, dict) and 'json_scope' in set_fields:
                    self._add_scope(scopes, set_fields['json_scope'])
            elif ctx.label in ('attributedef', 'nodetypevariant'):
                self._add_scope(scopes, nt_scope(op.get('node_type')))
                # set.node_type re-homes the def/variant: the new owner
                # type's scope is touched too.
                for value in self._set_str_values(op, 'node_type'):
                    self._add_scope(scopes, nt_scope(value))
            elif ctx.label == 'nodetypecomposition':
                for field_name in ('parent_type', 'child_type'):
                    self._add_scope(
                        scopes, nt_scope(op.get(field_name)),
                    )
                    for value in self._set_str_values(op, field_name):
                        self._add_scope(scopes, nt_scope(value))
            elif ctx.label in ('domain', 'domainitem'):
                domain_name = op.get('domain') or op.get('domain_name')
                if domain_map is None:
                    domain_map = self._domain_scope_map(
                        contexts, nt_scope,
                    )
                scopes.update(domain_map.get(domain_name, set()))
        # ``None``/``''`` are real scope values: NULL = scopeless type,
        # '' = empty-string scope. ``catalog_rows_in_scopes`` resolves
        # both, so the digest covers the scopeless perimeter too —
        # without them a changeset touching only NULL-scope rows would
        # hash the same empty constant as an empty changeset.
        return sorted(scopes, key=lambda s: (s is None, s or ''))

    @staticmethod
    def _add_scope(scopes: set, value) -> None:
        if value is None or isinstance(value, str):
            scopes.add(value)

    @staticmethod
    def _set_str_values(op: dict, field_name: str) -> list:
        """``set.<field>`` string overrides declared by an update op."""
        set_fields = op.get('set')
        if isinstance(set_fields, dict):
            value = set_fields.get(field_name)
            if isinstance(value, str) and value:
                return [value]
        return []

    def _domain_scope_map(self, contexts: list, nt_scope) -> dict:
        """domain_name -> json_scope set of the types binding it.

        Built once per validation: every domain-bound attribute def
        contributes its owner type's scope, plus the bindings the
        changeset's own def ops introduce (an ``add_attribute_def``
        under a new domain is the usual companion of ``add_domain``).
        """
        type_ids = self.attribute_def_repository.get_all_node_type_ids()
        defs = (
            self.attribute_def_repository
            .get_attribute_defs_by_node_type_ids(type_ids)
        )
        scope_by_id = {
            row['id']: row.get('json_scope')
            for row in self.node_type_repository.get_node_types_by_ids(
                type_ids,
            )
        }
        domain_map: dict = {}
        for row in defs:
            domain_name = row.get('domain__domain_name')
            scope = scope_by_id.get(row.get('node_type_id'))
            if domain_name:
                domain_map.setdefault(domain_name, set()).add(scope)
        for ctx in contexts:
            if ctx.label != 'attributedef':
                continue
            if ctx.kind == 'add':
                domain_name = ctx.op.get('domain')
            elif ctx.kind == 'update':
                domain_name = (ctx.op.get('set') or {}).get('domain')
            else:
                continue
            node_type = ctx.op.get('node_type')
            if isinstance(domain_name, str) and node_type:
                domain_map.setdefault(domain_name, set()).add(
                    nt_scope(node_type)
                )
        return domain_map

    # ------------------------------------------------------------------ #
    # base_revision — optimistic concurrency                             #
    # ------------------------------------------------------------------ #

    def _compute_base_revision(self, scopes: list) -> str:
        """sha256 over the deterministic serialization of scope rows.

        ``catalog_rows_in_scopes`` returns the six catalog models' rows
        belonging to the touched scopes (node types with
        ``json_scope`` in ``scopes`` plus their defs, variants,
        compositions and bound domains/items); the digest orders rows
        per label canonically so any row change inside the perimeter
        flips the hash — the apply-time re-verification key.
        """
        rows_by_label = (
            self.changeset_repository.catalog_rows_in_scopes(scopes)
            or {}
        )
        canonical = {}
        for label in sorted(rows_by_label):
            rows = [
                self._canonical_row(row) for row in rows_by_label[label]
            ]
            rows.sort(
                key=lambda r: json.dumps(r, sort_keys=True, default=str)
            )
            canonical[label] = rows
        payload = json.dumps(canonical, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    @staticmethod
    def _canonical_row(row):
        """JSON-stable rendering of one scope row for the digest.

        ``catalog_rows_in_scopes`` returns model instances keyed on
        their natural-key FKs via ``select_related`` — the digest reads
        raw column values (``value_from_object`` yields the ``*_id``
        storage for FK positions) so the serialization never depends
        on a related instance's ``str`` or triggers a lazy load.
        """
        if isinstance(row, dict):
            return row
        if hasattr(row, '_meta'):
            return {
                f.attname: f.value_from_object(row)
                for f in row._meta.fields
            }
        return row

    # ------------------------------------------------------------------ #
    # Tier 2 — post-application coherence (trial apply + rollback)      #
    # ------------------------------------------------------------------ #

    def _tier2_apply(self, contexts: list) -> list:
        """Apply the compiled ops inside a savepoint, then check coherence.

        The ops compile to fixture-format objects and run through the
        import service's real apply path — permission gate and audit
        record off — so validation executes exactly what the governed
        apply will run. The enclosing savepoint always rolls back; a
        failure envelope or a post-merge coherence check becomes
        breaking findings, never a committed write.
        """
        compiled: list = []  # (ctx, fixture-format object)
        for ctx in contexts:
            if not ctx.ok:
                continue
            for obj in self._compile_op(ctx):
                compiled.append((ctx, obj))
        if not compiled:
            return []
        findings: list = []
        objects = [obj for _, obj in compiled]
        try:
            with transaction.atomic():
                result = self.import_service.import_objects(
                    objects, user=None, enforce_perms=False,
                )
                if result.get('applied'):
                    findings.extend(
                        self._post_merge_checks(contexts)
                    )
                else:
                    findings.extend(
                        self._apply_error_findings(compiled, result)
                    )
                findings.extend(
                    self._apply_warning_findings(compiled, result)
                )
                raise _TrialApplyRollback()
        except _TrialApplyRollback:
            pass
        return findings

    def _apply_warning_findings(
        self, compiled: list, result: dict,
    ) -> list:
        """Map the apply envelope's non-fatal warnings to findings.

        The import demotes intent it cannot honor (e.g. ``is_required``
        on server-managed attributes) and reports it under the object
        path — the proposer's feedback loop should see the same
        normalization instead of believing the flag took effect.
        """
        findings = []
        for warning in result.get('warnings') or []:
            ctx = None
            match = _OBJECT_PATH_RE.match(
                str(warning.get('path') or ''),
            )
            if match:
                object_index = int(match.group(1))
                if 0 <= object_index < len(compiled):
                    ctx = compiled[object_index][0]
            findings.append(self._finding(
                ctx.index if ctx else None,
                SEVERITY_WARNING,
                ctx.entity if ctx else None,
                warning.get('code') or ERR_CHANGESET_APPLY_FAILED,
                warning.get('message') or 'payload normalized on apply',
            ))
        return findings

    def _apply_error_findings(
        self, compiled: list, result: dict,
    ) -> list:
        """Map an import failure envelope back to op findings."""
        findings = []
        for error in result.get('errors', []):
            ctx = None
            match = _OBJECT_PATH_RE.match(str(error.get('path') or ''))
            if match:
                object_index = int(match.group(1))
                if 0 <= object_index < len(compiled):
                    ctx = compiled[object_index][0]
            findings.append(self._finding(
                ctx.index if ctx else None,
                SEVERITY_BREAKING,
                ctx.entity if ctx else None,
                error.get('code') or ERR_CHANGESET_APPLY_FAILED,
                error.get('message') or _APPLY_ERROR_FALLBACK,
            ))
        return findings

    def _compile_op(self, ctx: _OpContext) -> list:
        """Translate one op into fixture-format objects.

        Adds pass their fields through with natural-key positions
        normalized (FK references serialize as natural-key lists,
        absent positions take the model default) and the
        ``required`` alias mapped to ``is_required``; updates merge
        ``set`` over the live fixture state and pin the target row by
        pk — an NK-position change inside ``set`` is a rename the
        pk-arm applies in place; a ``rename_attribute`` compiles to the
        same pk-armed update (in-place ``json_key`` rewrite preserves
        the row identity ``NodeAttribute`` rows reference); removes
        become natural-key delete objects carrying every NK position.
        """
        label = ctx.label
        model = f'schemas.{label}'
        op = ctx.op
        if ctx.kind == 'add':
            fields = {}
            for key, value in op.items():
                if key == 'op':
                    continue
                if key == 'required':
                    key = 'is_required'
                fields[key] = self._normalize_field(label, key, value)
            if label == 'attributedef' and not fields.get('name'):
                # ``name`` is required by the model; the attribute's
                # json_key is the conventional default when the op
                # declares none.
                fields['name'] = fields.get('json_key')
            defaults = self._model_defaults(label)
            for nk_field in NATURAL_KEY_FIELDS[label]:
                if nk_field not in fields:
                    fields[nk_field] = defaults.get(nk_field)
            return [{'model': model, 'fields': fields}]
        if ctx.kind == 'update':
            fields = dict(ctx.live_fields)
            for key, value in op['set'].items():
                if key == 'required':
                    key = 'is_required'
                fields[key] = self._normalize_field(label, key, value)
            return [{
                'model': model, 'pk': ctx.resolved_pk, 'fields': fields,
            }]
        if ctx.kind == 'rename':
            # In-place ``json_key`` change on the same row. A
            # create-new-NK + delete-old-NK pair would collide with the
            # ``(node_type, name, variant_key)`` unique constraint while
            # both rows coexist mid-apply, and deleting the row would
            # cascade every NodeAttribute implementing the def (they
            # reference it by pk). The pk-armed upsert rewrites the key
            # while preserving row identity — and routes the rename
            # through the import's update-in-place usage gate like any
            # other mutation of an implemented row.
            fields = dict(ctx.live_fields)
            fields['json_key'] = self._normalize_field(
                label, 'json_key', op['to'],
            )
            return [{
                'model': model, 'pk': ctx.resolved_pk, 'fields': fields,
            }]
        return [{
            'model': model,
            'fields': self._identity_fields(ctx),
            'delete': True,
        }]

    def _normalize_field(self, label: str, key: str, value):
        """Normalize one field to fixture serialization.

        Natural-key FK positions carry natural-key lists of strings —
        an op's scalar reference (``"node_type": "sdui_button"``) wraps
        into one, ``None`` stays the no-reference shape and an already-
        list value passes through for the import's own shape check.
        """
        if key in self._fk_positions(label):
            if value is None:
                return None
            if isinstance(value, list):
                return value
            return [value]
        return value

    @staticmethod
    def _fk_positions(label: str) -> frozenset:
        return frozenset(
            field_name for field_name, _ in FK_FIELDS.get(label, [])
        )

    def _identity_fields(self, ctx: _OpContext) -> dict:
        """Fixture-format ``fields`` carrying the op's natural key.

        Every NK position is emitted explicitly — the fixture
        validator requires the complete natural key to identify a row,
        with ``None`` addressing nullable positions.
        """
        fields = {}
        fk_positions = self._fk_positions(ctx.label)
        for field_name, component in zip(
            NATURAL_KEY_FIELDS[ctx.label], ctx.nk,
        ):
            if field_name in fk_positions and component is not None:
                fields[field_name] = [component]
            else:
                fields[field_name] = component
        return fields

    def _post_merge_checks(
        self, contexts: list,
    ) -> list:
        """Coherence checks over the catalog after the trial apply.

        Targeted at the node types the ops touch: variant
        discriminators must keep resolving to exactly one attribute
        def, ``props_node_type`` references must resolve, ``json_key``
        must stay unique per (node type, variant key), compositions
        must keep both endpoints and the composition graph must stay
        acyclic.
        """
        findings: list = []
        nt_names, nt_op_index = self._touched_node_type_names(contexts)
        nt_by_name = {}
        for name in nt_names:
            node_type = (
                self.node_type_repository.get_node_type_by_name(name)
            )
            if node_type is not None:
                nt_by_name[name] = node_type
        if not nt_by_name:
            return findings
        type_ids = [nt.id for nt in nt_by_name.values()]
        name_by_id = {nt.id: name for name, nt in nt_by_name.items()}

        variants = (
            self.node_type_repository
            .get_node_type_variants_by_node_type_ids(type_ids)
        )
        attribute_defs = (
            self.attribute_def_repository
            .get_attribute_defs_by_node_type_ids(type_ids)
        )

        findings.extend(self._check_discriminators(
            variants, attribute_defs, name_by_id, nt_op_index,
            nt_by_name,
        ))
        findings.extend(self._check_props_node_types(
            variants, name_by_id, nt_op_index,
        ))
        findings.extend(self._check_variant_scoped_defs(
            variants, attribute_defs, name_by_id, nt_op_index,
        ))
        findings.extend(self._check_duplicate_json_keys(
            attribute_defs, name_by_id, nt_op_index,
        ))
        findings.extend(self._check_compositions(
            contexts, type_ids, name_by_id, nt_op_index,
        ))
        return findings

    def _touched_node_type_names(self, contexts: list) -> tuple:
        """Node type names the ops touch + first owning op index."""
        names: set = set()
        op_index: dict = {}

        def register(value, index):
            if isinstance(value, str) and value:
                names.add(value)
                op_index.setdefault(value, index)

        for ctx in contexts:
            op = ctx.op
            if ctx.label == 'nodetype':
                register(op.get('name'), ctx.index)
                for value in self._set_str_values(op, 'name'):
                    register(value, ctx.index)
            elif ctx.label in ('attributedef', 'nodetypevariant'):
                register(op.get('node_type'), ctx.index)
                for value in self._set_str_values(op, 'node_type'):
                    register(value, ctx.index)
            elif ctx.label == 'nodetypecomposition':
                for field_name in ('parent_type', 'child_type'):
                    register(op.get(field_name), ctx.index)
                    for value in self._set_str_values(op, field_name):
                        register(value, ctx.index)
        return names, op_index

    def _check_discriminators(
        self, variants: list, attribute_defs: list,
        name_by_id: dict, nt_op_index: dict, nt_by_name: dict,
    ) -> list:
        """Variant discriminators must resolve to exactly one def.

        A node type's ``NodeTypeVariant`` rows must declare a single
        distinct ``discriminator_attr`` — the resolution reads one
        arbitrarily — and that attribute must exist as an
        ``AttributeDef`` of the same type, or variant discrimination
        silently breaks.
        """
        findings = []
        variants_by_type: dict = defaultdict(list)
        for row in variants:
            variants_by_type[row['node_type_id']].append(row)
        def_keys = {
            (row['node_type_id'], row['json_key'])
            for row in attribute_defs
        }
        for node_type_id, rows in variants_by_type.items():
            node_type = nt_by_name.get(name_by_id.get(node_type_id))
            # The discriminator the engine resolves for this type —
            # read from the same NodeTypeVariant rows — plus every
            # declared attribute across the type's variants.
            resolved = (
                self.node_type_repository.get_discriminator_attr(
                    node_type,
                )
                if node_type is not None else None
            )
            disc_attrs = {
                row['discriminator_attr']
                for row in rows
                if row['discriminator_attr']
            }
            if resolved:
                disc_attrs.add(resolved)
            name = name_by_id.get(node_type_id)
            op_index = nt_op_index.get(name)
            entity = {'model': 'nodetype', 'nk': [name]}
            if len(disc_attrs) > 1:
                findings.append(self._finding(
                    op_index, SEVERITY_BREAKING, entity,
                    ERR_CHANGESET_DISCRIMINATOR_AMBIGUOUS,
                    f"node type '{name}' declares multiple "
                    f"discriminator attributes {sorted(disc_attrs)} — "
                    'the variant discriminator resolves one '
                    'arbitrarily',
                    remediation=(
                        'keep a single discriminator_attr across the '
                        "type's variants"
                    ),
                ))
            for attr in disc_attrs:
                if (node_type_id, attr) in def_keys:
                    continue
                findings.append(self._finding(
                    op_index, SEVERITY_BREAKING, entity,
                    ERR_CHANGESET_DISCRIMINATOR_UNRESOLVED,
                    f"node type '{name}': variant discriminator "
                    f"'{attr}' has no attribute def with json_key "
                    f"'{attr}'",
                    remediation=(
                        'add the discriminator attribute def or fix '
                        'discriminator_attr'
                    ),
                ))
        return findings

    def _check_props_node_types(
        self, variants: list, name_by_id: dict, nt_op_index: dict,
    ) -> list:
        """``props_node_type`` references must resolve post-merge."""
        findings = []
        for row in variants:
            props_name = row.get('props_node_type__name')
            if not props_name:
                continue
            if self.node_type_repository.get_node_type_by_name(
                props_name,
            ) is not None:
                continue
            owner = name_by_id.get(row['node_type_id'])
            findings.append(self._finding(
                nt_op_index.get(owner), SEVERITY_BREAKING,
                {
                    'model': 'nodetypevariant',
                    'nk': [owner, row.get('variant_key'),
                           row.get('discriminator_attr')],
                },
                ERR_CHANGESET_PROPS_TYPE_UNRESOLVED,
                f"variant '{owner}:{row.get('variant_key')}': "
                f"props_node_type '{props_name}' does not resolve "
                'to a live node type',
            ))
        return findings

    def _check_variant_scoped_defs(
        self, variants: list, attribute_defs: list,
        name_by_id: dict, nt_op_index: dict,
    ) -> list:
        """Variant-scoped defs must reference a surviving variant.

        A ``remove_variant`` — or an ``update_variant`` rewriting the
        key — leaves ``AttributeDef`` rows whose ``variant_key``
        points at nothing: the def silently stops resolving. Every
        non-null ``variant_key`` on a touched type's defs must name a
        ``NodeTypeVariant`` of the same type post-merge.
        """
        declared = defaultdict(set)
        for row in variants:
            declared[row['node_type_id']].add(row['variant_key'])
        findings = []
        for row in attribute_defs:
            variant_key = row['variant_key']
            if (variant_key is None
                    or variant_key in declared.get(
                        row['node_type_id'], (),
                    )):
                continue
            name = name_by_id.get(row['node_type_id'])
            findings.append(self._finding(
                nt_op_index.get(name), SEVERITY_BREAKING,
                {
                    'model': 'attributedef',
                    'nk': [name, row['json_key'], variant_key],
                },
                ERR_CHANGESET_VARIANT_KEY_UNRESOLVED,
                f"node type '{name}': attribute def "
                f"'{row['json_key']}' is scoped to variant_key "
                f"'{variant_key}' that no variant declares",
                remediation=(
                    'restore the variant or re-scope the def — a '
                    'variant-scoped def only resolves while a '
                    'NodeTypeVariant declares the key'
                ),
            ))
        return findings

    def _check_duplicate_json_keys(
        self, attribute_defs: list, name_by_id: dict,
        nt_op_index: dict,
    ) -> list:
        """``json_key`` uniqueness per (node type, variant key).

        The table constraint treats NULL positions as distinct, so
        base (variant_key NULL) defs can collide semantically — a
        post-merge duplicate is incoherent however it was declared.
        """
        findings = []
        groups = Counter(
            (row['node_type_id'], row['json_key'], row['variant_key'])
            for row in attribute_defs
        )
        for (node_type_id, json_key, variant_key), count in groups.items():
            if count <= 1:
                continue
            name = name_by_id.get(node_type_id)
            findings.append(self._finding(
                nt_op_index.get(name), SEVERITY_BREAKING,
                {
                    'model': 'attributedef',
                    'nk': [name, json_key, variant_key],
                },
                ERR_CHANGESET_DUP_JSON_KEY,
                f"node type '{name}': {count} attribute defs share "
                f"json_key '{json_key}' under variant_key "
                f'{variant_key!r}',
                remediation=(
                    'keep one def per (node_type, json_key, '
                    'variant_key) — merge or remove the duplicates'
                ),
            ))
        return findings

    def _check_compositions(
        self, contexts: list, type_ids: list, name_by_id: dict,
        nt_op_index: dict,
    ) -> list:
        """Orphan + new-cycle checks over the composition graph.

        Compositions touching the affected types must keep both
        endpoint rows. Cycles are flagged only when the changeset
        *closes* one: the catalog legitimately contains recursive
        compositions (e.g. a ``children`` composition from a container
        type onto itself), so coherence is enforced as "no new
        type-level cycle" — a new composition edge ``u -> v`` creates
        one iff ``v`` already reaches ``u`` in the post-merge graph.
        """
        findings = []
        touched = set(type_ids)
        parent_ids = set(
            self.attribute_def_repository.get_parent_node_type_ids(
                type_ids,
            )
        )
        edge_rows = list(
            self.composition_repository
            .get_compositions_with_child_types_by_parent_ids(
                list(touched | parent_ids),
            )
        )
        # Orphans: both endpoints must exist post-merge.
        for comp in edge_rows:
            if (comp.parent_type_id not in touched
                    and comp.child_type_id not in touched):
                continue
            if (self.fixture_repository.pk_exists(
                    'nodetype', str(comp.parent_type_id))
                    and self.fixture_repository.pk_exists(
                        'nodetype', str(comp.child_type_id))):
                continue
            parent_name = (
                name_by_id.get(comp.parent_type_id)
                or str(comp.parent_type_id)
            )
            child_name = (
                name_by_id.get(comp.child_type_id)
                or str(comp.child_type_id)
            )
            findings.append(self._finding(
                nt_op_index.get(parent_name)
                or nt_op_index.get(child_name),
                SEVERITY_BREAKING,
                {
                    'model': 'nodetypecomposition',
                    'nk': [parent_name, child_name,
                           comp.collection_key],
                },
                ERR_CHANGESET_ORPHAN_COMPOSITION,
                f"composition {parent_name} -> {child_name} "
                'references a missing node type',
            ))

        new_edges = self._new_composition_edges(contexts)
        if not new_edges:
            return findings

        # Reachable composition graph (parent -> child) expanded BFS
        # from the touched types — the subgraph any new edge could
        # close a cycle inside.
        adjacency: dict = {}
        seen = set(touched)
        frontier = list(touched)
        while frontier:
            level_comps = list(
                self.composition_repository
                .get_compositions_with_child_types_by_parent_ids(
                    frontier,
                )
            )
            next_frontier = []
            for comp in level_comps:
                adjacency.setdefault(
                    comp.parent_type_id, set(),
                ).add(comp.child_type_id)
                if comp.child_type_id not in seen:
                    seen.add(comp.child_type_id)
                    next_frontier.append(comp.child_type_id)
            frontier = next_frontier

        for ctx, parent_id, child_id in new_edges:
            if parent_id == child_id:
                # A self-edge adds no new type-level reachability —
                # the catalog legitimately carries recursive
                # compositions (a container's ``children`` onto
                # itself), so it is not the cycle this check hunts.
                continue
            path = self._reachable_path(adjacency, child_id, parent_id)
            if path is None:
                continue
            cycle_ids = [parent_id] + path
            cycle_names = self._node_type_names_for_ids(cycle_ids)
            findings.append(self._finding(
                ctx.index, SEVERITY_BREAKING,
                {
                    'model': 'nodetypecomposition',
                    'nk': list(ctx.nk),
                },
                ERR_CHANGESET_COMPOSITION_CYCLE,
                'composition edge closes a cycle: '
                + ' -> '.join(cycle_names),
                remediation=(
                    'a node type cannot contain itself transitively — '
                    'remove the new edge or another edge of the cycle'
                ),
            ))
        return findings

    def _new_composition_edges(self, contexts: list) -> list:
        """Composition edges the ops introduce, as name pairs.

        ``add_composition`` always contributes its edge;
        ``update_composition`` contributes only when ``set`` actually
        moves the row's endpoints (a ``min_children``-only update adds
        no edge). Removes can never close a cycle.
        """
        edges = []
        for ctx in contexts:
            if ctx.label != 'nodetypecomposition' or not ctx.ok:
                continue
            op = ctx.op
            if ctx.kind == 'add':
                edges.append((ctx, op['parent_type'], op['child_type']))
            elif ctx.kind == 'update':
                set_fields = op.get('set') or {}
                parent_name = set_fields.get(
                    'parent_type', op.get('parent_type'),
                )
                child_name = set_fields.get(
                    'child_type', op.get('child_type'),
                )
                if (parent_name, child_name) != (
                    op.get('parent_type'), op.get('child_type')
                ):
                    edges.append((ctx, parent_name, child_name))
        resolved = []
        for ctx, parent_name, child_name in edges:
            parent = self.node_type_repository.get_node_type_by_name(
                parent_name,
            )
            child = self.node_type_repository.get_node_type_by_name(
                child_name,
            )
            if parent is None or child is None:
                continue
            resolved.append((ctx, parent.id, child.id))
        return resolved

    @staticmethod
    def _reachable_path(adjacency: dict, start, target) -> Optional[list]:
        """BFS shortest path ``start`` -> ``target`` or None.

        A new edge ``u -> v`` closes a cycle iff ``v`` reaches ``u``;
        the shortest path never routes through the new edge itself
        (that edge enters ``v``, which the search starts from), so the
        returned path — when any — is a pre-existing route the new
        edge closes.
        """
        if start == target:
            return [start]
        queue = [start]
        parent = {start: None}
        cursor = 0
        while cursor < len(queue):
            node = queue[cursor]
            cursor += 1
            for nxt in adjacency.get(node, ()):
                if nxt in parent:
                    continue
                parent[nxt] = node
                if nxt == target:
                    path = [target]
                    while path[-1] != start:
                        path.append(parent[path[-1]])
                    path.reverse()
                    return path
                queue.append(nxt)
        return None

    def _node_type_names_for_ids(self, type_ids: list) -> list:
        """Ordered node type names for the given ids (post-merge)."""
        names = {
            row['id']: row['name']
            for row in self.node_type_repository.get_node_types_by_ids(
                type_ids,
            )
        }
        return [names.get(type_id, str(type_id)) for type_id in type_ids]

    # ------------------------------------------------------------------ #
    # Tier 3 — impact (reachable set)                                    #
    # ------------------------------------------------------------------ #

    def _tier3_impact(self, contexts: list, impact: list) -> list:
        """Report implementation usage for every touched catalog row.

        Each resolved op target — plus the anchor rows of add ops (the
        node type a new def/variant/composition lands under, the
        domain a new item joins) — is measured through
        ``usage_for`` and appended to ``impact``. The risk policy
        then applies: destructive ops on implemented rows are
        breaking, mutative ops report a warning, additive ops are
        informational.
        """
        findings: list = []
        seen: set = set()

        def emit_usage(label, nk, pk):
            key = (label, tuple(nk))
            if key in seen:
                return None
            seen.add(key)
            usage = self.usage_repository.usage_for(label, pk)
            impact.append({
                'entity': {'model': label, 'nk': list(nk)},
                'implemented': usage['implemented'],
                'usage': usage['usage'],
                'projects': usage['usage'].get('projects') or [],
            })
            return usage

        for ctx in contexts:
            if not ctx.ok:
                continue
            if ctx.kind == 'add':
                anchor_notes = []
                for anchor_label, anchor_nk in self._anchors(ctx):
                    anchor_pk = (
                        self.fixture_repository.get_pk_by_natural_key(
                            anchor_label, list(anchor_nk),
                        )
                    )
                    if anchor_pk is None:
                        continue
                    usage = emit_usage(
                        anchor_label, anchor_nk, anchor_pk,
                    )
                    if usage and usage['implemented']:
                        anchor_notes.append(
                            f'{anchor_label} {list(anchor_nk)} is '
                            'implemented by '
                            + self._usage_summary(usage['usage'])
                        )
                message = (
                    f'additive operation — creates {ctx.label} '
                    f'{list(ctx.nk)}'
                )
                if anchor_notes:
                    message += '; ' + '; '.join(anchor_notes)
                findings.append(self._finding(
                    ctx.index, SEVERITY_INFO, ctx.entity,
                    INFO_CHANGESET_ADDITIVE, message,
                ))
                continue
            usage = emit_usage(ctx.label, ctx.nk, ctx.resolved_pk)
            if usage is None or not usage['implemented']:
                continue
            if ctx.risk == 'destructive':
                findings.append(self._finding(
                    ctx.index, SEVERITY_BREAKING, ctx.entity,
                    ERR_CATALOG_OBJECT_IN_USE,
                    ERR_CATALOG_OBJECT_IN_USE_MSG.format(
                        label=ctx.label,
                        natural_key=list(ctx.nk),
                        usage=self._usage_summary(usage['usage']),
                        action='deletes',
                    ),
                ))
            elif ctx.risk == 'mutative':
                findings.append(self._finding(
                    ctx.index, SEVERITY_WARNING, ctx.entity,
                    ERR_CATALOG_OBJECT_IN_USE,
                    f'{ctx.label} {list(ctx.nk)} is implemented by '
                    'existing schemas ('
                    + self._usage_summary(usage['usage'])
                    + ') — the mutation requires human review of '
                    'its blast radius',
                ))
        return findings

    @staticmethod
    def _anchors(ctx: _OpContext) -> list:
        """Existing catalog rows an add op attaches to.

        The impact of an add is measured on its anchor — the object it
        lands under — since the created row has no usage of its own
        yet.
        """
        op = ctx.op
        if ctx.label == 'attributedef':
            node_type = op.get('node_type')
            return (
                [('nodetype', (node_type,))]
                if isinstance(node_type, str) and node_type
                else []
            )
        if ctx.label == 'nodetypevariant':
            return [('nodetype', (op['node_type'],))]
        if ctx.label == 'nodetypecomposition':
            return [
                ('nodetype', (op['parent_type'],)),
                ('nodetype', (op['child_type'],)),
            ]
        if ctx.label == 'domainitem':
            return [('domain', (op['domain'],))]
        return []

    @staticmethod
    def _usage_summary(usage: dict) -> str:
        """Render a ``usage`` dict as short prose for messages."""
        return CatalogImportService._usage_summary(usage)

    # ------------------------------------------------------------------ #
    # Findings helpers                                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _finding(
        op_index, severity: str, entity, code: str, message: str,
        remediation: Optional[str] = None,
    ) -> dict:
        """Assemble one validation_report finding."""
        finding = {
            'op_index': op_index,
            'severity': severity,
            'entity': entity,
            'code': code,
            'message': message,
        }
        if remediation:
            finding['remediation'] = remediation
        return finding

    @staticmethod
    def _has_breaking(findings: list) -> bool:
        return any(
            finding.get('severity') == SEVERITY_BREAKING
            for finding in findings
        )

    @staticmethod
    def _sort_findings(findings: list) -> list:
        """Order findings by op and severity; drop exact duplicates.

        Sort puts breaking findings of each op first, then dedups on
        (op_index, code, entity) so an issue surfaced twice — e.g. the
        apply usage gate and the destructive-op enforcement naming the
        same implemented row — is reported once, keeping the
        higher-severity instance.
        """
        ordered = sorted(
            findings,
            key=lambda finding: (
                finding.get('op_index') is None,
                finding.get('op_index') or 0,
                _SEVERITY_RANK.get(finding.get('severity'), 3),
                str(finding.get('code')),
            ),
        )
        seen = set()
        deduped = []
        for finding in ordered:
            entity = finding.get('entity') or {}
            key = (
                finding.get('op_index'),
                finding.get('code'),
                entity.get('model'),
                tuple(entity.get('nk') or ()),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(finding)
        return deduped

    # ------------------------------------------------------------------ #
    # Post-apply revalidation                                            #
    # ------------------------------------------------------------------ #

    def _capture_affected_schemas(self, contexts: list) -> dict:
        """Resolve the schemas implementing the rows the ops will touch.

        Runs after the tier-3 gate and BEFORE the apply writes — the
        capture is mandatory pre-apply: a ``remove_*`` op deletes its
        target catalog row (and the implementing NodeAttribute rows
        through CASCADE) once the apply lands, so the blast radius only
        exists in the usage tables pre-write. Mutative and destructive
        ops measure their own resolved target; add ops have no target
        yet — their blast radius is the anchor row they land under (the
        same anchor set the impact tier measures).

        Returns:
            ``{node_id: root_row}`` deduped by root node id — a schema
            implementing three touched rows is captured once.
        """
        affected: dict = {}
        for ctx in contexts:
            if not ctx.ok:
                continue
            if ctx.kind in ('update', 'rename', 'remove'):
                candidates = self.usage_repository.implementing_schemas(
                    ctx.label, ctx.resolved_pk,
                )
            elif ctx.kind == 'add':
                candidates = []
                for anchor_label, anchor_nk in self._anchors(ctx):
                    anchor_pk = (
                        self.fixture_repository.get_pk_by_natural_key(
                            anchor_label, list(anchor_nk),
                        )
                    )
                    if anchor_pk is None:
                        continue
                    candidates.extend(
                        self.usage_repository.implementing_schemas(
                            anchor_label, anchor_pk,
                        )
                    )
            else:
                continue
            for row in candidates:
                affected.setdefault(row['node_id'], row)
        return affected

    def _revalidate_affected_schemas(
        self,
        changeset: CatalogChangeSet,
        affected: dict,
        user: 'User',
    ) -> None:
        """Re-validate affected published schemas against the new catalog.

        Runs at the tail of :meth:`apply`, inside the same transaction —
        the revalidation reads the catalog exactly as the apply left
        it. Draft and archived roots are counted but skipped
        (re-validating them is an opt-in outside this scope). Every
        affected root — published or not — gets its ``SchemaCache``
        row invalidated: a mutated catalog row can leave the
        precomputed resolved JSON stale even when the schema still
        validates.

        Findings never roll the apply back — they are a consequence,
        not a gate, and the schema's ``status`` is never touched. An
        infrastructure exception (database error, repository contract
        violation) propagates and DOES roll the transaction back,
        leaving the changeset ``approved``.
        """
        report = {
            'affected': len(affected),
            'revalidated': 0,
            'skipped_unpublished': 0,
            'failing': [],
            'cache_invalidated': 0,
        }
        for root in affected.values():
            deleted, _detail = (
                self.schema_repository.delete_schema_cache_by_key_version(
                    root['key'], root['version'],
                    project_id=root['project_id'],
                )
            )
            report['cache_invalidated'] += deleted
            if root['status'] != STATUS_PUBLISHED:
                report['skipped_unpublished'] += 1
                continue
            findings = self.validation_service.revalidate_schema(
                uuid.UUID(root['node_id']),
            )
            report['revalidated'] += 1
            if findings:
                report['failing'].append({
                    'schema_key': root['key'],
                    'version': root['version'],
                    'project_id': root['project_id'],
                    'findings': findings,
                })
        self.changeset_repository.update_fields(
            changeset, post_apply_report=report,
        )
        self.changeset_repository.record_changeset_action(
            user, changeset,
            'post-apply revalidation: '
            f"{report['revalidated']} revalidated, "
            f"{len(report['failing'])} failing, "
            f"{report['skipped_unpublished']} unpublished skipped",
        )
