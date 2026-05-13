import inspect
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from app.auth.jwt_validator import JwtValidator
from app.catalog.client import CatalogClient
from app.config import Settings, get_settings
from app.errors import (
    AppError,
    RangeNotSatisfiableError,
    app_error_handler,
    range_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from app.events.publisher import PlaybackEventPublisher, build_event_publisher
from app.playback.routes import router as playback_router
from app.playback.service import PlaybackService
from app.playback.token_service import PlaybackTokenService
from app.progress.repository import MongoPlaybackProgressRepository
from app.storage.minio_audio_storage import MinioAudioStorage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def create_app(
    settings: Settings | None = None,
    storage: MinioAudioStorage | None = None,
    progress_repository: MongoPlaybackProgressRepository | None = None,
    catalog_client: CatalogClient | None = None,
    event_publisher: PlaybackEventPublisher | None = None,
    jwt_validator: JwtValidator | None = None,
    playback_token_service: PlaybackTokenService | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    app_settings = settings or get_settings()
    app_storage = storage or MinioAudioStorage.from_settings(app_settings)
    app_repository = progress_repository or MongoPlaybackProgressRepository.from_settings(
        app_settings
    )
    app_catalog_client = catalog_client or CatalogClient(
        app_settings.catalog_grpc_target,
        timeout_seconds=app_settings.catalog_grpc_timeout_seconds,
    )
    app_event_publisher = event_publisher or build_event_publisher(app_settings)
    app_jwt_validator = jwt_validator or JwtValidator(
        jwks_url=app_settings.jwt_jwks_url,
        issuer=app_settings.jwt_issuer,
        audience=app_settings.jwt_audience,
    )
    app_playback_token_service = playback_token_service or PlaybackTokenService(
        secret=app_settings.streaming_playback_token_secret,
        ttl_seconds=app_settings.streaming_playback_token_ttl_seconds,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await maybe_await(app_repository.ensure_indexes())
        try:
            yield
        finally:
            close = getattr(app_repository, "close", None)
            if close:
                close()

    app = FastAPI(
        title="StreamButed Streaming Service",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.allowed_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Range",
            "If-Range",
            "X-Requested-With",
        ],
        expose_headers=["Accept-Ranges", "Content-Length", "Content-Range"],
    )

    app.state.playback_token_service = app_playback_token_service
    app.state.jwt_validator = app_jwt_validator
    app.state.playback_service = PlaybackService(
        catalog_client=app_catalog_client,
        storage=app_storage,
        progress_repository=app_repository,
        playback_token_service=app_playback_token_service,
        event_publisher=app_event_publisher,
        valid_playback_seconds=app_settings.streaming_valid_playback_seconds,
    )

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RangeNotSatisfiableError, range_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    @app.get("/health")
    async def internal_health() -> dict[str, str]:
        """Return internal service health."""
        return {"status": "UP", "service": "streaming-service"}

    app.include_router(playback_router)
    return app


async def maybe_await(value: Any) -> Any:
    """Await a value only when it is awaitable."""
    if inspect.isawaitable(value):
        return await value
    return value
