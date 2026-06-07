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
from app.events.outbox import MongoPlaybackEventOutbox, PlaybackEventOutboxProcessor
from app.events.publisher import PlaybackEventPublisher, build_event_publisher
from app.library.repository import MongoLibraryRepository
from app.library.routes import router as library_router
from app.library.service import LibraryService
from app.media.client import MediaAssetClient
from app.openapi import configure_openapi, register_swagger_docs
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
    library_repository: MongoLibraryRepository | None = None,
    catalog_client: CatalogClient | None = None,
    media_asset_client: MediaAssetClient | None = None,
    event_publisher: PlaybackEventPublisher | None = None,
    event_outbox: MongoPlaybackEventOutbox | None = None,
    event_outbox_processor: PlaybackEventOutboxProcessor | None = None,
    jwt_validator: JwtValidator | None = None,
    playback_token_service: PlaybackTokenService | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    app_settings = settings or get_settings()
    app_storage = storage or MinioAudioStorage.from_settings(app_settings)
    app_repository = progress_repository or MongoPlaybackProgressRepository.from_settings(
        app_settings
    )
    app_library_repository = library_repository or MongoLibraryRepository.from_settings(
        app_settings
    )
    app_catalog_client = catalog_client or CatalogClient(
        app_settings.catalog_grpc_target,
        timeout_seconds=app_settings.catalog_grpc_timeout_seconds,
        http_base_url=app_settings.catalog_http_base_url,
    )
    app_media_asset_client = media_asset_client or MediaAssetClient(
        app_settings.media_grpc_target,
        timeout_seconds=app_settings.media_grpc_timeout_seconds,
    )
    should_use_outbox = (
        event_publisher is None
        and bool(app_settings.event_signing_secret.strip())
        and bool(app_settings.rabbitmq_default_pass.strip())
    )
    app_event_outbox = (
        event_outbox
        if should_use_outbox
        else None
    ) or (
        MongoPlaybackEventOutbox.from_settings(app_settings)
        if should_use_outbox
        else None
    )
    app_event_publisher = event_publisher or build_event_publisher(
        app_settings,
        app_event_outbox,
    )
    app_event_outbox_processor = event_outbox_processor
    if app_event_outbox_processor is None and app_event_outbox is not None:
        app_event_outbox_processor = PlaybackEventOutboxProcessor(
            app_event_outbox,
            build_event_publisher(app_settings),
        )
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
        await maybe_await(app_library_repository.ensure_indexes())
        if app_event_outbox is not None:
            await app_event_outbox.ensure_indexes()
        if app_event_outbox_processor is not None:
            app_event_outbox_processor.start()
        try:
            yield
        finally:
            if app_event_outbox_processor is not None:
                await app_event_outbox_processor.stop()
            close = getattr(app_repository, "close", None)
            if close:
                close()
            library_close = getattr(app_library_repository, "close", None)
            if library_close:
                library_close()
            if app_event_outbox is not None:
                app_event_outbox.close()

    app = FastAPI(
        title="StreamButed Streaming Service",
        description=(
            "Playback bajo demanda y biblioteca de usuarios. "
            "Sirve sesiones de streaming, progreso, likes y playlists privadas."
        ),
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/streaming/openapi.json",
        lifespan=lifespan,
    )
    register_swagger_docs(
        app,
        service_name="StreamButed Streaming Service",
        docs_url="/api/v1/streaming/docs",
        openapi_url="/api/v1/streaming/openapi.json",
    )
    configure_openapi(
        app,
        title="StreamButed Streaming Service",
        version="1.0.0",
        description=(
            "Playback bajo demanda y biblioteca de usuarios. "
            "Incluye endpoints /api/v1/playback y /api/v1/library detras del gateway."
        ),
        public_paths={
            "/api/v1/playback/tracks/{track_id}/stream",
        },
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.allowed_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
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
    app.state.settings = app_settings
    app.state.playback_service = PlaybackService(
        catalog_client=app_catalog_client,
        storage=app_storage,
        progress_repository=app_repository,
        playback_token_service=app_playback_token_service,
        event_publisher=app_event_publisher,
        valid_playback_seconds=app_settings.streaming_valid_playback_seconds,
    )
    app.state.library_service = LibraryService(
        repository=app_library_repository,
        catalog_client=app_catalog_client,
        media_asset_client=app_media_asset_client,
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
    app.include_router(library_router)
    return app


async def maybe_await(value: Any) -> Any:
    """Await a value only when it is awaitable."""
    if inspect.isawaitable(value):
        return await value
    return value
