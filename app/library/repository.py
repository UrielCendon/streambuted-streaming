from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.config import Settings

PLAYLIST_COLLECTION = "library_playlists"
PLAYLIST_ITEM_COLLECTION = "library_playlist_items"
LIKED_SONGS_KEY = "liked_songs"

USER_SYSTEM_PLAYLIST_INDEX = "idx_library_playlist_user_system_unique"
USER_PLAYLIST_INDEX = "idx_library_playlist_user_updated"
PLAYLIST_TRACK_INDEX = "idx_library_playlist_item_playlist_track_unique"
PLAYLIST_POSITION_INDEX = "idx_library_playlist_item_playlist_position"


@dataclass(frozen=True)
class LibraryPlaylist:
    """Stored user playlist."""

    playlist_id: str
    user_id: str
    name: str
    cover_asset_id: str | None
    is_system: bool
    system_key: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class LibraryPlaylistItem:
    """Stored track membership for a playlist."""

    playlist_id: str
    track_id: str
    position: int
    added_at: datetime


class MongoLibraryRepository:
    """MongoDB repository for user library state."""

    def __init__(self, client: Any, database_name: str) -> None:
        self._client = client
        self._database = client[database_name]
        self._playlists = self._database[PLAYLIST_COLLECTION]
        self._items = self._database[PLAYLIST_ITEM_COLLECTION]

    @classmethod
    def from_settings(cls, settings: Settings) -> "MongoLibraryRepository":
        """Create the repository from runtime settings."""
        from motor.motor_asyncio import AsyncIOMotorClient

        client = AsyncIOMotorClient(
            settings.streaming_mongo_uri,
            uuidRepresentation="standard",
        )
        return cls(client=client, database_name=settings.streaming_mongo_db)

    async def ensure_indexes(self) -> None:
        """Create required MongoDB indexes."""
        await self._unset_legacy_private_system_keys()
        await self._ensure_system_playlist_index()
        await self._playlists.create_index(
            [("user_id", 1), ("updated_at", -1)],
            name=USER_PLAYLIST_INDEX,
        )
        await self._items.create_index(
            [("playlist_id", 1), ("track_id", 1)],
            unique=True,
            name=PLAYLIST_TRACK_INDEX,
        )
        await self._items.create_index(
            [("playlist_id", 1), ("position", 1)],
            name=PLAYLIST_POSITION_INDEX,
        )

    async def _unset_legacy_private_system_keys(self) -> None:
        """Remove legacy null system keys from private playlists."""
        await self._playlists.update_many(
            {
                "is_system": False,
                "system_key": {"$exists": True},
            },
            {"$unset": {"system_key": ""}},
        )

    async def _ensure_system_playlist_index(self) -> None:
        """Keep the system-playlist uniqueness index scoped to system rows only."""
        expected_partial_filter = {
            "is_system": True,
            "system_key": {"$exists": True},
        }
        index_information = await self._playlists.index_information()
        existing_index = index_information.get(USER_SYSTEM_PLAYLIST_INDEX)

        if existing_index and (
            not existing_index.get("unique")
            or existing_index.get("partialFilterExpression") != expected_partial_filter
        ):
            await self._playlists.drop_index(USER_SYSTEM_PLAYLIST_INDEX)

        await self._playlists.create_index(
            [("user_id", 1), ("system_key", 1)],
            unique=True,
            partialFilterExpression=expected_partial_filter,
            name=USER_SYSTEM_PLAYLIST_INDEX,
        )

    async def get_or_create_liked_playlist(self, user_id: str) -> LibraryPlaylist:
        """Return the user's system liked-songs playlist."""
        existing = await self._playlists.find_one(
            {"user_id": user_id, "system_key": LIKED_SONGS_KEY}
        )
        if existing:
            return map_playlist(existing)

        now = datetime.now(UTC)
        document = {
            "playlist_id": str(uuid4()),
            "user_id": user_id,
            "name": "Canciones que te gustan",
            "cover_asset_id": None,
            "is_system": True,
            "system_key": LIKED_SONGS_KEY,
            "created_at": now,
            "updated_at": now,
        }
        try:
            await self._playlists.insert_one(document)
            return map_playlist(document)
        except get_duplicate_key_error():
            created = await self._playlists.find_one(
                {"user_id": user_id, "system_key": LIKED_SONGS_KEY}
            )
            return map_playlist(created)

    async def create_playlist(
        self,
        user_id: str,
        name: str,
        cover_asset_id: str | None,
    ) -> LibraryPlaylist:
        """Create a private user playlist."""
        now = datetime.now(UTC)
        document = {
            "playlist_id": str(uuid4()),
            "user_id": user_id,
            "name": name,
            "cover_asset_id": cover_asset_id,
            "is_system": False,
            "created_at": now,
            "updated_at": now,
        }
        await self._playlists.insert_one(document)
        return map_playlist(document)

    async def find_playlist_by_name(
        self,
        user_id: str,
        name: str,
        exclude_playlist_id: str | None = None,
    ) -> LibraryPlaylist | None:
        """Find a private playlist by exact name for a user."""
        filter_document: dict[str, object] = {
            "user_id": user_id,
            "name": name,
            "is_system": False,
        }
        if exclude_playlist_id is not None:
            filter_document["playlist_id"] = {"$ne": exclude_playlist_id}

        document = await self._playlists.find_one(filter_document)
        return map_playlist(document) if document else None

    async def find_playlist(
        self,
        user_id: str,
        playlist_id: str,
    ) -> LibraryPlaylist | None:
        """Find a playlist owned by a user."""
        document = await self._playlists.find_one(
            {"user_id": user_id, "playlist_id": playlist_id}
        )
        return map_playlist(document) if document else None

    async def list_playlists(self, user_id: str) -> list[LibraryPlaylist]:
        """List private user-created playlists."""
        cursor = self._playlists.find(
            {"user_id": user_id, "is_system": False}
        ).sort("updated_at", -1)
        return [map_playlist(document) async for document in cursor]

    async def update_playlist(
        self,
        user_id: str,
        playlist_id: str,
        name: str | None,
        cover_asset_id: str | None,
        cover_asset_id_set: bool = False,
        allow_system: bool = False,
    ) -> LibraryPlaylist | None:
        """Update mutable playlist metadata."""
        from pymongo import ReturnDocument

        updates: dict[str, Any] = {"updated_at": datetime.now(UTC)}
        if name is not None:
            updates["name"] = name
        if cover_asset_id_set:
            updates["cover_asset_id"] = cover_asset_id

        filter_document: dict[str, Any] = {
            "user_id": user_id,
            "playlist_id": playlist_id,
        }
        if not allow_system:
            filter_document["is_system"] = False
        document = await self._playlists.find_one_and_update(
            filter_document,
            {"$set": updates},
            return_document=ReturnDocument.AFTER,
        )
        return map_playlist(document) if document else None

    async def delete_playlist(self, user_id: str, playlist_id: str) -> bool:
        """Delete a private playlist and its items."""
        result = await self._playlists.delete_one(
            {
                "user_id": user_id,
                "playlist_id": playlist_id,
                "is_system": False,
            }
        )
        if result.deleted_count != 1:
            return False

        await self._items.delete_many({"playlist_id": playlist_id})
        return True

    async def add_track(
        self,
        playlist_id: str,
        track_id: str,
    ) -> LibraryPlaylistItem:
        """Add a track to a playlist idempotently."""
        existing = await self._items.find_one(
            {"playlist_id": playlist_id, "track_id": track_id}
        )
        if existing:
            return map_item(existing)

        now = datetime.now(UTC)
        position = await self._items.count_documents({"playlist_id": playlist_id})
        document = {
            "playlist_id": playlist_id,
            "track_id": track_id,
            "position": position,
            "added_at": now,
        }
        try:
            await self._items.insert_one(document)
            await self._touch_playlist(playlist_id)
            return map_item(document)
        except get_duplicate_key_error():
            created = await self._items.find_one(
                {"playlist_id": playlist_id, "track_id": track_id}
            )
            return map_item(created)

    async def remove_track(self, playlist_id: str, track_id: str) -> bool:
        """Remove a track from a playlist idempotently."""
        result = await self._items.delete_one(
            {"playlist_id": playlist_id, "track_id": track_id}
        )
        if result.deleted_count == 1:
            await self._touch_playlist(playlist_id)
            return True
        return False

    async def has_track(self, playlist_id: str, track_id: str) -> bool:
        """Return whether a playlist contains a track."""
        document = await self._items.find_one(
            {"playlist_id": playlist_id, "track_id": track_id},
            projection={"_id": 1},
        )
        return document is not None

    async def list_items(self, playlist_id: str) -> list[LibraryPlaylistItem]:
        """List playlist items in display order."""
        cursor = self._items.find({"playlist_id": playlist_id}).sort("position", 1)
        return [map_item(document) async for document in cursor]

    async def count_items(self, playlist_id: str) -> int:
        """Count tracks in a playlist."""
        return await self._items.count_documents({"playlist_id": playlist_id})

    async def _touch_playlist(self, playlist_id: str) -> None:
        await self._playlists.update_one(
            {"playlist_id": playlist_id},
            {"$set": {"updated_at": datetime.now(UTC)}},
        )

    def close(self) -> None:
        """Close the MongoDB client."""
        self._client.close()


