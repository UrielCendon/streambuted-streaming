from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from jwt import InvalidTokenError

from app.errors import AppError

PLAYBACK_TOKEN_PURPOSE = "playback_stream"


class PlaybackTokenService:
    """Creates and validates short-lived playback stream tokens."""

    def __init__(self, secret: str, ttl_seconds: int) -> None:
        """Create a playback token service."""
        if not secret.strip():
            raise ValueError("STREAMING_PLAYBACK_TOKEN_SECRET must be configured.")
        if ttl_seconds <= 0:
            raise ValueError("STREAMING_PLAYBACK_TOKEN_TTL_SECONDS must be positive.")
        self._secret = secret
        self._ttl_seconds = ttl_seconds

    def create_token(self, user_id: str, track_id: str) -> tuple[str, datetime]:
        """Create an HS256 token scoped to a user and a track."""
        issued_at = datetime.now(UTC)
        expires_at = issued_at + timedelta(seconds=self._ttl_seconds)
        payload = {
            "sub": user_id,
            "trackId": track_id,
            "purpose": PLAYBACK_TOKEN_PURPOSE,
            "iat": issued_at,
            "exp": expires_at,
        }
        token = jwt.encode(payload, self._secret, algorithm="HS256")
        return token, expires_at

    def validate_token(self, token: str, track_id: str) -> str:
        """Validate a playback token and return the authenticated user id."""
        try:
            payload = jwt.decode(token, self._secret, algorithms=["HS256"])
        except InvalidTokenError as exc:
            raise AppError(
                401,
                "Unauthorized",
                "Invalid or expired playback token.",
            ) from exc

        if not isinstance(payload, dict):
            raise AppError(401, "Unauthorized", "Invalid playback token payload.")

        subject = payload.get("sub")
        token_track_id = payload.get("trackId")
        purpose = payload.get("purpose")

        if not isinstance(subject, str) or not subject.strip():
            raise AppError(401, "Unauthorized", "Playback token subject is missing.")
        if token_track_id != track_id:
            raise AppError(403, "Forbidden", "Playback token is not valid for this track.")
        if purpose != PLAYBACK_TOKEN_PURPOSE:
            raise AppError(401, "Unauthorized", "Playback token purpose is invalid.")

        return subject.strip()

    @staticmethod
    def read_unverified_payload(token: str) -> dict[str, Any]:
        """Read a token without signature validation for test diagnostics."""
        payload = jwt.decode(token, options={"verify_signature": False})
        return payload if isinstance(payload, dict) else {}
