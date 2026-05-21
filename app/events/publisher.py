import logging
from collections.abc import Awaitable
from typing import Any, Protocol

import pika
from pika.exceptions import AMQPError

from app.config import Settings
from app.events.signer import canonical_json, sign_serialized_payload

logger = logging.getLogger(__name__)

STREAMING_EXCHANGE = "streaming.events"
TRACK_PLAYBACK_COUNTED_ROUTING_KEY = "track.playback.counted"


class PlaybackEventPublisher(Protocol):
    """Publishes playback lifecycle events."""

    def publish_track_playback_counted(
        self,
        event: dict[str, Any],
    ) -> bool | Awaitable[bool]:
        """Publish a TrackPlaybackCounted event."""


class NoopPlaybackEventPublisher:
    """Publisher used when event configuration is intentionally unavailable."""

    def publish_track_playback_counted(self, event: dict[str, Any]) -> bool:
        """Skip publishing while keeping progress writes available."""
        logger.warning(
            "Skipping TrackPlaybackCounted publish for trackId=%s; RabbitMQ is not configured.",
            event.get("trackId"),
        )
        return False


class OutboxPlaybackEventPublisher:
    """Stores playback events locally so RabbitMQ outages do not drop them."""

    def __init__(self, event_outbox: Any) -> None:
        """Create an outbox-backed playback event publisher."""
        self._event_outbox = event_outbox

    async def publish_track_playback_counted(self, event: dict[str, Any]) -> bool:
        """Save a TrackPlaybackCounted event for asynchronous delivery."""
        await self._event_outbox.enqueue_track_playback_counted(event)
        return True


class RabbitMqPlaybackEventPublisher:
    """RabbitMQ publisher used by the playback outbox relay."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        signing_secret: str,
    ) -> None:
        """Create a RabbitMQ playback event publisher."""
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._signing_secret = signing_secret

    def publish_track_playback_counted(self, event: dict[str, Any]) -> bool:
        """Publish a TrackPlaybackCounted event after progress crosses the threshold."""
        payload_json = canonical_json(event)
        signature = sign_serialized_payload(payload_json, self._signing_secret)
        credentials = pika.PlainCredentials(self._username, self._password)
        parameters = pika.ConnectionParameters(
            host=self._host,
            port=self._port,
            credentials=credentials,
            heartbeat=30,
            blocked_connection_timeout=5,
            connection_attempts=1,
        )

        connection: pika.BlockingConnection | None = None
        try:
            connection = pika.BlockingConnection(parameters)
            channel = connection.channel()
            channel.exchange_declare(
                exchange=STREAMING_EXCHANGE,
                exchange_type="topic",
                durable=True,
            )
            channel.confirm_delivery()
            channel.basic_publish(
                exchange=STREAMING_EXCHANGE,
                routing_key=TRACK_PLAYBACK_COUNTED_ROUTING_KEY,
                body=payload_json.encode("utf-8"),
                properties=pika.BasicProperties(
                    content_type="application/json",
                    content_encoding="utf-8",
                    delivery_mode=2,
                    headers={"X-Event-Signature": signature},
                ),
            )
            logger.info(
                "Published TrackPlaybackCounted for trackId=%s userId=%s",
                event.get("trackId"),
                event.get("userId"),
            )
            return True
        except (AMQPError, OSError) as exc:
            logger.error(
                "Failed to publish TrackPlaybackCounted for trackId=%s: %s",
                event.get("trackId"),
                exc,
                exc_info=True,
            )
            return False
        finally:
            if connection and connection.is_open:
                connection.close()


def build_event_publisher(
    settings: Settings,
    event_outbox: Any | None = None,
) -> PlaybackEventPublisher:
    """Build the configured playback event publisher."""
    if not settings.event_signing_secret.strip():
        return NoopPlaybackEventPublisher()
    if not settings.rabbitmq_default_pass.strip():
        return NoopPlaybackEventPublisher()
    if event_outbox is not None:
        return OutboxPlaybackEventPublisher(event_outbox)

    return RabbitMqPlaybackEventPublisher(
        host=settings.rabbitmq_host,
        port=settings.rabbitmq_port,
        username=settings.rabbitmq_default_user,
        password=settings.rabbitmq_default_pass,
        signing_secret=settings.event_signing_secret,
    )
