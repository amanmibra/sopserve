"""sopserve [--host H] [--port P] [--database-url URL]

Env: DATABASE_URL (default sqlite:///sopserve.db), SOPC_BIN for the sopc binary (default: `sopc` on PATH).
There is no authentication yet: keep it on a private network.
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from .app import create_app
from .sopc import Sopc
from .store import Store


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sopserve", description="Versioned sopc workspaces, and each agent's prompt at call start.")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8484)))
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", "sqlite:///sopserve.db"))
    args = parser.parse_args(argv)
    sopc = Sopc()
    if not sopc.binary:
        parser.error("sopc not found; install it or set SOPC_BIN")
    uvicorn.run(create_app(Store(args.database_url, sopc)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
