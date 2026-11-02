"""Structured service-layer exceptions for the schema pipeline."""

from typing import Any, Optional

from ..constants import (
    ERR_CATALOG_OBJECT_NOT_FOUND,
    ERR_INVALID_SCHEMA,
    ERR_SCOPE_NOT_FOUND,
)


class SchemaValidationError(ValueError):
    """Structured validation failure raised by the import pipeline.

    Carries the JSON path of the offending node/key plus expected/received
    context for the validate endpoint error envelope. Subclasses
    ``ValueError`` so existing callers that catch ``ValueError`` keep working;
    ``str(exc)`` yields the human-readable message.
    """

    def __init__(
        self,
        message: str,
        code: str = ERR_INVALID_SCHEMA,
        path: Optional[str] = None,
        expected: Optional[Any] = None,
        received: Optional[Any] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.expected = expected
        self.received = received

    def to_dict(self) -> dict:
        """Return this error as an entry of the validate response envelope.

        Always emits the full key set; context fields are ``None`` when they
        do not apply to the failure.
        """
        return {
            "code": self.code,
            "message": str(self),
            "path": self.path,
            "expected": self.expected,
            "received": self.received,
        }


class ScopeNotFoundError(LookupError):
    """Scope lookup failure raised by the resolved catalog service.

    Carries the list of valid root scopes so consumers can discover them
    from the error envelope itself. Subclasses ``LookupError`` so existing
    callers that catch ``LookupError`` keep working; ``str(exc)`` yields
    ``ERR_SCOPE_NOT_FOUND``.
    """

    def __init__(self, available_scopes: list[str]) -> None:
        super().__init__(ERR_SCOPE_NOT_FOUND)
        self.available_scopes = available_scopes


class CatalogObjectNotFoundError(LookupError):
    """Catalog-object lookup failure for the usage pre-check endpoint.

    ``str(exc)`` yields ``ERR_CATALOG_OBJECT_NOT_FOUND``; ``model`` and
    ``identifier`` echo the addressing for the 404 envelope —
    ``identifier`` is ``{"pk": <raw value>}`` or ``{"natural_key":
    {<param>: <value>, ...}}`` depending on the arm the caller used.
    Subclasses ``LookupError`` so callers that already catch
    ``LookupError`` keep working.
    """

    def __init__(self, model: str, identifier: dict) -> None:
        super().__init__(ERR_CATALOG_OBJECT_NOT_FOUND)
        self.model = model
        self.identifier = identifier
