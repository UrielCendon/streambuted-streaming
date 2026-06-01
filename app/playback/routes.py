from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.auth.jwt_validator import JwtValidator
from app.auth.models import AuthenticatedUser
from app.config import Settings
from app.errors import AppError
from app.playback.schemas import (
    HealthResponse,
    LatestPlaybackProgressResponse,
    PlaybackProgressCompatRequest,
    PlaybackProgressRequest,
    PlaybackProgressResponse,
    StreamSessionResponse,
)
from app.playback.service import PlaybackService
from app.playback.token_service import PlaybackTokenService

router = APIRouter(prefix="/api/v1/playback", tags=["Playback"])
PLAYBACK_COOKIE_NAME = "streambuted_playback_token"


def get_playback_service(request: Request) -> PlaybackService:
    """Resolve the playback service from application state."""
    return request.app.state.playback_service


def get_settings(request: Request) -> Settings:
    """Resolve the application settings from application state."""
    return request.app.state.settings


def get_jwt_validator(request: Request) -> JwtValidator:
    """Resolve the JWT validator from application state."""
    return request.app.state.jwt_validator


def get_playback_token_service(request: Request) -> PlaybackTokenService:
    """Resolve the playback token service from application state."""
    return request.app.state.playback_token_service


def get_current_user(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    validator: JwtValidator = Depends(get_jwt_validator),
) -> AuthenticatedUser:
    """Resolve the authenticated user from the Authorization header."""
    return validator.validate_authorization_header(authorization)


@router.get("/health", response_model=HealthResponse)
async def public_health() -> HealthResponse:
    """Return public Streaming Service health for gateway checks."""
    return HealthResponse(status="healthy", service="streaming-service")


@router.post(
    "/tracks/{track_id}/stream-session",
    response_model=StreamSessionResponse,
)
async def create_stream_session(
    track_id: str,
    response: Response,
    current_user: AuthenticatedUser = Depends(get_current_user),
    playback_service: PlaybackService = Depends(get_playback_service),
    settings: Settings = Depends(get_settings),
) -> StreamSessionResponse:
    """Create a short-lived stream session for a published track."""
    session = await playback_service.create_stream_session(
        track_id=track_id,
        user_id=current_user.subject,
    )
    max_age = max(
        1,
        int((session.session.expires_at - datetime.now(UTC)).total_seconds()),
    )
    response.set_cookie(
        key=PLAYBACK_COOKIE_NAME,
        value=session.playback_token,
        httponly=True,
        secure=settings.streaming_playback_cookie_secure,
        samesite="strict",
        path=f"/api/v1/playback/tracks/{quote(track_id, safe='')}/stream",
        max_age=max_age,
    )
    return session.session


@router.get("/tracks/{track_id}/stream")
async def stream_track(
    track_id: str,
    playback_token: Annotated[str | None, Query(alias="playbackToken")] = None,
    playback_cookie_token: Annotated[str | None, Cookie(alias=PLAYBACK_COOKIE_NAME)] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    playback_service: PlaybackService = Depends(get_playback_service),
    token_service: PlaybackTokenService = Depends(get_playback_token_service),
    validator: JwtValidator = Depends(get_jwt_validator),
) -> StreamingResponse:
    """Stream audio for a published track using a playback cookie or compatible token transport."""
    effective_playback_token = playback_cookie_token or playback_token
    if effective_playback_token:
        token_service.validate_token(effective_playback_token, track_id)
    elif authorization:
        bearer_token = JwtValidator.extract_bearer_token(authorization)
        try:
            token_service.validate_token(bearer_token, track_id)
        except AppError:
            validator.validate_authorization_header(authorization)
    else:
        raise AppError(
            401,
            "Unauthorized",
            "Debes enviar una sesion de reproduccion valida o el encabezado Authorization.",
        )

    return await playback_service.stream_track(
        track_id=track_id,
        range_header=range_header,
    )


@router.get(
    "/progress/latest",
    response_model=LatestPlaybackProgressResponse,
)
async def get_latest_progress(
    current_user: AuthenticatedUser = Depends(get_current_user),
    playback_service: PlaybackService = Depends(get_playback_service),
) -> LatestPlaybackProgressResponse:
    """Get the latest playback state stored for the authenticated user."""
    return await playback_service.get_latest_progress(current_user.subject)


@router.get(
    "/progress/{track_id}",
    response_model=PlaybackProgressResponse,
)
async def get_progress(
    track_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    playback_service: PlaybackService = Depends(get_playback_service),
) -> PlaybackProgressResponse:
    """Get playback progress for the authenticated user and track."""
    return await playback_service.get_progress(current_user.subject, track_id)


@router.put(
    "/progress/{track_id}",
    response_model=PlaybackProgressResponse,
)
async def update_progress(
    track_id: str,
    request: PlaybackProgressRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    playback_service: PlaybackService = Depends(get_playback_service),
) -> PlaybackProgressResponse:
    """Save playback progress for the authenticated user and track."""
    return await playback_service.update_progress(
        current_user.subject,
        track_id,
        request,
    )


@router.post(
    "/progress",
    response_model=PlaybackProgressResponse,
)
async def update_progress_compat(
    request: PlaybackProgressCompatRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    playback_service: PlaybackService = Depends(get_playback_service),
) -> PlaybackProgressResponse:
    """Compatibility endpoint that saves progress with trackId in the body."""
    return await playback_service.update_progress(
        current_user.subject,
        request.track_id,
        request,
    )
