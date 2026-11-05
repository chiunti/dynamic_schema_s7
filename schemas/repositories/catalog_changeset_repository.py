"""Data access layer for CatalogChangeSet entities.

Catalog changesets are the governed mutation path for the catalog: a
proposer (machine credential or human user) submits ops, a reviewer
approves, and the service layer applies them. This repository is the
thin CRUD boundary — it performs defensive input-shape validation and
maps database errors; lifecycle transition enforcement, ops validation
and scope derivation live in the service layer.
"""

import uuid
from typing import Optional

from django.contrib.auth import get_user_model
from django.db import DatabaseError, IntegrityError
from django.db.models import QuerySet
from django.utils import timezone

from ..constants import (
    CHANGESET_STATUS_DRAFT,
    CHANGESET_TRANSITIONS,
    ERR_CHANGESET_FIELDS_NOT_MUTABLE,
    ERR_CHANGESET_MUST_BE_INSTANCE,
    ERR_CHANGESET_OPS_MUST_BE_LIST,
    ERR_CHANGESET_PROPOSER_XOR,
    ERR_CHANGESET_RATIONALE_MUST_BE_STRING,
    ERR_CHANGESET_SCOPES_MUST_BE_LIST_OR_NONE,
    ERR_CHANGESET_STATUS_MUST_BE_VALID,
    ERR_PK_MUST_BE_VALID_UUID,
)
from ..models import CatalogChangeSet, ProjectAPICredential

User = get_user_model()


class CatalogChangeSetRepository:
    """Thin CRUD persistence for catalog changesets."""

    # Columns update_fields() will write. The proposer columns
    # (proposed_by_credential / proposed_by_user) are intentionally
    # absent — the proposer is immutable once the row exists (the
    # DB-level XOR CheckConstraint also covers them).
    _MUTABLE_FIELDS = frozenset(
        {
            "status",
            "scopes",
            "validation_report",
            "base_revision",
            "reviewed_by",
            "applied_at",
            "ops",
            "rationale",
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

    def get_by_id(self, changeset_id: "str | uuid.UUID") -> CatalogChangeSet:
        """Fetch one changeset by primary key.

        Args:
            changeset_id: changeset UUID or its string form.

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
        try:
            return (
                CatalogChangeSet.objects
                .select_related(
                    "proposed_by_credential",
                    "proposed_by_user",
                    "reviewed_by",
                )
                .get(id=canonical_id)
            )
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
