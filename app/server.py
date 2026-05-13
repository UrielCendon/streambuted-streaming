import asyncio
import logging

import uvicorn

from app.config import get_settings
from app.main import create_app

logger = logging.getLogger(__name__)


async def run() -> None:
    """Run the FastAPI HTTP server."""
    settings = get_settings()
    fastapi_app = create_app(settings=settings)
    logger.info("Streaming HTTP server listening on port %s", settings.streaming_port)

    server = uvicorn.Server(
        uvicorn.Config(
            fastapi_app,
            host="0.0.0.0",
            port=settings.streaming_port,
            log_level="info",
        ),
    )
    await server.serve()


def main() -> None:
    """Process entrypoint for Streaming Service."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
