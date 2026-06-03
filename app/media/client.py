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
            logger.error("Media dependency is unavailable: %s", exc)
            raise AppError(
                503,
                "MediaUnavailable",
                "Esta funcion no esta disponible en este momento. Intenta de nuevo mas tarde.",
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
        return AppError(404, "PlaylistCoverNotFound", "La portada de la playlist no existe.")
    if error.code() == grpc.StatusCode.PERMISSION_DENIED:
        return AppError(403, "PlaylistCoverForbidden", "La portada de la playlist no es accesible.")
    if error.code() == grpc.StatusCode.UNAUTHENTICATED:
        return AppError(401, "Unauthorized", "Tu sesion expiro. Inicia sesion nuevamente.")
    if error.code() == grpc.StatusCode.INVALID_ARGUMENT:
        return AppError(400, "InvalidPlaylistCover", "El id de la portada de la playlist no es valido.")
    if error.code() in {
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.UNAVAILABLE,
    }:
        if error.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
            return AppError(
                503,
                "MediaUnavailable",
                "La solicitud tardo demasiado y no se pudo completar. Intenta nuevamente.",
            )
        return AppError(
            503,
            "MediaUnavailable",
            "Esta funcion no esta disponible en este momento. Intenta de nuevo mas tarde.",
        )

    return AppError(
        503,
        "MediaUnavailable",
        "No se pudo validar la informacion relacionada con esta accion.",
    )
