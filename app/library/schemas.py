from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

PlaylistName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class LibraryTrackResponse(BaseModel):
    """Track metadata returned by library endpoints."""

    track_id: str = Field(..., alias="trackId")
    artist_id: str = Field(..., alias="artistId")
    album_id: str | None = Field(default=None, alias="albumId")
    title: str
    genre: str
    cover_asset_id: str | None = Field(default=None, alias="coverAssetId")
    duration_seconds: float | None = Field(default=None, alias="durationSeconds")
    artist_name: str = Field(..., alias="artistName")
    album_title: str | None = Field(default=None, alias="albumTitle")
    added_at: datetime | None = Field(default=None, alias="addedAt")

    model_config = ConfigDict(populate_by_name=True)


class LibraryPlaylistResponse(BaseModel):
    """Playlist summary returned by library endpoints."""

    playlist_id: str = Field(..., alias="playlistId")
    name: str
    cover_asset_id: str | None = Field(default=None, alias="coverAssetId")
    is_system: bool = Field(..., alias="isSystem")
    system_key: str | None = Field(default=None, alias="systemKey")
    track_count: int = Field(..., alias="trackCount")
    created_at: datetime = Field(..., alias="createdAt")
    updated_at: datetime = Field(..., alias="updatedAt")

    model_config = ConfigDict(populate_by_name=True)


class LibraryPlaylistDetailResponse(LibraryPlaylistResponse):
    """Playlist response including hydrated tracks."""

    tracks: list[LibraryTrackResponse]


class LibrarySummaryResponse(BaseModel):
    """Top-level user library response."""

    liked_songs: LibraryPlaylistDetailResponse = Field(..., alias="likedSongs")
    playlists: list[LibraryPlaylistResponse]

    model_config = ConfigDict(populate_by_name=True)


class LikeStatusResponse(BaseModel):
    """Response with the like state for one track."""

    track_id: str = Field(..., alias="trackId")
    is_liked: bool = Field(..., alias="isLiked")

    model_config = ConfigDict(populate_by_name=True)


class CreatePlaylistRequest(BaseModel):
    """Request body used to create a private playlist."""

    name: PlaylistName
    cover_asset_id: str | None = Field(default=None, alias="coverAssetId", pattern=UUID_PATTERN)

    model_config = ConfigDict(populate_by_name=True)


class UpdatePlaylistRequest(BaseModel):
    """Request body used to update a private playlist."""

    name: PlaylistName | None = None
    cover_asset_id: str | None = Field(default=None, alias="coverAssetId", pattern=UUID_PATTERN)

    model_config = ConfigDict(populate_by_name=True)


class AddPlaylistTrackRequest(BaseModel):
    """Request body used to add a track to a playlist."""

    track_id: str = Field(..., alias="trackId", pattern=UUID_PATTERN)

    model_config = ConfigDict(populate_by_name=True)
