import logging
from dataclasses import dataclass
from typing import Any

import grpc

from app.errors import AppError
from app.grpc.generated import catalog_playback_pb2, catalog_playback_pb2_grpc

logger = logging.getLogger(__name__)

PUBLISHED_STATUSES = {"PUBLICADO", "PUBLISHED", "published", "publicado"}


@dataclass(frozen=True)
class CatalogTrack:
    """Track metadata required to decide playback eligibility."""

    track_id: str
    status: str
    audio_asset_id: str
    raw: dict[str, Any]
    duration_seconds: float | None = None


class CatalogClient:
    """gRPC client for Catalog Service track metadata."""

    def __init__(self, target: str, timeout_seconds: float = 5.0) -> None:
        """Create a Catalog client."""
        if not target.strip():
            raise ValueError("CATALOG_GRPC_TARGET must be configured.")
        self._target = target.strip()
        self._timeout_seconds = timeout_seconds

    async def get_playable_track(self, track_id: str) -> CatalogTrack:
        """Fetch and validate that a track is playable."""
        track = await self._fetch_track(track_id)
        resolved_track_id = track.track_id or track_id
        status = track.status
        audio_asset_id = track.audio_asset_id

        if status not in PUBLISHED_STATUSES:
            raise AppError(
                409,
                "TrackNotPlayable",
                "Track is not published.",
            )
        if not audio_asset_id:
            raise AppError(
                409,
                "TrackNotPlayable",
                "Track does not have an audio asset.",
            )

        return CatalogTrack(
            track_id=resolved_track_id,
            status=status,
            audio_asset_id=audio_asset_id,
            raw=track.raw,
            duration_seconds=track.duration_seconds,
        )

    async def _fetch_track(self, track_id: str) -> CatalogTrack:
        try:
            async with grpc.aio.insecure_channel(self._target) as channel:
                stub = catalog_playback_pb2_grpc.CatalogPlaybackServiceStub(channel)
                response = await stub.GetPlayableTrack(
                    catalog_playback_pb2.GetPlayableTrackRequest(track_id=track_id),
                    timeout=self._timeout_seconds,
                )
        except grpc.aio.AioRpcError as exc:
            raise map_catalog_grpc_error(exc) from exc
        except (grpc.RpcError, OSError) as exc:
            logger.error("Catalog Service is unavailable: %s", exc)
            raise AppError(
                503,
                "CatalogUnavailable",
                "Catalog Service is unavailable.",
            ) from exc

        raw = {
            "trackId": response.track_id,
            "status": response.status,
            "audioAssetId": response.audio_asset_id,
            "durationSeconds": response.duration_seconds,
            "exists": response.exists,
        }
        return CatalogTrack(
            track_id=response.track_id or track_id,
            status=response.status or "",
            audio_asset_id=response.audio_asset_id or "",
            raw=raw,
            duration_seconds=response.duration_seconds if response.duration_seconds > 0 else None,
        )


def map_catalog_grpc_error(error: grpc.aio.AioRpcError) -> AppError:
    """Map Catalog gRPC failures to existing playback API errors."""
    if error.code() == grpc.StatusCode.NOT_FOUND:
        return AppError(404, "TrackNotFound", "Track not found.")
    if error.code() == grpc.StatusCode.INVALID_ARGUMENT:
        return AppError(
            400,
            "CatalogRejectedTrack",
            "Catalog Service rejected the track lookup.",
        )
    if error.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
        return AppError(
            503,
            "CatalogUnavailable",
            "Catalog Service is unavailable.",
        )
    if error.code() == grpc.StatusCode.UNAVAILABLE:
        return AppError(
            503,
            "CatalogUnavailable",
            "Catalog Service is unavailable.",
        )

    return AppError(
        503,
        "CatalogUnavailable",
        "Catalog Service is unavailable.",
    )
