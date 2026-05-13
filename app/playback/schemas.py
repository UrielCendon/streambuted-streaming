from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HealthResponse(BaseModel):
    """Health response returned by Playback endpoints."""

    status: str = Field(..., description="Service health status")
    service: str = Field(..., description="Service name")


class StreamSessionResponse(BaseModel):
    """Response returned after creating an ephemeral stream session."""

    stream_url: str = Field(..., alias="streamUrl")
    expires_at: datetime = Field(..., alias="expiresAt")
    track_id: str = Field(..., alias="trackId")

    model_config = ConfigDict(populate_by_name=True)


class PlaybackProgressRequest(BaseModel):
    """Request body used to save playback progress."""

    position_seconds: float = Field(..., ge=0, alias="positionSeconds")
    duration_seconds: Optional[float] = Field(default=None, alias="durationSeconds")
    is_playing: Optional[bool] = Field(default=None, alias="isPlaying")

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def validate_duration(self) -> "PlaybackProgressRequest":
        """Validate duration against current playback position."""
        if self.duration_seconds is not None and self.duration_seconds < self.position_seconds:
            raise ValueError("durationSeconds must be greater than or equal to positionSeconds.")
        return self


class PlaybackProgressCompatRequest(PlaybackProgressRequest):
    """Compatibility request body that carries trackId in the body."""

    track_id: str = Field(..., alias="trackId")


class PlaybackProgressResponse(BaseModel):
    """Response returned when reading or saving playback progress."""

    track_id: str = Field(..., alias="trackId")
    position_seconds: float = Field(..., alias="positionSeconds")
    duration_seconds: Optional[float] = Field(default=None, alias="durationSeconds")
    updated_at: Optional[datetime] = Field(default=None, alias="updatedAt")

    model_config = ConfigDict(populate_by_name=True)


class LatestPlaybackProgressResponse(BaseModel):
    """Response returned for the user's latest playback state."""

    track_id: Optional[str] = Field(default=None, alias="trackId")
    position_seconds: float = Field(default=0, alias="positionSeconds")
    duration_seconds: Optional[float] = Field(default=None, alias="durationSeconds")
    updated_at: Optional[datetime] = Field(default=None, alias="updatedAt")

    model_config = ConfigDict(populate_by_name=True)
