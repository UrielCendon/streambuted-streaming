import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import grpc
import jwt
from fastapi.testclient import TestClient

from app.auth.models import AuthenticatedUser, UserRole
from app.catalog.client import CatalogClient, CatalogTrack, PublicCatalogTrack
from app.config import Settings
from app.errors import AppError
from app.grpc.generated import catalog_playback_pb2, catalog_playback_pb2_grpc
from app.library.repository import (
    LIKED_SONGS_KEY,
    LibraryPlaylist,
    LibraryPlaylistItem,
    USER_SYSTEM_PLAYLIST_INDEX,
)
from app.main import create_app
from app.playback.ranges import parse_range_header
from app.playback.token_service import PLAYBACK_TOKEN_PURPOSE, PlaybackTokenService
from app.progress.repository import PlaybackProgress, USER_TRACK_INDEX_NAME
from app.storage.minio_audio_storage import AudioObjectMetadata, AudioObjectStream

TRACK_ID = "8ec8d920-a0f4-467d-ad47-53ecf694cbf4"
OTHER_TRACK_ID = "d3d87e12-3fd0-4d3f-af1e-77330831257b"
USER_ID = "37f6c3cb-d848-4678-b545-cd81f5d0f4ea"
ASSET_ID = "d63f4e03-8f01-4f79-8da4-2faf3a9eb20f"
PLAYLIST_COVER_ID = "7ab5ee01-64ac-4ed5-95d8-57f6a329b3b5"
PLAYBACK_SECRET = "test-playback-secret"
AUDIO_BYTES = bytes(range(256))


class FakeJwtValidator:
    def validate_authorization_header(
        self,
        authorization_header: str | None,
    ) -> AuthenticatedUser:
        if authorization_header != "Bearer token":
            raise AppError(401, "Unauthorized", "Missing or invalid Authorization header.")
        return AuthenticatedUser(subject=USER_ID, role=UserRole.LISTENER)


class SuspendedJwtValidator:
    def validate_authorization_header(
        self,
        authorization_header: str | None,
    ) -> AuthenticatedUser:
        raise AppError(
            403,
            "AccountBannedException",
            "La cuenta se encuentra suspendida.",
            {
                "code": "ACCOUNT_BANNED",
                "banType": "TEMPORARY",
                "remainingSeconds": 600,
            },
        )


class FakeCatalogClient:
    def __init__(self) -> None:
        self.tracks: dict[str, CatalogTrack] = {
            TRACK_ID: CatalogTrack(
                track_id=TRACK_ID,
                status="PUBLICADO",
                audio_asset_id=ASSET_ID,
                raw={},
            )
        }
        self.unavailable = False

    async def get_playable_track(self, track_id: str) -> CatalogTrack:
        if self.unavailable:
            raise AppError(503, "CatalogUnavailable", "Catalog Service is unavailable.")
        track = self.tracks.get(track_id)
        if track is None:
            raise AppError(404, "TrackNotFound", "Track not found.")
        if track.status != "PUBLICADO":
            raise AppError(409, "TrackNotPlayable", "Track is not published.")
        if not track.audio_asset_id:
            raise AppError(409, "TrackNotPlayable", "Track does not have an audio asset.")
        return track

    async def get_public_tracks_by_ids(self, track_ids: list[str]) -> list[PublicCatalogTrack]:
        result = []
        for track_id in track_ids:
            track = self.tracks.get(track_id)
            if track and track.status == "PUBLICADO":
                result.append(
                    PublicCatalogTrack(
                        track_id=track_id,
                        artist_id="artist-1",
                        album_id=None,
                        title="Midnight Signals",
                        genre="Electronica",
                        cover_asset_id="2bb0fdd3-1c8f-44f2-9cc5-17fda43a8ddc",
                        duration_seconds=138,
                        status=track.status,
                        artist_name="Signal Artist",
                        album_title=None,
                        raw={},
                    )
                )
        return result


