"""Where a server keeps published builds and the fetch log. Plain files, so it self-hosts anywhere.

    <data_dir>/<workspace>/builds/<build_id>/   write_build output
    <data_dir>/<workspace>/current              build_id being served
    <data_dir>/<workspace>/fetches.jsonl        one line per prompt fetch
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from opensop.build import write_build
from opensop.render import Build

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class NotFound(Exception):
    pass


@dataclass(frozen=True)
class ServedPrompt:
    agent_id: str
    prompt: str
    hash: str
    build_id: str


class FileStore:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir)

    def publish(self, workspace: str, build: Build) -> str:
        """Store a build and make it current. Returns the build id (hash of its lock)."""
        ws = self._ws(workspace)
        lock = json.dumps(build.lock(), sort_keys=True)
        build_id = hashlib.sha256(lock.encode()).hexdigest()[:16]
        dest = ws / "builds" / build_id
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(dir=dest.parent))
            write_build(build, tmp)
            tmp.rename(dest)
        _atomic_write(ws / "current", build_id)
        return build_id

    def current_build(self, workspace: str) -> str:
        path = self._ws(workspace) / "current"
        if not path.exists():
            raise NotFound(f"workspace '{workspace}' has no published build")
        return path.read_text().strip()

    def prompt(self, workspace: str, agent: str) -> ServedPrompt:
        build_id = self.current_build(workspace)
        build_dir = self._ws(workspace) / "builds" / build_id
        lock = json.loads((build_dir / "lock.json").read_text())["agents"]
        agent_id = _resolve_agent(lock, agent)
        return ServedPrompt(
            agent_id=agent_id,
            prompt=(build_dir / f"{agent_id}.prompt.md").read_text(),
            hash=lock[agent_id]["hash"],
            build_id=build_id,
        )

    def sop(self, workspace: str, agent: str, sop_id: str) -> dict:
        build_id = self.current_build(workspace)
        build_dir = self._ws(workspace) / "builds" / build_id
        agent_id = _resolve_agent(json.loads((build_dir / "lock.json").read_text())["agents"], agent)
        path = build_dir / f"{agent_id}.tool.json"
        payload = json.loads(path.read_text()) if path.exists() else {}
        if sop_id not in payload:
            raise NotFound(f"SOP '{sop_id}' is not tool-delivered to '{agent_id}'")
        return payload[sop_id]

    def log_fetch(self, workspace: str, served: ServedPrompt, at: datetime | None = None) -> None:
        entry = {
            "at": (at or datetime.now(timezone.utc)).isoformat(),
            "agent": served.agent_id,
            "hash": served.hash,
            "build": served.build_id,
        }
        with open(self._ws(workspace) / "fetches.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")

    def fetches(self, workspace: str, agent: str | None = None) -> list[dict]:
        path = self._ws(workspace) / "fetches.jsonl"
        if not path.exists():
            return []
        entries = [json.loads(line) for line in path.read_text().splitlines() if line]
        return [e for e in entries if agent is None or e["agent"] == agent]

    def _ws(self, workspace: str) -> Path:
        if not _SAFE_ID.match(workspace):
            raise NotFound(f"invalid workspace id '{workspace}'")
        path = self.root / workspace
        path.mkdir(parents=True, exist_ok=True)
        return path


def _resolve_agent(lock: dict, agent: str) -> str:
    """Accept the alias ("tonys-pizza") or the platform ref ("livekit:tonys-pizza")."""
    if agent in lock:
        return agent
    for agent_id, entry in lock.items():
        if entry["platform_ref"] == agent:
            return agent_id
    raise NotFound(f"agent '{agent}' not found")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
