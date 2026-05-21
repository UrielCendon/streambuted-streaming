import asyncio
import inspect
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import Settings
from app.events.publisher import PlaybackEventPublisher

PLAYBACK_EVENT_OUTBOX_COLLECTION = "playback_event_outbox"
STATUS_CREATED_INDEX_NAME = "idx_playback_event_outbox_status_created"
EVENT_ID_INDEX_NAME = "idx_playback_event_outbox_event_id_unique"
MAX_RETRY_COUNT = 5
FAILED_RETRY_MIN_AGE = timedelta(hours=1)
DEFAULT_POLL_INTERVAL_SECONDS = 5

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlaybackEventOutboxRecord:
    """Pending playback event stored for asynchronous delivery."""

    event_id: str
    payload: dict[str, Any]
    retry_count: int


class MongoPlaybackEventOutbox:
    """MongoDB outbox for playback analytics events."""

    def __init__(self, client: Any, database_name: str) -> None:
        """Create the MongoDB outbox."""
        self._client = client
        self._database = client[database_name]
        self._collection = self._database[PLAYBACK_EVENT_OUTBOX_COLLECTION]

    @classmethod
    def from_settings(cls, settings: Settings) -> "MongoPlaybackEventOutbox":
        """Create the outbox from runtime settings."""
        from motor.motor_asyncio import AsyncIOMotorClient

        client = AsyncIOMotorClient(
            settings.streaming_mongo_uri,
            uuidRepresentation="standard",
        )
        return cls(client=client, database_name=settings.streaming_mongo_db)

    async def ensure_indexes(self) -> None:
        """Create MongoDB indexes required by the event outbox."""
        await self._collection.create_index(
            "event_id",
            unique=True,
            name=EVENT_ID_INDEX_NAME,
        )
        await self._collection.create_index(
            [("status", 1), ("created_at", 1)],
            name=STATUS_CREATED_INDEX_NAME,
        )

    async def enqueue_track_playback_counted(self, event: dict[str, Any]) -> bool:
        """Store one TrackPlaybackCounted event once."""
        event_id = str(event["eventId"])
        now = datetime.now(UTC)
        result = await self._collection.update_one(
            {"event_id": event_id},
            {
                "$setOnInsert": {
                    "event_id": event_id,
                    "event_type": "TrackPlaybackCounted",
                    "routing_key": "track.playback.counted",
                    "payload": event,
                    "status": "PENDING",
                    "retry_count": 0,
                    "created_at": now,
                    "processed_at": None,
                }
            },
            upsert=True,
        )
        return result.upserted_id is not None

    async def fetch_pending(self, limit: int = 50) -> list[PlaybackEventOutboxRecord]:
        """Return pending events ordered by creation time."""
        cursor = (
            self._collection.find({"status": "PENDING"})
            .sort("created_at", 1)
            .limit(limit)
        )
        documents = await cursor.to_list(length=limit)
        return [
            PlaybackEventOutboxRecord(
                event_id=str(document["event_id"]),
                payload=dict(document["payload"]),
                retry_count=int(document.get("retry_count", 0)),
            )
            for document in documents
        ]

    async def mark_processed(self, event_id: str) -> None:
        """Mark an outbox event as delivered."""
        await self._collection.update_one(
            {"event_id": event_id},
            {
                "$set": {
                    "status": "PROCESSED",
                    "processed_at": datetime.now(UTC),
                }
            },
        )

    async def mark_retry_or_failed(self, event_id: str, retry_count: int) -> None:
        """Increment retry count and eventually park an event as failed."""
        next_retry_count = retry_count + 1
        status = "FAILED" if next_retry_count >= MAX_RETRY_COUNT else "PENDING"
        update: dict[str, Any] = {
            "status": status,
            "retry_count": next_retry_count,
        }
        if status == "FAILED":
            update["processed_at"] = datetime.now(UTC)

        await self._collection.update_one(
            {"event_id": event_id},
            {"$set": update},
        )

    async def retry_stale_failed_events(self) -> None:
        """Requeue failed events after a cooldown, matching Identity outbox behavior."""
        cutoff = datetime.now(UTC) - FAILED_RETRY_MIN_AGE
        await self._collection.update_many(
            {
                "status": "FAILED",
                "created_at": {"$lt": cutoff},
            },
            {
                "$set": {
                    "status": "PENDING",
                    "retry_count": 0,
                    "processed_at": None,
                }
            },
        )

    def close(self) -> None:
        """Close the MongoDB client."""
        self._client.close()


class PlaybackEventOutboxProcessor:
    """Relays pending playback events from MongoDB to RabbitMQ."""

    def __init__(
        self,
        event_outbox: MongoPlaybackEventOutbox,
        rabbitmq_publisher: PlaybackEventPublisher,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        """Create the async outbox relay."""
        self._event_outbox = event_outbox
        self._rabbitmq_publisher = rabbitmq_publisher
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the outbox relay on the current event loop."""
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="playback-event-outbox")

    async def stop(self) -> None:
        """Stop the outbox relay."""
        self._stop_event.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.process_pending_events()
            except Exception as exc:
                logger.error("Playback event outbox relay failed: %s", exc, exc_info=True)

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except TimeoutError:
                continue

    async def process_pending_events(self) -> None:
        """Publish pending events and update their delivery state."""
        await self._event_outbox.retry_stale_failed_events()
        for record in await self._event_outbox.fetch_pending():
            try:
                publish_result = self._rabbitmq_publisher.publish_track_playback_counted(
                    record.payload
                )
                if inspect.isawaitable(publish_result):
                    published = bool(await publish_result)
                else:
                    published = bool(publish_result)

                if published:
                    await self._event_outbox.mark_processed(record.event_id)
                else:
                    await self._event_outbox.mark_retry_or_failed(
                        record.event_id,
                        record.retry_count,
                    )
            except Exception as exc:
                logger.error(
                    "Failed to relay TrackPlaybackCounted event_id=%s: %s",
                    record.event_id,
                    exc,
                    exc_info=True,
                )
                await self._event_outbox.mark_retry_or_failed(
                    record.event_id,
                    record.retry_count,
                )
