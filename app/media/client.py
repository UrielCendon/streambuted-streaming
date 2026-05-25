import logging
from dataclasses import dataclass

import grpc

from app.errors import AppError
from app.grpc.generated import media_asset_pb2, media_asset_pb2_grpc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaAssetMetadata:
    """Metadata returned by Media Service for an asset."""

    asset_id: str
    asset_type: str
    owner_user_id: str
    content_type: str
    size_bytes: int
    exists: bool


class MediaAssetClient:
    """gRPC client for Media Service asset metadata."""

    def __init__(
        self,
        target: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        """Create a Media Asset client."""
        if not target.strip():
            raise ValueError("MEDIA_GRPC_TARGET must be configured.")
        self._target = target.strip()
        self._timeout_seconds = timeout_seconds

    async def get_asset_metadata(
        self,
        asset_id: str,
        authorization_header: str | None,
    ) -> MediaAssetMetadata:
        """Fetch metadata for an asset using the caller's authorization."""
        try:
            async with grpc.aio.insecure_channel(self._target) as channel:
                stub = media_asset_pb2_grpc.MediaAssetServiceStub(channel)
                metadata = []
                if authorization_header:
                    metadata.append(("authorization", authorization_header))
                response = await stub.GetAssetMetadata(
                    media_asset_pb2.GetAssetMetadataRequest(asset_id=asset_id),
                    timeout=self._timeout_seconds,
                    metadata=metadata,
                )
        except grpc.aio.AioRpcError as exc:
            raise map_media_grpc_error(exc) from exc
        except (grpc.RpcError, OSError) as exc:
            logger.error("Media Service is unavailable: %s", exc)
            raise AppError(
                503,
                "MediaUnavailable",
                "Media Service is unavailable.",
            ) from exc

        return MediaAssetMetadata(
            asset_id=response.asset_id or asset_id,
            asset_type=response.asset_type or "",
            owner_user_id=response.owner_user_id or "",
            content_type=response.content_type or "",
            size_bytes=int(response.size_bytes),
            exists=bool(response.exists),
        )


def map_media_grpc_error(error: grpc.aio.AioRpcError) -> AppError:
    """Map Media gRPC failures to Library API errors."""
    if error.code() == grpc.StatusCode.NOT_FOUND:
        return AppError(404, "PlaylistCoverNotFound", "Playlist cover not found.")
    if error.code() == grpc.StatusCode.PERMISSION_DENIED:
        return AppError(403, "PlaylistCoverForbidden", "Playlist cover is not accessible.")
    if error.code() == grpc.StatusCode.UNAUTHENTICATED:
        return AppError(401, "Unauthorized", "Missing or invalid Authorization header.")
    if error.code() == grpc.StatusCode.INVALID_ARGUMENT:
        return AppError(400, "InvalidPlaylistCover", "Playlist cover id is invalid.")
    if error.code() in {
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.UNAVAILABLE,
    }:
        return AppError(503, "MediaUnavailable", "Media Service is unavailable.")

    return AppError(503, "MediaUnavailable", "Media Service is unavailable.")
