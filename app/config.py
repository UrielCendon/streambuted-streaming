from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    streaming_port: int = Field(default=8084, alias="STREAMING_PORT")
    streaming_mongo_uri: str = Field(
        default="mongodb://streaming-mongo:27017",
        alias="STREAMING_MONGO_URI",
    )
    streaming_mongo_db: str = Field(
        default="streambuted_streaming",
        alias="STREAMING_MONGO_DB",
    )
    streaming_playback_token_secret: str = Field(
        default="replace_with_your_streaming_playback_secret",
        alias="STREAMING_PLAYBACK_TOKEN_SECRET",
    )
    streaming_playback_token_ttl_seconds: int = Field(
        default=300,
        alias="STREAMING_PLAYBACK_TOKEN_TTL_SECONDS",
    )
    streaming_valid_playback_seconds: float = Field(
        default=30,
        alias="STREAMING_VALID_PLAYBACK_SECONDS",
    )

    catalog_grpc_target: str = Field(
        default="catalog-service:9092",
        alias="CATALOG_GRPC_TARGET",
    )
    catalog_grpc_timeout_seconds: float = Field(
        default=5.0,
        alias="CATALOG_GRPC_TIMEOUT_SECONDS",
    )

    minio_endpoint: str = Field(default="minio:9000", alias="MINIO_ENDPOINT")
    minio_access_key: str = Field(default="streambuted", alias="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field(default="", alias="MINIO_SECRET_KEY")
    minio_bucket: str = Field(default="streambuted-media", alias="MINIO_BUCKET")
    minio_secure: bool = Field(default=False, alias="MINIO_SECURE")

    jwt_issuer: str = Field(
        default="http://identity-service:8081",
        alias="JWT_ISSUER",
    )
    jwt_jwks_url: str = Field(
        default="http://identity-service:8081/api/v1/auth/.well-known/jwks.json",
        alias="JWT_JWKS_URL",
    )
    jwt_audience: str | None = Field(default="streambuted-api", alias="JWT_AUDIENCE")

    rabbitmq_host: str = Field(default="rabbitmq", alias="RABBITMQ_HOST")
    rabbitmq_port: int = Field(default=5672, alias="RABBITMQ_PORT")
    rabbitmq_default_user: str = Field(
        default="streambuted",
        alias="RABBITMQ_DEFAULT_USER",
    )
    rabbitmq_default_pass: str = Field(default="", alias="RABBITMQ_DEFAULT_PASS")
    event_signing_secret: str = Field(default="", alias="EVENT_SIGNING_SECRET")

    cors_allowed_origins: str = Field(
        default="http://localhost:5173,http://localhost",
        alias="CORS_ALLOWED_ORIGINS",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    @field_validator("jwt_audience", mode="before")
    @classmethod
    def empty_string_to_none(cls, value: object) -> str | None:
        """Treat blank JWT_AUDIENCE as disabled audience validation."""
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return str(value)

    @property
    def allowed_cors_origins(self) -> list[str]:
        """Configured explicit browser origins allowed to call Streaming Service."""
        origins = [
            origin.strip()
            for origin in self.cors_allowed_origins.split(",")
            if origin.strip()
        ]
        if not origins or "*" in origins:
            raise ValueError(
                "CORS_ALLOWED_ORIGINS must define explicit origins and cannot include '*'."
            )
        return origins


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
