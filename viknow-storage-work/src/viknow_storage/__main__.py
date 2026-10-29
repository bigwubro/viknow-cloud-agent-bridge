"""Run the storage API."""

from __future__ import annotations

import uvicorn

from viknow_storage.app import create_app
from viknow_storage.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
