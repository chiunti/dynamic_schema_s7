"""Structured service-layer exceptions for the schema pipeline."""

from typing import Any, Optional

from ..constants import ERR_INVALID_SCHEMA


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
