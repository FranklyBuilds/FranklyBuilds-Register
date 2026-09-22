"""OutlookRegisterPlus API entry point bundled with the main application."""

from __future__ import annotations

import os

import uvicorn

from api.app import create_app


app = create_app()


def main() -> None:
    host = os.environ.get("OUTLOOK_REGISTER_HOST", "127.0.0.1")
    port = int(os.environ.get("OUTLOOK_REGISTER_PORT", "8001"))
    token = str(os.environ.get("OUTLOOK_REGISTER_ADMIN_TOKEN") or "").strip()
    if token:
        os.environ["ADMIN_TOKEN"] = token
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
