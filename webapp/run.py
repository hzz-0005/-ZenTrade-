"""Entrypoint: .venv/Scripts/python.exe -m webapp.run"""
from __future__ import annotations

import logging

import uvicorn


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    uvicorn.run("webapp.server.app:create_app", factory=True, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
