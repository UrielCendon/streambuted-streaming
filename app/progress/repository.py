from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.config import Settings

PLAYBACK_PROGRESS_COLLECTION = "playback_progress"
USER_TRACK_INDEX_NAME = "idx_playback_progress_user_track_unique"
USER_INDEX_NAME = "idx_playback_progress_user_unique"


@dataclass(frozen=True)
class PlaybackProgress:
    """Latest playback progress stored for a user."""

    user_id: str
    track_id: str
    position_seconds: float
    duration_seconds: float | None
    playback_counted: bool
    created_at: datetime
    updated_at: datetime


class MongoPlaybackProgressRepository:
    """MongoDB repository for playback progress."""

    def __init__(
        self,
        client: Any,
        database_name: str,
    ) -> None:
        """Create a MongoDB progress repository."""
        self._client = client
        self._database = client[database_name]
        self._collection = self._database[PLAYBACK_PROGRESS_COLLECTION]

    @classmethod
    def from_settings(cls, settings: Settings) -> "MongoPlaybackProgressRepository":
        """Create the repository from runtime settings."""
        from motor.motor_asyncio import AsyncIOMotorClient

        client = AsyncIOMotorClient(
            settings.streaming_mongo_uri,
            uuidRepresentation="standard",
        )
        return cls(client=client, database_name=settings.streaming_mongo_db)

    async def ensure_indexes(self) -> None:
        """Create required MongoDB indexes."""
        await self._drop_legacy_user_track_index()
        await self._collapse_legacy_user_documents()
        await self._collection.create_index(
            [("user_id", 1)],
            unique=True,
            name=USER_INDEX_NAME,
        )

    async def _drop_legacy_user_track_index(self) -> None:
        indexes = await self._collection.index_information()
        if USER_TRACK_INDEX_NAME in indexes:
            await self._collection.drop_index(USER_TRACK_INDEX_NAME)

    async def _collapse_legacy_user_documents(self) -> None:
        """Keep only the most recently updated progress document per user."""
        cursor = self._collection.aggregate(
            [
                {"$sort": {"updated_at": -1, "created_at": -1}},
                {
                    "$group": {
                        "_id": "$user_id",
                        "keep_id": {"$first": "$_id"},
                        "all_ids": {"$push": "$_id"},
                        "count": {"$sum": 1},
                    }
                },
                {"$match": {"count": {"$gt": 1}}},
            ]
        )
        async for group in cursor:
            delete_ids = [
                item_id for item_id in group["all_ids"] if item_id != group["keep_id"]
            ]
            if delete_ids:
                await self._collection.delete_many({"_id": {"$in": delete_ids}})

    async def get_progress(
        self,
        user_id: str,
        track_id: str,
    ) -> PlaybackProgress | None:
        """Get playback progress for a user and a track."""
        document = await self._collection.find_one(
            {
                "user_id": user_id,
            }
        )
        if not document or str(document.get("track_id")) != track_id:
            return None
        return map_progress(document)

    async def get_latest_progress(self, user_id: str) -> PlaybackProgress | None:
        """Get the latest stored playback progress for a user."""
        document = await self._collection.find_one({"user_id": user_id})
        return map_progress(document) if document else None

    async def upsert_progress(
        self,
        user_id: str,
        track_id: str,
        position_seconds: float,
        duration_seconds: float | None,
    ) -> PlaybackProgress:
        """Insert or update the user's latest playback progress."""
        from pymongo import ReturnDocument

        now = datetime.now(UTC)
        existing = await self._collection.find_one({"user_id": user_id})
        same_track = existing is not None and str(existing.get("track_id")) == track_id

        document = await self._collection.find_one_and_update(
            {
                "user_id": user_id,
            },
            {
                "$set": {
                    "track_id": track_id,
                    "position_seconds": position_seconds,
                    "duration_seconds": duration_seconds,
                    "playback_counted": bool(existing.get("playback_counted", False))
                    if same_track
                    else False,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "user_id": user_id,
                    "created_at": now,
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return map_progress(document)

    async def mark_playback_counted(
        self,
        user_id: str,
        track_id: str,
    ) -> bool:
        """Mark playback as counted once for user and track."""
        result = await self._collection.update_one(
            {
                "user_id": user_id,
                "track_id": track_id,
                "playback_counted": {"$ne": True},
            },
            {
                "$set": {
                    "playback_counted": True,
                    "updated_at": datetime.now(UTC),
                }
            },
        )
        return result.modified_count == 1

    def close(self) -> None:
        """Close the MongoDB client."""
        self._client.close()


def map_progress(document: dict[str, Any]) -> PlaybackProgress:
    """Map a MongoDB document into a domain progress object."""
    created_at = document.get("created_at")
    updated_at = document.get("updated_at")
    if not isinstance(created_at, datetime):
        created_at = datetime.now(UTC)
    if not isinstance(updated_at, datetime):
        updated_at = created_at

    return PlaybackProgress(
        user_id=str(document["user_id"]),
        track_id=str(document["track_id"]),
        position_seconds=float(document.get("position_seconds", 0)),
        duration_seconds=to_optional_float(document.get("duration_seconds")),
        playback_counted=bool(document.get("playback_counted", False)),
        created_at=created_at,
        updated_at=updated_at,
    )


def to_optional_float(value: object) -> float | None:
    """Convert a possible numeric Mongo value to float."""
    if value is None:
        return None
    return float(value)
