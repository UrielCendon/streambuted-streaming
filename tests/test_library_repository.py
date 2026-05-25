import asyncio

from app.library.repository import MongoLibraryRepository


class FakeCollection:
    def __init__(self) -> None:
        self.inserted_documents: list[dict[str, object]] = []
        self.updated_filters: list[dict[str, object]] = []
        self.updated_payloads: list[dict[str, object]] = []
        self.created_indexes: list[dict[str, object]] = []
        self.dropped_indexes: list[str] = []
        self.indexes: dict[str, dict[str, object]] = {}

    async def insert_one(self, document: dict[str, object]) -> None:
        self.inserted_documents.append(document)

    async def update_many(self, filter_document: dict[str, object], update_document: dict[str, object]) -> None:
        self.updated_filters.append(filter_document)
        self.updated_payloads.append(update_document)

    async def index_information(self) -> dict[str, dict[str, object]]:
        return self.indexes

    async def drop_index(self, name: str) -> None:
        self.dropped_indexes.append(name)
        self.indexes.pop(name, None)

    async def create_index(self, keys: list[tuple[str, int]], **kwargs: object) -> None:
        self.created_indexes.append({"keys": keys, **kwargs})
        name = str(kwargs["name"])
        self.indexes[name] = {"key": keys, **kwargs}


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


def test_ensure_indexes_recreates_system_playlist_index_as_partial() -> None:
    client = FakeClient()
    playlists = client["streambuted_streaming"]["library_playlists"]
    playlists.indexes["idx_library_playlist_user_system_unique"] = {
        "key": [("user_id", 1), ("system_key", 1)],
        "unique": True,
        "sparse": True,
        "name": "idx_library_playlist_user_system_unique",
    }
    repository = MongoLibraryRepository(client, "streambuted_streaming")

    asyncio.run(repository.ensure_indexes())

    assert playlists.updated_filters == [{
        "is_system": False,
        "system_key": {"$exists": True},
    }]
    assert playlists.updated_payloads == [{"$unset": {"system_key": ""}}]
    assert playlists.dropped_indexes == ["idx_library_playlist_user_system_unique"]
    assert playlists.created_indexes[0]["partialFilterExpression"] == {
        "is_system": True,
        "system_key": {"$exists": True},
    }
