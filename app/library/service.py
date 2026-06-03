from app.catalog.client import CatalogClient, PublicCatalogTrack
from app.errors import AppError
from app.library.repository import (
    LibraryPlaylist,
    LibraryPlaylistItem,
    MongoLibraryRepository,
)
from app.media.client import MediaAssetClient
from app.library.schemas import (
    LibraryPlaylistDetailResponse,
    LibraryPlaylistResponse,
    LibrarySummaryResponse,
    LibraryTrackResponse,
    LikeStatusResponse,
)


class LibraryService:
    """Coordinates user likes, private playlists, and catalog hydration."""

    def __init__(
        self,
        repository: MongoLibraryRepository,
        catalog_client: CatalogClient,
        media_asset_client: MediaAssetClient,
    ) -> None:
        self._repository = repository
        self._catalog_client = catalog_client
        self._media_asset_client = media_asset_client

    async def get_summary(self, user_id: str) -> LibrarySummaryResponse:
        """Return the user's library landing data."""
        liked_playlist = await self._repository.get_or_create_liked_playlist(user_id)
        playlists = await self._repository.list_playlists(user_id)
        return LibrarySummaryResponse(
            likedSongs=await self._playlist_detail(liked_playlist),
            playlists=[
                await self._playlist_summary(playlist)
                for playlist in playlists
            ],
        )

    async def get_liked_songs(self, user_id: str) -> LibraryPlaylistDetailResponse:
        """Return the user's liked-songs playlist."""
        playlist = await self._repository.get_or_create_liked_playlist(user_id)
        return await self._playlist_detail(playlist)

    async def get_like_status(self, user_id: str, track_id: str) -> LikeStatusResponse:
        """Return whether a track is in the user's liked-songs playlist."""
        playlist = await self._repository.get_or_create_liked_playlist(user_id)
        return LikeStatusResponse(
            trackId=track_id,
            isLiked=await self._repository.has_track(playlist.playlist_id, track_id),
        )

    async def like_track(self, user_id: str, track_id: str) -> LikeStatusResponse:
        """Like a published track idempotently."""
        await self._catalog_client.get_playable_track(track_id)
        playlist = await self._repository.get_or_create_liked_playlist(user_id)
        await self._repository.add_track(playlist.playlist_id, track_id)
        return LikeStatusResponse(trackId=track_id, isLiked=True)

    async def unlike_track(self, user_id: str, track_id: str) -> LikeStatusResponse:
        """Remove a track from liked songs idempotently."""
        playlist = await self._repository.get_or_create_liked_playlist(user_id)
        await self._repository.remove_track(playlist.playlist_id, track_id)
        return LikeStatusResponse(trackId=track_id, isLiked=False)

    async def list_playlists(self, user_id: str) -> list[LibraryPlaylistResponse]:
        """List private playlists owned by the user."""
        playlists = await self._repository.list_playlists(user_id)
        return [await self._playlist_summary(playlist) for playlist in playlists]

    async def create_playlist(
        self,
        user_id: str,
        name: str,
        cover_asset_id: str | None,
        authorization_header: str | None,
    ) -> LibraryPlaylistResponse:
        """Create a private playlist."""
        await self._validate_playlist_name_uniqueness(user_id, name)
        await self._validate_playlist_cover(user_id, cover_asset_id, authorization_header)
        playlist = await self._repository.create_playlist(
            user_id=user_id,
            name=name,
            cover_asset_id=cover_asset_id,
        )
        return await self._playlist_summary(playlist)

    async def get_playlist(
        self,
        user_id: str,
        playlist_id: str,
    ) -> LibraryPlaylistDetailResponse:
        """Return a playlist owned by the user."""
        playlist = await self._require_playlist(user_id, playlist_id)
        return await self._playlist_detail(playlist)

    async def update_playlist(
        self,
        user_id: str,
        playlist_id: str,
        name: str | None,
        cover_asset_id: str | None,
        cover_asset_id_set: bool,
        authorization_header: str | None,
    ) -> LibraryPlaylistResponse:
        """Update mutable playlist metadata."""
        if name is None and not cover_asset_id_set:
            raise AppError(
                422,
                "ValidationError",
                "Debes enviar al menos un campo de la playlist.",
            )

        existing = await self._require_playlist(user_id, playlist_id, allow_system=True)
        if existing.is_system and name is not None:
            raise AppError(
                403,
                "SystemPlaylistImmutable",
                "Las playlists del sistema no pueden renombrarse.",
            )
        if name is not None:
            await self._validate_playlist_name_uniqueness(
                user_id,
                name,
                exclude_playlist_id=playlist_id,
            )
        if cover_asset_id_set:
            await self._validate_playlist_cover(user_id, cover_asset_id, authorization_header)

        playlist = await self._repository.update_playlist(
            user_id=user_id,
            playlist_id=playlist_id,
            name=name,
            cover_asset_id=cover_asset_id,
            cover_asset_id_set=cover_asset_id_set,
            allow_system=existing.is_system,
        )
        if playlist is None:
            raise playlist_not_found_error()
        return await self._playlist_summary(playlist)

    async def delete_playlist(self, user_id: str, playlist_id: str) -> None:
        """Delete a private playlist owned by the user."""
        deleted = await self._repository.delete_playlist(user_id, playlist_id)
        if not deleted:
            raise playlist_not_found_error()

    async def add_track_to_playlist(
        self,
        user_id: str,
        playlist_id: str,
        track_id: str,
    ) -> LibraryPlaylistDetailResponse:
        """Add a published track to a private playlist."""
        playlist = await self._require_playlist(user_id, playlist_id, allow_system=False)
        await self._catalog_client.get_playable_track(track_id)
        if await self._repository.has_track(playlist.playlist_id, track_id):
            raise AppError(
                409,
                "TrackAlreadyInPlaylist",
                "Esta cancion ya se encuentra en esa playlist.",
            )
        await self._repository.add_track(playlist.playlist_id, track_id)
        return await self._playlist_detail(playlist)

    async def remove_track_from_playlist(
        self,
        user_id: str,
        playlist_id: str,
        track_id: str,
    ) -> LibraryPlaylistDetailResponse:
        """Remove a track from a private playlist."""
        playlist = await self._require_playlist(user_id, playlist_id, allow_system=False)
        await self._repository.remove_track(playlist.playlist_id, track_id)
        return await self._playlist_detail(playlist)

    async def _require_playlist(
        self,
        user_id: str,
        playlist_id: str,
        allow_system: bool = True,
    ) -> LibraryPlaylist:
        playlist = await self._repository.find_playlist(user_id, playlist_id)
        if playlist is None or (playlist.is_system and not allow_system):
            raise playlist_not_found_error()
        return playlist

    async def _playlist_summary(self, playlist: LibraryPlaylist) -> LibraryPlaylistResponse:
        track_count = await self._playlist_visible_track_count(playlist.playlist_id)
        return LibraryPlaylistResponse(
            playlistId=playlist.playlist_id,
            name=playlist.name,
            coverAssetId=playlist.cover_asset_id,
            isSystem=playlist.is_system,
            systemKey=playlist.system_key,
            trackCount=track_count,
            createdAt=playlist.created_at,
            updatedAt=playlist.updated_at,
        )

    async def _playlist_detail(self, playlist: LibraryPlaylist) -> LibraryPlaylistDetailResponse:
        items = await self._repository.list_items(playlist.playlist_id)
        tracks = await self._hydrate_items(items)
        payload = {
            "playlistId": playlist.playlist_id,
            "name": playlist.name,
            "coverAssetId": playlist.cover_asset_id,
            "isSystem": playlist.is_system,
            "systemKey": playlist.system_key,
            "trackCount": len(tracks),
            "createdAt": playlist.created_at,
            "updatedAt": playlist.updated_at,
        }
        return LibraryPlaylistDetailResponse(
            **payload,
            tracks=tracks,
        )

    async def _playlist_visible_track_count(self, playlist_id: str) -> int:
        items = await self._repository.list_items(playlist_id)
        tracks = await self._hydrate_items(items)
        return len(tracks)

    async def _hydrate_items(
        self,
        items: list[LibraryPlaylistItem],
    ) -> list[LibraryTrackResponse]:
        tracks = await self._catalog_client.get_public_tracks_by_ids(
            [item.track_id for item in items]
        )
        track_by_id = {track.track_id: track for track in tracks}
        return [
            map_track_response(track_by_id[item.track_id], item)
            for item in items
            if item.track_id in track_by_id
        ]

    async def _validate_playlist_cover(
        self,
        user_id: str,
        cover_asset_id: str | None,
        authorization_header: str | None,
    ) -> None:
        if cover_asset_id is None:
            return

        metadata = await self._media_asset_client.get_asset_metadata(
            cover_asset_id,
            authorization_header,
        )
        if metadata.owner_user_id != user_id:
            raise AppError(403, "PlaylistCoverForbidden", "La portada de la playlist no es accesible.")
        if metadata.asset_type != "PLAYLIST_COVER":
            raise AppError(
                422,
                "InvalidPlaylistCoverType",
                "La portada debe ser un archivo de tipo PLAYLIST_COVER.",
            )

    async def _validate_playlist_name_uniqueness(
        self,
        user_id: str,
        name: str,
        exclude_playlist_id: str | None = None,
    ) -> None:
        existing = await self._repository.find_playlist_by_name(
            user_id,
            name,
            exclude_playlist_id=exclude_playlist_id,
        )
        if existing is not None:
            raise AppError(
                409,
                "PlaylistNameAlreadyExists",
                "Ya tienes una playlist con ese nombre.",
            )


def map_track_response(
    track: PublicCatalogTrack,
    item: LibraryPlaylistItem,
) -> LibraryTrackResponse:
    """Map catalog metadata and membership data into a public response."""
    return LibraryTrackResponse(
        trackId=track.track_id,
        artistId=track.artist_id,
        albumId=track.album_id,
        title=track.title,
        genre=track.genre,
        coverAssetId=track.cover_asset_id,
        durationSeconds=track.duration_seconds,
        artistName=track.artist_name,
        albumTitle=track.album_title,
        addedAt=item.added_at,
    )


def playlist_not_found_error() -> AppError:
    """Build a not-found error without revealing playlist ownership."""
    return AppError(404, "PlaylistNotFound", "La playlist no existe o no te pertenece.")
