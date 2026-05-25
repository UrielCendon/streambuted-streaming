import logging
from dataclasses import dataclass
from typing import Any

import grpc
import httpx

from app.errors import AppError
from app.grpc.generated import catalog_playback_pb2, catalog_playback_pb2_grpc

logger = logging.getLogger(__name__)

PUBLISHED_STATUSES = {"PUBLICADO", "PUBLISHED", "published", "publicado"}
PUBLIC_TRACK_BATCH_SIZE = 100


@dataclass(frozen=True)
class CatalogTrack:
    """Track metadata required to decide playback eligibility."""

    track_id: str
    status: str
    audio_asset_id: str
    raw: dict[str, Any]
    duration_seconds: float | None = None


@dataclass(frozen=True)
class PublicCatalogTrack:
    """Published track metadata exposed to library views."""

    track_id: str
    artist_id: str
    album_id: str | None
    title: str
    genre: str
    cover_asset_id: str | None
    duration_seconds: float | None
    status: str
    artist_name: str
    album_title: str | None
    raw: dict[str, Any]


class CatalogClient:
    """gRPC client for Catalog Service track metadata."""

    def __init__(
        self,
        target: str,
        timeout_seconds: float = 5.0,
        http_base_url: str = "http://catalog-service:8082/api/v1/catalog",
    ) -> None:
        """Create a Catalog client."""
        if not target.strip():
            raise ValueError("CATALOG_GRPC_TARGET must be configured.")
        self._target = target.strip()
        self._timeout_seconds = timeout_seconds
        self._http_base_url = http_base_url.rstrip("/")

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

    async def get_public_tracks_by_ids(
        self,
        track_ids: list[str],
    ) -> list[PublicCatalogTrack]:
        """Fetch public metadata for published tracks, preserving requested order."""
        if not track_ids:
            return []

        tracks: list[PublicCatalogTrack] = []
        for start in range(0, len(track_ids), PUBLIC_TRACK_BATCH_SIZE):
            tracks.extend(
                await self._fetch_public_track_batch(
                    track_ids[start : start + PUBLIC_TRACK_BATCH_SIZE]
                )
            )
        return tracks

    async def _fetch_public_track_batch(
        self,
        track_ids: list[str],
    ) -> list[PublicCatalogTrack]:
        """Fetch one Catalog HTTP batch within the public endpoint limit."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    f"{self._http_base_url}/tracks/batch",
                    json={"trackIds": track_ids},
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Catalog Service rejected public track batch lookup: %s",
                exc.response.status_code,
            )
            raise AppError(
                503,
                "CatalogUnavailable",
                "Catalog Service is unavailable.",
            ) from exc
        except httpx.HTTPError as exc:
            logger.error("Catalog Service public track batch lookup failed: %s", exc)
            raise AppError(
                503,
                "CatalogUnavailable",
                "Catalog Service is unavailable.",
            ) from exc

        payload = response.json()
        batch_tracks = payload.get("tracks", []) if isinstance(payload, dict) else []
        return [map_public_track(track) for track in batch_tracks if isinstance(track, dict)]


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


def map_public_track(document: dict[str, Any]) -> PublicCatalogTrack:
    """Map Catalog HTTP JSON into a typed library metadata object."""
    return PublicCatalogTrack(
        track_id=str(document["trackId"]),
        artist_id=str(document["artistId"]),
        album_id=str(document["albumId"]) if document.get("albumId") else None,
        title=str(document["title"]),
        genre=str(document.get("genre") or ""),
        cover_asset_id=str(document["coverAssetId"]) if document.get("coverAssetId") else None,
        duration_seconds=to_optional_float(document.get("durationSeconds")),
        status=str(document.get("status") or ""),
        artist_name=str(document.get("artistName") or "Artista"),
        album_title=str(document["albumTitle"]) if document.get("albumTitle") else None,
        raw=document,
    )


def to_optional_float(value: object) -> float | None:
    """Convert a possible JSON numeric value to float."""
    if value is None:
        return None
    return float(value)