def map_playlist(document: dict[str, Any] | None) -> LibraryPlaylist:
    """Map a MongoDB document into a playlist object."""
    if document is None:
        raise ValueError("Playlist document is required.")
    created_at = coerce_datetime(document.get("created_at"))
    updated_at = coerce_datetime(document.get("updated_at"), fallback=created_at)
    return LibraryPlaylist(
        playlist_id=str(document["playlist_id"]),
        user_id=str(document["user_id"]),
        name=str(document["name"]),
        cover_asset_id=str(document["cover_asset_id"]) if document.get("cover_asset_id") else None,
        is_system=bool(document.get("is_system", False)),
        system_key=str(document["system_key"]) if document.get("system_key") else None,
        created_at=created_at,
        updated_at=updated_at,
    )


def map_item(document: dict[str, Any] | None) -> LibraryPlaylistItem:
    """Map a MongoDB document into a playlist item object."""
    if document is None:
        raise ValueError("Playlist item document is required.")
    return LibraryPlaylistItem(
        playlist_id=str(document["playlist_id"]),
        track_id=str(document["track_id"]),
        position=int(document.get("position", 0)),
        added_at=coerce_datetime(document.get("added_at")),
    )


def coerce_datetime(value: object, fallback: datetime | None = None) -> datetime:
    """Return a datetime even if a legacy document is malformed."""
    if isinstance(value, datetime):
        return value
    return fallback or datetime.now(UTC)


def get_duplicate_key_error() -> type[Exception]:
    """Import PyMongo's DuplicateKeyError only when the real repository is used."""
    from pymongo.errors import DuplicateKeyError

    return DuplicateKeyError
