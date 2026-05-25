from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request, Response, status

from app.auth.jwt_validator import JwtValidator
from app.auth.models import AuthenticatedUser
from app.library.schemas import (
    AddPlaylistTrackRequest,
    CreatePlaylistRequest,
    LibraryPlaylistDetailResponse,
    LibraryPlaylistResponse,
    LibrarySummaryResponse,
    LikeStatusResponse,
    UpdatePlaylistRequest,
)
from app.library.service import LibraryService

UUID_PATH = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
UuidPath = Annotated[str, Path(pattern=UUID_PATH)]

router = APIRouter(prefix="/api/v1/library", tags=["Library"])


def get_library_service(request: Request) -> LibraryService:
    """Resolve the library service from application state."""
    return request.app.state.library_service


def get_jwt_validator(request: Request) -> JwtValidator:
    """Resolve the JWT validator from application state."""
    return request.app.state.jwt_validator


def get_current_user(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    validator: JwtValidator = Depends(get_jwt_validator),
) -> AuthenticatedUser:
    """Resolve the authenticated user from the Authorization header."""
    return validator.validate_authorization_header(authorization)


@router.get("", response_model=LibrarySummaryResponse)
@router.get("/", response_model=LibrarySummaryResponse, include_in_schema=False)
async def get_library_summary(
    current_user: AuthenticatedUser = Depends(get_current_user),
    library_service: LibraryService = Depends(get_library_service),
) -> LibrarySummaryResponse:
    """Return the authenticated user's library summary."""
    return await library_service.get_summary(current_user.subject)


@router.get("/liked-songs", response_model=LibraryPlaylistDetailResponse)
async def get_liked_songs(
    current_user: AuthenticatedUser = Depends(get_current_user),
    library_service: LibraryService = Depends(get_library_service),
) -> LibraryPlaylistDetailResponse:
    """Return the authenticated user's liked songs."""
    return await library_service.get_liked_songs(current_user.subject)


@router.get("/tracks/{track_id}/like-status", response_model=LikeStatusResponse)
async def get_like_status(
    track_id: UuidPath,
    current_user: AuthenticatedUser = Depends(get_current_user),
    library_service: LibraryService = Depends(get_library_service),
) -> LikeStatusResponse:
    """Return whether the authenticated user likes the track."""
    return await library_service.get_like_status(current_user.subject, track_id)


@router.put("/tracks/{track_id}/like", response_model=LikeStatusResponse)
async def like_track(
    track_id: UuidPath,
    current_user: AuthenticatedUser = Depends(get_current_user),
    library_service: LibraryService = Depends(get_library_service),
) -> LikeStatusResponse:
    """Like a published track."""
    return await library_service.like_track(current_user.subject, track_id)


@router.delete("/tracks/{track_id}/like", response_model=LikeStatusResponse)
async def unlike_track(
    track_id: UuidPath,
    current_user: AuthenticatedUser = Depends(get_current_user),
    library_service: LibraryService = Depends(get_library_service),
) -> LikeStatusResponse:
    """Remove a track from liked songs."""
    return await library_service.unlike_track(current_user.subject, track_id)


@router.get("/playlists", response_model=list[LibraryPlaylistResponse])
async def list_playlists(
    current_user: AuthenticatedUser = Depends(get_current_user),
    library_service: LibraryService = Depends(get_library_service),
) -> list[LibraryPlaylistResponse]:
    """List private playlists owned by the authenticated user."""
    return await library_service.list_playlists(current_user.subject)


@router.post(
    "/playlists",
    response_model=LibraryPlaylistResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_playlist(
    request: CreatePlaylistRequest,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    current_user: AuthenticatedUser = Depends(get_current_user),
    library_service: LibraryService = Depends(get_library_service),
) -> LibraryPlaylistResponse:
    """Create a private playlist for the authenticated user."""
    return await library_service.create_playlist(
        user_id=current_user.subject,
        name=request.name,
        cover_asset_id=request.cover_asset_id,
        authorization_header=authorization,
    )


@router.get("/playlists/{playlist_id}", response_model=LibraryPlaylistDetailResponse)
async def get_playlist(
    playlist_id: UuidPath,
    current_user: AuthenticatedUser = Depends(get_current_user),
    library_service: LibraryService = Depends(get_library_service),
) -> LibraryPlaylistDetailResponse:
    """Return a private playlist owned by the authenticated user."""
    return await library_service.get_playlist(current_user.subject, playlist_id)


@router.patch("/playlists/{playlist_id}", response_model=LibraryPlaylistResponse)
async def update_playlist(
    playlist_id: UuidPath,
    request: UpdatePlaylistRequest,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    current_user: AuthenticatedUser = Depends(get_current_user),
    library_service: LibraryService = Depends(get_library_service),
) -> LibraryPlaylistResponse:
    """Update a private playlist owned by the authenticated user."""
    return await library_service.update_playlist(
        user_id=current_user.subject,
        playlist_id=playlist_id,
        name=request.name,
        cover_asset_id=request.cover_asset_id,
        cover_asset_id_set="cover_asset_id" in request.model_fields_set or "coverAssetId" in request.model_fields_set,
        authorization_header=authorization,
    )


@router.delete("/playlists/{playlist_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_playlist(
    playlist_id: UuidPath,
    current_user: AuthenticatedUser = Depends(get_current_user),
    library_service: LibraryService = Depends(get_library_service),
) -> Response:
    """Delete a private playlist owned by the authenticated user."""
    await library_service.delete_playlist(current_user.subject, playlist_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/playlists/{playlist_id}/tracks", response_model=LibraryPlaylistDetailResponse)
async def add_track_to_playlist(
    playlist_id: UuidPath,
    request: AddPlaylistTrackRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    library_service: LibraryService = Depends(get_library_service),
) -> LibraryPlaylistDetailResponse:
    """Add a published track to a private playlist."""
    return await library_service.add_track_to_playlist(
        user_id=current_user.subject,
        playlist_id=playlist_id,
        track_id=request.track_id,
    )


@router.delete(
    "/playlists/{playlist_id}/tracks/{track_id}",
    response_model=LibraryPlaylistDetailResponse,
)
async def remove_track_from_playlist(
    playlist_id: UuidPath,
    track_id: UuidPath,
    current_user: AuthenticatedUser = Depends(get_current_user),
    library_service: LibraryService = Depends(get_library_service),
) -> LibraryPlaylistDetailResponse:
    """Remove a track from a private playlist."""
    return await library_service.remove_track_from_playlist(
        user_id=current_user.subject,
        playlist_id=playlist_id,
        track_id=track_id,
    )