class FakeMediaAssetClient:
    def __init__(self) -> None:
        self.assets: dict[str, dict[str, str | bool]] = {
            PLAYLIST_COVER_ID: {
                "asset_type": "PLAYLIST_COVER",
                "owner_user_id": USER_ID,
                "exists": True,
            }
        }

    async def get_asset_metadata(
        self,
        asset_id: str,
        authorization_header: str | None,
    ):
        if authorization_header != "Bearer token":
            raise AppError(401, "Unauthorized", "Missing or invalid Authorization header.")
        asset = self.assets.get(asset_id)
        if asset is None:
            raise AppError(404, "PlaylistCoverNotFound", "Playlist cover not found.")

        class Metadata:
            asset_type = str(asset["asset_type"])
            owner_user_id = str(asset["owner_user_id"])

        return Metadata()


class FakeStorage:
    def __init__(self) -> None:
        self.assets = {ASSET_ID: AUDIO_BYTES}
        self.opened_lengths: list[int | None] = []

    def stat_audio(self, asset_id: str) -> AudioObjectMetadata:
        content = self.assets.get(asset_id)
        if content is None:
            raise AppError(404, "AssetNotFound", "Audio asset not found.")
        return AudioObjectMetadata(
            asset_id=asset_id,
            content_type="audio/mpeg",
            size_bytes=len(content),
        )

    def open_audio(
        self,
        asset_id: str,
        offset: int = 0,
        length: int | None = None,
    ) -> AudioObjectStream:
        content = self.assets[asset_id]
        self.opened_lengths.append(length)
        selected = content[offset:] if length is None else content[offset : offset + length]
        return AudioObjectStream(
            content=iter([selected]),
            content_type="audio/mpeg",
            size_bytes=len(content),
        )


