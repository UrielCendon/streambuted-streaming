import asyncio

from app.library.repository import MongoLibraryRepository


class FakeCollection:
    def __init__(self) -> None:
        self.inserted_documents: list[dict[str, object]] = []

    async def insert_one(self, document: dict[str, object]) -> None:
        self.inserted_documents.append(document)


class FakeDatabase:
    def __init__(self) -> None:
        self.collections = {
            "library_playlists": FakeCollection(),
            "library_playlist_items": FakeCollection(),
        }

    def __getitem__(self, name: str) -> FakeCollection:
        return self.collections[name]


class FakeClient:
    def __init__(self) -> None:
        self.databases = {"streambuted_streaming": FakeDatabase()}

    def __getitem__(self, name: str) -> FakeDatabase:
        return self.databases[name]


def test_create_playlist_omits_system_key_for_private_playlists() -> None:
    client = FakeClient()
    repository = MongoLibraryRepository(client, "streambuted_streaming")

    playlist = asyncio.run(
        repository.create_playlist(
            user_id="user-1",
            name="Ruta",
            cover_asset_id=None,
        )
    )

    inserted = client["streambuted_streaming"]["library_playlists"].inserted_documents[0]

    assert playlist.system_key is None
    assert inserted["user_id"] == "user-1"
    assert inserted["name"] == "Ruta"
    assert inserted["is_system"] is False
    assert "system_key" not in inserted
