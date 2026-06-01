import logging
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger(__name__)
SENSITIVE_FIELD_NAMES = {
    "authorization",
    "cookie",
    "set-cookie",
    "accesstoken",
    "refreshtoken",
    "password",
    "confirmpassword",
    "playbacktoken",
    "attemptid",
    "verificationcode",
    "resetcode",
    "token",
}


def flatten_details(details: Any | None) -> dict[str, Any]:
    if not isinstance(details, dict):
        return {}
    return details


class AppError(Exception):
    """Controlled application error translated into a REST response."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        """Create a controlled application error."""
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class RangeNotSatisfiableError(Exception):
    """Raised when an HTTP Range header cannot be satisfied."""

    def __init__(self, size_bytes: int) -> None:
        """Create a range error for the requested object size."""
        super().__init__("El rango solicitado no se puede satisfacer.")
        self.size_bytes = size_bytes


def utc_timestamp() -> str:
    """Return the current UTC timestamp formatted for JSON responses."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    """Handle controlled application errors."""
    log_payload = {
        "code": exc.code,
        "message": exc.message,
        "status_code": exc.status_code,
        "details": sanitize_sensitive_data(exc.details),
    }
    if exc.status_code >= 500:
        logger.error("Handled application error: %s", log_payload)
    else:
        logger.warning("Handled application error: %s", log_payload)

    payload: dict[str, Any] = {
        "error": exc.code,
        **flatten_details(sanitize_sensitive_data(exc.details)),
        "message": exc.message,
        "statusCode": exc.status_code,
        "timestamp": utc_timestamp(),
    }
    if exc.details is not None:
        payload["details"] = sanitize_sensitive_data(exc.details)
    return JSONResponse(status_code=exc.status_code, content=payload)


async def range_error_handler(
    _request: Request,
    exc: RangeNotSatisfiableError,
) -> Response:
    """Return an RFC 7233 compliant unsatisfiable range response."""
    return Response(
        status_code=416,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes */{exc.size_bytes}",
        },
    )


async def validation_error_handler(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Handle FastAPI request validation errors."""
    details = sanitize_validation_errors(exc.errors())
    logger.warning("Request validation failed: %s", details)
    return JSONResponse(
        status_code=422,
        content={
            "error": "ValidationError",
            "message": "La solicitud no cumple con el formato esperado.",
            "statusCode": 422,
            "details": details,
            "timestamp": utc_timestamp(),
        },
    )


async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Handle unexpected errors without exposing internal details."""
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": "InternalServerError",
            "message": "Ocurrio un error interno. Intenta de nuevo mas tarde.",
            "statusCode": 500,
            "timestamp": utc_timestamp(),
        },
    )


def sanitize_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert validation errors into JSON-serializable details."""
    sanitized_errors: list[dict[str, Any]] = []
    for error in errors:
        sanitized_error = dict(error)
        sanitized_error.pop("input", None)
        context = sanitized_error.get("ctx")
        if isinstance(context, dict):
            sanitized_context = dict(context)
            context_error = sanitized_context.get("error")
            if context_error is not None:
                sanitized_context["error"] = str(context_error)
            sanitized_error["ctx"] = sanitize_sensitive_data(sanitized_context)
        sanitized_error["msg"] = spanish_validation_message(sanitized_error)
        sanitized_errors.append(sanitized_error)
    return sanitized_errors


def sanitize_sensitive_data(value: Any) -> Any:
    """Recursively redact sensitive fields before logging or serializing errors."""
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).replace("-", "").replace("_", "").lower()
            if normalized_key in SENSITIVE_FIELD_NAMES:
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = sanitize_sensitive_data(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_sensitive_data(item) for item in value)
    if isinstance(value, str):
        return redact_sensitive_string(value)
    return value


def redact_sensitive_string(value: str) -> str:
    """Remove high-risk token shapes from plain strings."""
    return re.sub(r"Bearer\s+[^\s]+", "Bearer [REDACTED]", re.sub(
        r"playbackToken=[^&\s]+",
        "playbackToken=[REDACTED]",
        value,
        flags=re.IGNORECASE,
    ), flags=re.IGNORECASE)


def spanish_validation_message(error: dict[str, Any]) -> str:
    """Return a generic Spanish validation message without leaking internals."""
    error_type = str(error.get("type") or "")
    context = error.get("ctx")
    if error_type == "value_error" and isinstance(context, dict) and context.get("error"):
        return str(context["error"])
    if "missing" in error_type:
        return "El campo es obligatorio."
    if "uuid" in error_type:
        return "El valor debe ser un UUID valido."
    if "greater_than" in error_type:
        return "El valor debe ser mayor al minimo permitido."
    if "less_than" in error_type:
        return "El valor debe ser menor al maximo permitido."
    if "string_too_short" in error_type:
        return "El texto es demasiado corto."
    if "string_too_long" in error_type:
        return "El texto es demasiado largo."
    return "El valor no cumple con el formato esperado."