class FakeProgressRepository:
    def __init__(self) -> None:
        self.documents: dict[tuple[str, str], PlaybackProgress] = {}
        self.index_name: str | None = None

    async def ensure_indexes(self) -> None:
        self.index_name = USER_TRACK_INDEX_NAME

    async def get_progress(
        self,
        user_id: str,
        track_id: str,
    ) -> PlaybackProgress | None:
        return self.documents.get((user_id, track_id))

    async def upsert_progress(
        self,
        user_id: str,
        track_id: str,
        position_seconds: float,
        duration_seconds: float | None,
    ) -> PlaybackProgress:
        existing = self.documents.get((user_id, track_id))
        now = datetime.now(UTC)
        progress = PlaybackProgress(
            user_id=user_id,
            track_id=track_id,
            position_seconds=position_seconds,
            duration_seconds=duration_seconds,
            playback_counted=existing.playback_counted if existing else False,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self.documents[(user_id, track_id)] = progress
        return progress

    async def mark_playback_counted(self, user_id: str, track_id: str) -> bool:
        existing = self.documents[(user_id, track_id)]
        if existing.playback_counted:
            return False
        self.documents[(user_id, track_id)] = PlaybackProgress(
            user_id=existing.user_id,
            track_id=existing.track_id,
            position_seconds=existing.position_seconds,
            duration_seconds=existing.duration_seconds,
            playback_counted=True,
            created_at=existing.created_at,
            updated_at=datetime.now(UTC),
        )
        return True

    def close(self) -> None:
        return None


class FakeLibraryRepository:
    def __init__(self) -> None:
        self.playlists: dict[str, LibraryPlaylist] = {}
        self.items: dict[tuple[str, str], LibraryPlaylistItem] = {}
        self.index_name: str | None = None

    async def ensure_indexes(self) -> None:
        self.index_name = USER_SYSTEM_PLAYLIST_INDEX

    async def get_or_create_liked_playlist(self, user_id: str) -> LibraryPlaylist:
        existing = next(
            (
                playlist
                for playlist in self.playlists.values()
                if playlist.user_id == user_id and playlist.system_key == LIKED_SONGS_KEY
            ),
            None,
        )
        if existing:
            return existing

        now = datetime.now(UTC)
        playlist = LibraryPlaylist(
            playlist_id=str(uuid4()),
            user_id=user_id,
            name="Canciones que te gustan",
            cover_asset_id=None,
            is_system=True,
            system_key=LIKED_SONGS_KEY,
            created_at=now,
            updated_at=now,
        )
        self.playlists[playlist.playlist_id] = playlist
        return playlist

    async def create_playlist(
        self,
        user_id: str,
        name: str,
        cover_asset_id: str | None,
    ) -> LibraryPlaylist:
        now = datetime.now(UTC)
        playlist = LibraryPlaylist(
            playlist_id=str(uuid4()),
            user_id=user_id,
            name=name,
            cover_asset_id=cover_asset_id,
            is_system=False,
            system_key=None,
            created_at=now,
            updated_at=now,
        )
        self.playlists[playlist.playlist_id] = playlist
        return playlist

    async def find_playlist_by_name(
        self,
        user_id: str,
        name: str,
        exclude_playlist_id: str | None = None,
    ) -> LibraryPlaylist | None:
        for playlist in self.playlists.values():
            if playlist.user_id != user_id or playlist.is_system or playlist.name != name:
                continue
            if exclude_playlist_id is not None and playlist.playlist_id == exclude_playlist_id:
                continue
            return playlist
        return None

    async def find_playlist(
        self,
        user_id: str,
        playlist_id: str,
    ) -> LibraryPlaylist | None:
        playlist = self.playlists.get(playlist_id)
        if playlist and playlist.user_id == user_id:
            return playlist
        return None

    async def list_playlists(self, user_id: str) -> list[LibraryPlaylist]:
        return [
            playlist
            for playlist in self.playlists.values()
            if playlist.user_id == user_id and not playlist.is_system
        ]

    async def update_playlist(
        self,
        user_id: str,
        playlist_id: str,
        name: str | None,
        cover_asset_id: str | None,
        cover_asset_id_set: bool = False,
        allow_system: bool = False,
    ) -> LibraryPlaylist | None:
        playlist = await self.find_playlist(user_id, playlist_id)
        if not playlist or (playlist.is_system and not allow_system):
            return None
        updated = LibraryPlaylist(
            playlist_id=playlist.playlist_id,
            user_id=playlist.user_id,
            name=name if name is not None else playlist.name,
            cover_asset_id=cover_asset_id if cover_asset_id_set else playlist.cover_asset_id,
            is_system=playlist.is_system,
            system_key=playlist.system_key,
            created_at=playlist.created_at,
            updated_at=datetime.now(UTC),
        )
        self.playlists[playlist_id] = updated
        return updated

    async def delete_playlist(self, user_id: str, playlist_id: str) -> bool:
        playlist = await self.find_playlist(user_id, playlist_id)
        if not playlist or playlist.is_system:
            return False
        del self.playlists[playlist_id]
        self.items = {
            key: item
            for key, item in self.items.items()
            if item.playlist_id != playlist_id
        }
        return True

    async def add_track(
        self,
        playlist_id: str,
        track_id: str,
    ) -> LibraryPlaylistItem:
        existing = self.items.get((playlist_id, track_id))
        if existing:
            return existing
        item = LibraryPlaylistItem(
            playlist_id=playlist_id,
            track_id=track_id,
            position=len([entry for entry in self.items.values() if entry.playlist_id == playlist_id]),
            added_at=datetime.now(UTC),
        )
        self.items[(playlist_id, track_id)] = item
        return item

    async def remove_track(self, playlist_id: str, track_id: str) -> bool:
        return self.items.pop((playlist_id, track_id), None) is not None

    async def has_track(self, playlist_id: str, track_id: str) -> bool:
        return (playlist_id, track_id) in self.items

    async def list_items(self, playlist_id: str) -> list[LibraryPlaylistItem]:
        return sorted(
            [item for item in self.items.values() if item.playlist_id == playlist_id],
            key=lambda item: item.position,
        )

    async def count_items(self, playlist_id: str) -> int:
        return len([item for item in self.items.values() if item.playlist_id == playlist_id])

    def close(self) -> None:
        return None


class FakePublisher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish_track_playback_counted(self, event: dict[str, Any]) -> bool:
        self.events.append(event)
        return True


def build_settings() -> Settings:
    return Settings(
        minio_secret_key="minio-secret",
        rabbitmq_default_pass="rabbit-secret",
        event_signing_secret="event-secret",
        streaming_playback_token_secret=PLAYBACK_SECRET,
        streaming_playback_token_ttl_seconds=300,
        streaming_valid_playback_seconds=30,
    )


def build_client() -> tuple[TestClient, FakeCatalogClient, FakeStorage, FakeProgressRepository, FakePublisher, FakeLibraryRepository]:
    catalog = FakeCatalogClient()
    storage = FakeStorage()
    repository = FakeProgressRepository()
    library_repository = FakeLibraryRepository()
    media_asset_client = FakeMediaAssetClient()
    publisher = FakePublisher()
    app = create_app(
        settings=build_settings(),
        storage=storage,
        progress_repository=repository,
        library_repository=library_repository,
        catalog_client=catalog,
        media_asset_client=media_asset_client,
        event_publisher=publisher,
        jwt_validator=FakeJwtValidator(),
        playback_token_service=PlaybackTokenService(PLAYBACK_SECRET, 300),
    )
    return TestClient(app), catalog, storage, repository, publisher, library_repository


def build_client_with_validator(
    jwt_validator: FakeJwtValidator | SuspendedJwtValidator,
) -> TestClient:
    app = create_app(
        settings=build_settings(),
        storage=FakeStorage(),
        progress_repository=FakeProgressRepository(),
        library_repository=FakeLibraryRepository(),
        catalog_client=FakeCatalogClient(),
        media_asset_client=FakeMediaAssetClient(),
        event_publisher=FakePublisher(),
        jwt_validator=jwt_validator,
        playback_token_service=PlaybackTokenService(PLAYBACK_SECRET, 300),
    )
    return TestClient(app)


def create_playback_token(track_id: str = TRACK_ID, expires_delta: timedelta | None = None) -> str:
    now = datetime.now(UTC)
    expires_at = now + (expires_delta if expires_delta is not None else timedelta(minutes=5))
    return jwt.encode(
        {
            "sub": USER_ID,
            "trackId": track_id,
            "purpose": PLAYBACK_TOKEN_PURPOSE,
            "iat": now,
            "exp": expires_at,
        },
        PLAYBACK_SECRET,
        algorithm="HS256",
    )


def test_health_returns_healthy() -> None:
    client, *_ = build_client()

    with client:
        response = client.get("/api/v1/playback/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "service": "streaming-service"}


def test_range_parser_accepts_common_ranges() -> None:
    first = parse_range_header("bytes=0-99", 256)
    open_ended = parse_range_header("bytes=100-", 256)
    suffix = parse_range_header("bytes=-100", 256)

    assert first and first.start == 0 and first.end == 99
    assert open_ended and open_ended.start == 100 and open_ended.end == 255
    assert suffix and suffix.start == 156 and suffix.end == 255


def test_stream_returns_partial_content_for_range() -> None:
    client, *_ = build_client()
    token = create_playback_token()

    with client:
        response = client.get(
            f"/api/v1/playback/tracks/{TRACK_ID}/stream",
            params={"playbackToken": token},
            headers={"Range": "bytes=0-99"},
        )

    assert response.status_code == 206
    assert response.headers["Accept-Ranges"] == "bytes"
    assert response.headers["Content-Range"] == "bytes 0-99/256"
    assert response.headers["Content-Length"] == "100"
    assert response.content == AUDIO_BYTES[:100]


def test_stream_returns_416_for_invalid_range() -> None:
    client, *_ = build_client()
    token = create_playback_token()

    with client:
        response = client.get(
            f"/api/v1/playback/tracks/{TRACK_ID}/stream",
            params={"playbackToken": token},
            headers={"Range": "bytes=999-1000"},
        )

    assert response.status_code == 416
    assert response.headers["Content-Range"] == "bytes */256"


def test_stream_returns_200_without_loading_all_bytes_upfront() -> None:
    client, _catalog, storage, *_ = build_client()
    token = create_playback_token()

    with client:
        response = client.get(
            f"/api/v1/playback/tracks/{TRACK_ID}/stream",
            params={"playbackToken": token},
        )

    assert response.status_code == 200
    assert response.headers["Accept-Ranges"] == "bytes"
    assert response.headers["Content-Length"] == "256"
    assert storage.opened_lengths == [None]


def test_stream_session_returns_scoped_playback_token() -> None:
    client, *_ = build_client()

    with client:
        response = client.post(
            f"/api/v1/playback/tracks/{TRACK_ID}/stream-session",
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 200
    body = response.json()
    query = parse_qs(urlparse(body["streamUrl"]).query)
    token = query["playbackToken"][0]
    payload = jwt.decode(token, PLAYBACK_SECRET, algorithms=["HS256"])
    assert payload["sub"] == USER_ID
    assert payload["trackId"] == TRACK_ID
    assert payload["purpose"] == PLAYBACK_TOKEN_PURPOSE


def test_stream_session_rejects_suspended_account() -> None:
    client = build_client_with_validator(SuspendedJwtValidator())

    with client:
        response = client.post(
            f"/api/v1/playback/tracks/{TRACK_ID}/stream-session",
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "ACCOUNT_BANNED"


def test_stream_rejects_expired_playback_token() -> None:
    client, *_ = build_client()
    token = create_playback_token(expires_delta=timedelta(seconds=-1))

    with client:
        response = client.get(
            f"/api/v1/playback/tracks/{TRACK_ID}/stream",
            params={"playbackToken": token},
        )

    assert response.status_code == 401


def test_stream_rejects_token_for_another_track() -> None:
    client, *_ = build_client()
    token = create_playback_token(track_id=OTHER_TRACK_ID)

    with client:
        response = client.get(
            f"/api/v1/playback/tracks/{TRACK_ID}/stream",
            params={"playbackToken": token},
        )

    assert response.status_code == 403


def test_stream_rejects_missing_token_and_authorization() -> None:
    client, *_ = build_client()

    with client:
        response = client.get(f"/api/v1/playback/tracks/{TRACK_ID}/stream")

    assert response.status_code == 401


def test_get_progress_without_document_returns_initial_state() -> None:
    client, *_ = build_client()

    with client:
        response = client.get(
            f"/api/v1/playback/progress/{TRACK_ID}",
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 200
    assert response.json()["positionSeconds"] == 0
    assert response.json()["durationSeconds"] is None
    assert response.json()["updatedAt"] is None


def test_put_progress_creates_and_updates_document() -> None:
    client, *_ = build_client()

    with client:
        created = client.put(
            f"/api/v1/playback/progress/{TRACK_ID}",
            headers={"Authorization": "Bearer token"},
            json={"positionSeconds": 12.5, "durationSeconds": 180.0, "isPlaying": False},
        )
        updated = client.put(
            f"/api/v1/playback/progress/{TRACK_ID}",
            headers={"Authorization": "Bearer token"},
            json={"positionSeconds": 22.5, "durationSeconds": 180.0, "isPlaying": True},
        )

    assert created.status_code == 200
    assert updated.status_code == 200
    assert updated.json()["positionSeconds"] == 22.5


def test_put_progress_validates_negative_position() -> None:
    client, *_ = build_client()

    with client:
        response = client.put(
            f"/api/v1/playback/progress/{TRACK_ID}",
            headers={"Authorization": "Bearer token"},
            json={"positionSeconds": -1, "durationSeconds": 180.0},
        )

    assert response.status_code == 422


def test_put_progress_validates_duration_not_less_than_position() -> None:
    client, *_ = build_client()

    with client:
        response = client.put(
            f"/api/v1/playback/progress/{TRACK_ID}",
            headers={"Authorization": "Bearer token"},
            json={"positionSeconds": 60, "durationSeconds": 10},
        )

    assert response.status_code == 422


def test_progress_crossing_threshold_publishes_event_once() -> None:
    client, _catalog, _storage, _repository, publisher, _library_repository = build_client()

    with client:
        first = client.put(
            f"/api/v1/playback/progress/{TRACK_ID}",
            headers={"Authorization": "Bearer token"},
            json={"positionSeconds": 31, "durationSeconds": 180},
        )
        second = client.put(
            f"/api/v1/playback/progress/{TRACK_ID}",
            headers={"Authorization": "Bearer token"},
            json={"positionSeconds": 45, "durationSeconds": 180},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(publisher.events) == 1
    assert publisher.events[0]["eventType"] == "TrackPlaybackCounted"


def test_catalog_unavailable_returns_503() -> None:
    client, catalog, *_ = build_client()
    catalog.unavailable = True

    with client:
        response = client.post(
            f"/api/v1/playback/tracks/{TRACK_ID}/stream-session",
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 503


def test_missing_track_returns_404() -> None:
    client, *_ = build_client()

    with client:
        response = client.post(
            f"/api/v1/playback/tracks/{OTHER_TRACK_ID}/stream-session",
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 404


def test_like_track_creates_liked_playlist_idempotently() -> None:
    client, *_ = build_client()

    with client:
        first = client.put(
            f"/api/v1/library/tracks/{TRACK_ID}/like",
            headers={"Authorization": "Bearer token"},
        )
        second = client.put(
            f"/api/v1/library/tracks/{TRACK_ID}/like",
            headers={"Authorization": "Bearer token"},
        )
        liked_songs = client.get(
            "/api/v1/library/liked-songs",
            headers={"Authorization": "Bearer token"},
        )

    assert first.status_code == 200
    assert first.json()["isLiked"] is True
    assert second.status_code == 200
    assert liked_songs.status_code == 200
    body = liked_songs.json()
    assert body["trackCount"] == 1
    assert len(body["tracks"]) == 1
    assert body["tracks"][0]["trackId"] == TRACK_ID
    assert body["tracks"][0]["artistName"] == "Signal Artist"


def test_unlike_track_is_idempotent() -> None:
    client, *_ = build_client()

    with client:
        first = client.delete(
            f"/api/v1/library/tracks/{TRACK_ID}/like",
            headers={"Authorization": "Bearer token"},
        )
        status_response = client.get(
            f"/api/v1/library/tracks/{TRACK_ID}/like-status",
            headers={"Authorization": "Bearer token"},
        )

    assert first.status_code == 200
    assert first.json()["isLiked"] is False
    assert status_response.status_code == 200
    assert status_response.json()["isLiked"] is False


def test_like_rejects_unpublished_track() -> None:
    client, catalog, *_ = build_client()
    catalog.tracks[TRACK_ID] = CatalogTrack(
        track_id=TRACK_ID,
        status="RETIRADO",
        audio_asset_id=ASSET_ID,
        raw={},
    )

    with client:
        response = client.put(
            f"/api/v1/library/tracks/{TRACK_ID}/like",
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 409


def test_create_playlist_and_add_track() -> None:
    client, *_ = build_client()

    with client:
        created = client.post(
            "/api/v1/library/playlists",
            headers={"Authorization": "Bearer token"},
            json={"name": "Para manejar", "coverAssetId": PLAYLIST_COVER_ID},
        )
        playlist_id = created.json()["playlistId"]
        updated = client.post(
            f"/api/v1/library/playlists/{playlist_id}/tracks",
            headers={"Authorization": "Bearer token"},
            json={"trackId": TRACK_ID},
        )

    assert created.status_code == 201
    assert created.json()["coverAssetId"] == PLAYLIST_COVER_ID
    assert created.json()["trackCount"] == 0
    assert updated.status_code == 200
    assert updated.json()["trackCount"] == 1
    assert updated.json()["tracks"][0]["title"] == "Midnight Signals"


def test_create_playlist_rejects_name_longer_than_twenty_characters() -> None:
    client, *_ = build_client()

    with client:
        response = client.post(
            "/api/v1/library/playlists",
            headers={"Authorization": "Bearer token"},
            json={"name": "Playlist con nombre demasiado largo"},
        )

    assert response.status_code == 422


def test_create_playlist_rejects_duplicate_name_with_exact_case() -> None:
    client, *_ = build_client()

    with client:
        first = client.post(
            "/api/v1/library/playlists",
            headers={"Authorization": "Bearer token"},
            json={"name": "uriel"},
        )
        duplicate = client.post(
            "/api/v1/library/playlists",
            headers={"Authorization": "Bearer token"},
            json={"name": "uriel"},
        )
        distinct_case = client.post(
            "/api/v1/library/playlists",
            headers={"Authorization": "Bearer token"},
            json={"name": "Uriel"},
        )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["error"] == "PlaylistNameAlreadyExists"
    assert distinct_case.status_code == 201


def test_create_playlist_rejects_invalid_cover_type() -> None:
    client, _catalog, _storage, _repository, _publisher, _library_repository = build_client()
    media_asset_client = client.app.state.library_service._media_asset_client
    media_asset_client.assets[PLAYLIST_COVER_ID] = {
        "asset_type": "TRACK_COVER",
        "owner_user_id": USER_ID,
        "exists": True,
    }

    with client:
        response = client.post(
            "/api/v1/library/playlists",
            headers={"Authorization": "Bearer token"},
            json={"name": "Viaje", "coverAssetId": PLAYLIST_COVER_ID},
        )

    assert response.status_code == 422
    assert response.json()["error"] == "InvalidPlaylistCoverType"


def test_add_track_to_playlist_rejects_duplicate_track() -> None:
    client, *_ = build_client()

    with client:
        created = client.post(
            "/api/v1/library/playlists",
            headers={"Authorization": "Bearer token"},
            json={"name": "Gym"},
        )
        playlist_id = created.json()["playlistId"]
        first_add = client.post(
            f"/api/v1/library/playlists/{playlist_id}/tracks",
            headers={"Authorization": "Bearer token"},
            json={"trackId": TRACK_ID},
        )
        duplicate_add = client.post(
            f"/api/v1/library/playlists/{playlist_id}/tracks",
            headers={"Authorization": "Bearer token"},
            json={"trackId": TRACK_ID},
        )

    assert first_add.status_code == 200
    assert duplicate_add.status_code == 409
    assert duplicate_add.json()["error"] == "TrackAlreadyInPlaylist"


def test_liked_songs_cover_can_be_updated_without_renaming() -> None:
    client, *_ = build_client()

    with client:
        liked_songs = client.get(
            "/api/v1/library/liked-songs",
            headers={"Authorization": "Bearer token"},
        )
        playlist_id = liked_songs.json()["playlistId"]
        updated = client.patch(
            f"/api/v1/library/playlists/{playlist_id}",
            headers={"Authorization": "Bearer token"},
            json={"coverAssetId": PLAYLIST_COVER_ID},
        )
        renamed = client.patch(
            f"/api/v1/library/playlists/{playlist_id}",
            headers={"Authorization": "Bearer token"},
            json={"name": "Otro nombre"},
        )

    assert updated.status_code == 200
    assert updated.json()["coverAssetId"] == PLAYLIST_COVER_ID
    assert renamed.status_code == 403


def test_playlist_lookup_does_not_leak_other_users_playlists() -> None:
    client, _catalog, _storage, _repository, _publisher, library_repository = build_client()
    other_playlist = asyncio.run(
        library_repository.create_playlist(
            user_id=OTHER_TRACK_ID,
            name="Privada",
            cover_asset_id=None,
        )
    )

    with client:
        response = client.get(
            f"/api/v1/library/playlists/{other_playlist.playlist_id}",
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 404


def test_unpublished_track_is_not_playable() -> None:
    client, catalog, *_ = build_client()
    catalog.tracks[TRACK_ID] = CatalogTrack(
        track_id=TRACK_ID,
        status="RETIRADO",
        audio_asset_id=ASSET_ID,
        raw={},
    )

    with client:
        response = client.post(
            f"/api/v1/playback/tracks/{TRACK_ID}/stream-session",
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 409


def test_track_without_audio_is_not_playable() -> None:
    client, catalog, *_ = build_client()
    catalog.tracks[TRACK_ID] = CatalogTrack(
        track_id=TRACK_ID,
        status="PUBLICADO",
        audio_asset_id="",
        raw={},
    )

    with client:
        response = client.post(
            f"/api/v1/playback/tracks/{TRACK_ID}/stream-session",
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 409


def test_catalog_grpc_client_returns_playable_track() -> None:
    async def run() -> None:
        server = grpc.aio.server()
        catalog_playback_pb2_grpc.add_CatalogPlaybackServiceServicer_to_server(
            FakeCatalogPlaybackGrpcServicer(),
            server,
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            client = CatalogClient(f"127.0.0.1:{port}")
            track = await client.get_playable_track(TRACK_ID)
        finally:
            await server.stop(grace=None)

        assert track.track_id == TRACK_ID
        assert track.status == "PUBLICADO"
        assert track.audio_asset_id == ASSET_ID
        assert track.duration_seconds == 138

    asyncio.run(run())


def test_catalog_grpc_client_maps_missing_track() -> None:
    async def run() -> None:
        server = grpc.aio.server()
        catalog_playback_pb2_grpc.add_CatalogPlaybackServiceServicer_to_server(
            MissingCatalogPlaybackGrpcServicer(),
            server,
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            client = CatalogClient(f"127.0.0.1:{port}")
            try:
                await client.get_playable_track(TRACK_ID)
            except AppError as exc:
                assert exc.status_code == 404
                assert exc.code == "TrackNotFound"
            else:
                raise AssertionError("Expected AppError.")
        finally:
            await server.stop(grace=None)

    asyncio.run(run())


class FakeCatalogPlaybackGrpcServicer(
    catalog_playback_pb2_grpc.CatalogPlaybackServiceServicer
):
    async def GetPlayableTrack(self, request, context):
        return catalog_playback_pb2.PlayableTrackResponse(
            track_id=request.track_id,
            status="PUBLICADO",
            audio_asset_id=ASSET_ID,
            duration_seconds=138,
            exists=True,
        )


class MissingCatalogPlaybackGrpcServicer(
    catalog_playback_pb2_grpc.CatalogPlaybackServiceServicer
):
    async def GetPlayableTrack(self, request, context):
        await context.abort(grpc.StatusCode.NOT_FOUND, "Track not found.")


def test_progress_repository_creates_unique_user_track_index() -> None:
    client, _catalog, _storage, repository, _publisher, _library_repository = build_client()

    with client:
        response = client.get("/api/v1/playback/health")

    assert response.status_code == 200
    assert repository.index_name == USER_TRACK_INDEX_NAME
