from datetime import UTC, datetime
import inspect
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from fastapi.responses import StreamingResponse

from app.catalog.client import CatalogClient
from app.events.publisher import PlaybackEventPublisher
from app.playback.ranges import parse_range_header
from app.playback.schemas import (
    LatestPlaybackProgressResponse,
    PlaybackProgressRequest,
    PlaybackProgressResponse,
    StreamSessionResponse,
)
from app.playback.token_service import PlaybackTokenService
from app.progress.repository import MongoPlaybackProgressRepository, PlaybackProgress
from app.storage.minio_audio_storage import MinioAudioStorage


class PlaybackService:
    """Coordinates playback sessions, audio streaming, progress, and events."""

    def __init__(
        self,
        catalog_client: CatalogClient,
        storage: MinioAudioStorage,
        progress_repository: MongoPlaybackProgressRepository,
        playback_token_service: PlaybackTokenService,
        event_publisher: PlaybackEventPublisher,
        valid_playback_seconds: float,
    ) -> None:
        """Create a playback service."""
        self._catalog_client = catalog_client
        self._storage = storage
        self._progress_repository = progress_repository
        self._playback_token_service = playback_token_service
        self._event_publisher = event_publisher
        self._valid_playback_seconds = valid_playback_seconds

    async def create_stream_session(
        self,
        track_id: str,
        user_id: str,
    ) -> StreamSessionResponse:
        """Create an ephemeral playback token for a published track."""
        await self._catalog_client.get_playable_track(track_id)
        playback_token, expires_at = self._playback_token_service.create_token(
            user_id=user_id,
            track_id=track_id,
        )
        stream_url = (
            f"/api/v1/playback/tracks/{quote(track_id, safe='')}/stream"
            f"?playbackToken={quote(playback_token, safe='')}"
        )
        return StreamSessionResponse(
            streamUrl=stream_url,
            expiresAt=expires_at,
            trackId=track_id,
        )

    async def stream_track(
        self,
        track_id: str,
        range_header: str | None,
    ) -> StreamingResponse:
        """Stream a playable track with HTTP Range request support."""
        track = await self._catalog_client.get_playable_track(track_id)
        metadata = self._storage.stat_audio(track.audio_asset_id)
        byte_range = parse_range_header(range_header, metadata.size_bytes)

        if byte_range is None:
            stored_object = self._storage.open_audio(track.audio_asset_id)
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Length": str(metadata.size_bytes),
            }
            return StreamingResponse(
                stored_object.content,
                status_code=200,
                media_type=metadata.content_type,
                headers=headers,
            )

        stored_object = self._storage.open_audio(
            track.audio_asset_id,
            offset=byte_range.start,
            length=byte_range.length,
        )
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Range": byte_range.content_range,
            "Content-Length": str(byte_range.length),
        }
        return StreamingResponse(
            stored_object.content,
            status_code=206,
            media_type=metadata.content_type,
            headers=headers,
        )

    async def get_progress(
        self,
        user_id: str,
        track_id: str,
    ) -> PlaybackProgressResponse:
        """Get current playback progress for a user and track."""
        progress = await self._progress_repository.get_progress(user_id, track_id)
        if progress is None:
            return PlaybackProgressResponse(
                trackId=track_id,
                positionSeconds=0,
                durationSeconds=None,
                updatedAt=None,
            )
        return progress_response(progress)

    async def get_latest_progress(self, user_id: str) -> LatestPlaybackProgressResponse:
        """Get the latest playback progress stored for a user."""
        progress = await self._progress_repository.get_latest_progress(user_id)
        if progress is None:
            return LatestPlaybackProgressResponse()

        return LatestPlaybackProgressResponse(
            trackId=progress.track_id,
            positionSeconds=progress.position_seconds,
            durationSeconds=progress.duration_seconds,
            updatedAt=progress.updated_at,
        )

    async def update_progress(
        self,
        user_id: str,
        track_id: str,
        request: PlaybackProgressRequest,
    ) -> PlaybackProgressResponse:
        """Upsert progress and publish playback counted event once when valid."""
        progress = await self._progress_repository.upsert_progress(
            user_id=user_id,
            track_id=track_id,
            position_seconds=request.position_seconds,
            duration_seconds=request.duration_seconds,
        )

        if request.position_seconds >= self._valid_playback_seconds:
            counted_now = await self._progress_repository.mark_playback_counted(
                user_id,
                track_id,
            )
            if counted_now:
                publish_result = self._event_publisher.publish_track_playback_counted(
                    build_track_playback_counted_event(
                        user_id=user_id,
                        track_id=track_id,
                        position_seconds=request.position_seconds,
                    )
                )
                if inspect.isawaitable(publish_result):
                    await publish_result
                updated_progress = await self._progress_repository.get_progress(
                    user_id,
                    track_id,
                )
                if updated_progress is not None:
                    progress = updated_progress

        return progress_response(progress)


def progress_response(progress: PlaybackProgress) -> PlaybackProgressResponse:
    """Map stored progress to the public response schema."""
    return PlaybackProgressResponse(
        trackId=progress.track_id,
        positionSeconds=progress.position_seconds,
        durationSeconds=progress.duration_seconds,
        updatedAt=progress.updated_at,
    )


def build_track_playback_counted_event(
    user_id: str,
    track_id: str,
    position_seconds: float,
) -> dict[str, Any]:
    """Build a TrackPlaybackCounted event payload."""
    counted_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "eventId": str(uuid4()),
        "eventType": "TrackPlaybackCounted",
        "userId": user_id,
        "trackId": track_id,
        "countedAt": counted_at,
        "positionSeconds": position_seconds,
    }
