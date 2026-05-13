import logging
from dataclasses import dataclass
from typing import Iterator

from minio import Minio
from minio.error import S3Error

from app.config import Settings
from app.errors import AppError

logger = logging.getLogger(__name__)

OBJECT_KEY_PREFIX = "assets"
DEFAULT_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class AudioObjectMetadata:
    """Metadata required to stream a stored audio object."""

    asset_id: str
    content_type: str
    size_bytes: int


@dataclass(frozen=True)
class AudioObjectStream:
    """Streaming handle for a stored audio object."""

    content: Iterator[bytes]
    content_type: str
    size_bytes: int


def build_object_key(asset_id: str) -> str:
    """Build the MinIO object key for an asset id."""
    return f"{OBJECT_KEY_PREFIX}/{asset_id}"


class MinioAudioStorage:
    """MinIO-backed reader for audio playback streams."""

    def __init__(self, client: Minio, bucket_name: str) -> None:
        """Create a MinIO audio storage adapter."""
        if not bucket_name.strip():
            raise ValueError("MINIO_BUCKET must be configured.")
        self._client = client
        self._bucket_name = bucket_name

    @classmethod
    def from_settings(cls, settings: Settings) -> "MinioAudioStorage":
        """Create MinIO storage from runtime settings."""
        client = Minio(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        return cls(client=client, bucket_name=settings.minio_bucket)

    def stat_audio(self, asset_id: str) -> AudioObjectMetadata:
        """Return object metadata without reading the binary content."""
        try:
            stat = self._client.stat_object(
                bucket_name=self._bucket_name,
                object_name=build_object_key(asset_id),
            )
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                raise AppError(404, "AssetNotFound", "Audio asset not found.") from exc
            logger.error("Failed to stat MinIO object: %s", exc, exc_info=True)
            raise AppError(
                503,
                "StorageUnavailable",
                "Media storage is unavailable.",
            ) from exc

        return AudioObjectMetadata(
            asset_id=asset_id,
            content_type=stat.content_type or "application/octet-stream",
            size_bytes=stat.size or 0,
        )

    def open_audio(
        self,
        asset_id: str,
        offset: int = 0,
        length: int | None = None,
    ) -> AudioObjectStream:
        """Open an audio asset from MinIO using optional byte offsets."""
        metadata = self.stat_audio(asset_id)
        try:
            response = self._client.get_object(
                bucket_name=self._bucket_name,
                object_name=build_object_key(asset_id),
                offset=offset,
                length=length,
            )
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                raise AppError(404, "AssetNotFound", "Audio asset not found.") from exc
            logger.error("Failed to read MinIO object: %s", exc, exc_info=True)
            raise AppError(
                503,
                "StorageUnavailable",
                "Media storage is unavailable.",
            ) from exc

        def iter_content() -> Iterator[bytes]:
            try:
                yield from response.stream(DEFAULT_CHUNK_SIZE)
            finally:
                response.close()
                response.release_conn()

        return AudioObjectStream(
            content=iter_content(),
            content_type=metadata.content_type,
            size_bytes=metadata.size_bytes,
        )
