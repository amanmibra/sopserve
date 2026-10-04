"""sopserve [--host H] [--port P] [--data-dir DIR]

Set SOPSERVE_TOKEN to require `Authorization: Bearer <token>`.
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from .app import create_app
from .store import FileStore


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sopserve", description="Serve sopkit-built agent prompts over HTTP.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8484)
    parser.add_argument("--data-dir", default=os.environ.get("SOPSERVE_DATA_DIR", ".sopserve-data"))
    args = parser.parse_args(argv)
    uvicorn.run(create_app(FileStore(args.data_dir), os.environ.get("SOPSERVE_TOKEN")), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
