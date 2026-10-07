"""Versioned files, releases and the fetch log, in SQLite or Postgres (via SQLAlchemy Core).

Every file is versioned on its own. A release is an immutable {path: version} snapshot plus its compiled build;
the workspace's current release is what agents get. Writes compile the resulting files first, so head always compiles.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Iterator

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from . import forms
from .sopc import Files, Invalid, Issue, Sopc, path_issues

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

metadata = MetaData()

workspaces = Table(
    "workspaces",
    metadata,
    Column("workspace", String(128), primary_key=True),
    Column("current_release", Integer, nullable=True),
)

file_versions = Table(
    "file_versions",
    metadata,
    Column("workspace", String(128), primary_key=True),
    Column("path", String(512), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("content", Text, nullable=True),  # NULL: deleted in this version
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("author", String(256), nullable=True),
    Column("note", Text, nullable=True),
)

releases = Table(
    "releases",
    metadata,
    Column("workspace", String(128), primary_key=True),
    Column("number", Integer, primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("author", String(256), nullable=True),
    Column("note", Text, nullable=True),
    Column("files", JSON, nullable=False),  # {path: version}
    Column("build", JSON, nullable=False),  # {"agents": {id: {platform_ref, hash, prompt, tools, sops, components}}}
)

fetches = Table(
    "fetches",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("workspace", String(128), nullable=False, index=True),
    Column("agent", String(256), nullable=False),
    Column("release", Integer, nullable=False),
    Column("hash", String(128), nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
)

# Which folder each lock.json block kind lives in.
_KIND_DIRS = {"agent": "agents", "base": "bases", "sop": "procedures"}
CONFIG = "sopc.yaml"


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def engine_from_url(url: str) -> Engine:
    """`postgres://` and `postgresql://` URLs (as Supabase and Neon hand out) use psycopg 3."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix) :]
    return create_engine(url, pool_pre_ping=True)


class Store:
    def __init__(self, engine: Engine | str, sopc: Sopc | None = None):
        self.engine = engine_from_url(engine) if isinstance(engine, str) else engine
        self.sopc = sopc or Sopc()
        metadata.create_all(self.engine)

    # --- workspaces & files ----------------------------------------------------

    def workspaces(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(workspaces).order_by(workspaces.c.workspace)).mappings()]

    def head(self, workspace: str, content: bool = False) -> list[dict]:
        """The latest version of every file that isn't deleted."""
        with self.engine.connect() as conn:
            return [_file_out(r, content) for r in self._head_rows(conn, _ws(workspace))]

    def file(self, workspace: str, path: str, version: int | None = None) -> dict:
        q = select(file_versions).where(file_versions.c.workspace == _ws(workspace), file_versions.c.path == path)
        q = q.where(file_versions.c.version == version) if version else q.order_by(file_versions.c.version.desc()).limit(1)
        with self.engine.connect() as conn:
            row = conn.execute(q).mappings().first()
        if not row or (version is None and row["content"] is None):
            raise NotFound(f"file '{path}' not found" + (f" at version {version}" if version else ""))
        return _file_out(row, content=True)

    def history(self, workspace: str, path: str) -> list[dict]:
        q = (
            select(file_versions)
            .where(file_versions.c.workspace == _ws(workspace), file_versions.c.path == path)
            .order_by(file_versions.c.version.desc())
        )
        with self.engine.connect() as conn:
            rows = [_file_out(r) for r in conn.execute(q).mappings()]
        if not rows:
            raise NotFound(f"file '{path}' not found")
        return rows

    def edit(self, workspace: str, changes: dict[str, str | None], author: str | None = None, note: str | None = None) -> list[dict]:
        """Apply {path: content, or None to delete} atomically. Raises Invalid if the result doesn't compile."""
        _check_paths(changes)
        with self._locked(workspace) as conn:
            return self._write(conn, workspace, changes, author, note, replace=False)

    def replace(self, workspace: str, files: Files, author: str | None = None, note: str | None = None) -> list[dict]:
        """Make head exactly `files`; paths not in it are deleted."""
        _check_paths(files)
        with self._locked(workspace) as conn:
            return self._write(conn, workspace, files, author, note, replace=True)

    # --- items: agents, bases, procedures and settings as structured data ------------

    def items(self, workspace: str) -> dict:
        """Head as {settings, agents, bases, procedures}, each with its version."""
        ws = _ws(workspace)
        with self.engine.connect() as conn:
            head = self._head_rows(conn, ws)
        return self._items(head)

    def _items(self, head: list[dict]) -> dict:
        files = {r["path"]: r["content"] for r in head}
        if forms.CONFIG in files:
            items = forms.from_export(self.sopc.export(files))
        else:  # a new workspace: sopc.yaml is written with the first item
            items = {"settings": {"variables": {}, "procedures_heading": forms.DEFAULT_HEADING, "procedure_order": []}, "agents": [], "bases": [], "procedures": []}
        rows = {r["path"]: r for r in head}
        for item in [items["settings"], *items["agents"], *items["bases"], *items["procedures"]]:
            row = rows.get(item.get("file", forms.CONFIG))
            item["version"] = row["version"] if row else None
            item["updated_at"] = _utc(row["created_at"]) if row else None
            item["updated_by"] = row["author"] if row else None
        items["settings"]["file"] = forms.CONFIG
        return items

    def item(self, workspace: str, kind: str, id: str) -> dict:
        found = forms.find(self.items(workspace), kind, id)
        if found is None:
            raise NotFound(f"{_KIND_NAMES[kind]} '{id}' not found")
        return found

    def put_item(self, workspace: str, kind: str, id: str, data: dict, author=None, note=None, create=False) -> tuple[dict, list[dict]]:
        """Create or replace one item from its fields. Returns (the item as saved, files written)."""
        ws = _ws(workspace)
        issues = forms.check(kind, id, data)
        if issues:
            raise Invalid(issues)
        with self._locked(ws) as conn:
            head = self._head_rows(conn, ws)
            items = self._items(head)
            current = forms.find(items, kind, id)
            if kind != "settings" and current is not None and create:
                raise Conflict(f"{_KIND_NAMES[kind]} '{id}' already exists")
            if kind == "procedure" and any(b["id"] == id for b in items["bases"]):
                raise Invalid([_name_taken(kind, id, "a shared instruction")])
            if kind == "base" and any(p["id"] == id for p in items["procedures"]):
                raise Invalid([_name_taken(kind, id, "a procedure")])
            current_path = current["file"] if current and current.get("version") else None
            path, text, saved = forms.write(kind, id, data, current_path, self.sopc.export)
            if current_path and forms.same(kind, current, saved):
                return current, []
            changes: dict[str, str | None] = {path: text}
            if current_path and current_path != path:
                changes[current_path] = None
            if forms.CONFIG not in {r["path"] for r in head} and path != forms.CONFIG:
                changes[forms.CONFIG] = "version: 1\n"
            written = self._write(conn, ws, changes, author, note, replace=False)
            return {**saved, "file": path}, written

    def delete_item(self, workspace: str, kind: str, id: str, author=None, note=None) -> list[dict]:
        """Delete an item, and remove it from every list that names it (inherits, applies to, skips, order)."""
        ws = _ws(workspace)
        if kind == "settings":
            raise Conflict("settings can't be deleted")
        with self._locked(ws) as conn:
            head = self._head_rows(conn, ws)
            items = self._items(head)
            target = forms.find(items, kind, id)
            if target is None:
                raise NotFound(f"{_KIND_NAMES[kind]} '{id}' not found")
            names = {id}
            if kind == "agent":
                names.add(f"{target['platform']}:{target['platform_id']}")
            changes: dict[str, str | None] = {target["file"]: None}

            def drop(values: list[str]) -> list[str]:
                return [v for v in values if v not in names]

            edits: list[tuple[str, dict]] = []
            for other_kind, key in (("agent", "agents"), ("base", "bases"), ("procedure", "procedures")):
                for other in items[key]:
                    if other_kind == kind and other["id"] == id:
                        continue
                    new = dict(other)
                    if kind == "agent" and other_kind in ("base", "procedure"):
                        if new["agents"] != "*":
                            new["agents"] = drop(new["agents"])
                        new["exclude"] = drop(new["exclude"])
                    if kind == "base" and other_kind in ("agent", "base"):
                        new["inherits"] = drop(new["inherits"])
                    if kind in ("base", "procedure") and other_kind == "agent":
                        new["exclude"] = drop(new["exclude"])
                    if not forms.same(other_kind, other, new):
                        edits.append((other_kind, new))
            if kind == "procedure" and id in items["settings"]["procedure_order"]:
                edits.append(("settings", {**items["settings"], "procedure_order": drop(items["settings"]["procedure_order"])}))
            for other_kind, new in edits:
                path, text, _ = forms.write(other_kind, new.get("id", "settings"), new, new["file"], self.sopc.export)
                changes[path] = text
                if path != new["file"]:
                    changes[new["file"]] = None
            return self._write(conn, ws, changes, author, note, replace=False)

    def _parse(self, kind: str, id: str, path: str, content: str) -> dict:
        """One item's fields from one file's content, read by sopc on its own."""
        files = {path: content} if kind == "settings" else {forms.CONFIG: "", path: content}
        return forms.find(forms.from_export(self.sopc.export(files)), kind, id)

    def item_history(self, workspace: str, kind: str, id: str) -> list[dict]:
        """Every version of an item, newest first. A procedure moved from Markdown to YAML keeps both histories."""
        ws = _ws(workspace)
        q = (
            select(file_versions)
            .where(file_versions.c.workspace == ws, file_versions.c.path.in_(forms.paths_of(kind, id)))
            .order_by(file_versions.c.created_at.desc(), file_versions.c.version.desc())
        )
        with self.engine.connect() as conn:
            rows = [_file_out(r) for r in conn.execute(q).mappings()]
        if not rows:
            raise NotFound(f"{_KIND_NAMES[kind]} '{id}' not found")
        return [{**{k: v for k, v in r.items() if k != "path"}, "file": r["path"]} for r in rows]

    def item_version(self, workspace: str, kind: str, id: str, version: int, file: str | None = None) -> dict:
        """An older version of an item, as fields."""
        paths = forms.paths_of(kind, id)
        if file is not None and file not in paths:
            raise NotFound(f"'{file}' is not a file of {_KIND_NAMES[kind]} '{id}'")
        for path in [file] if file else paths:
            try:
                row = self.file(workspace, path, version)
            except NotFound:
                continue
            if row["deleted"]:
                return {"id": id, "version": version, "deleted": True, "file": path, "updated_at": row["created_at"], "updated_by": row["author"]}
            found = self._parse(kind, id, path, row["content"])
            return {**found, "file": path, "version": version, "deleted": False, "updated_at": row["created_at"], "updated_by": row["author"]}
        raise NotFound(f"{_KIND_NAMES[kind]} '{id}' has no version {version}")

    # --- releases --------------------------------------------------------------

    def draft(self, workspace: str) -> dict:
        """Head compiled, compared with the current release."""
        ws = _ws(workspace)
        with self.engine.connect() as conn:
            head = self._head_rows(conn, ws)
            current = self._current(conn, ws)
        live_files = current["files"] if current else {}
        live_agents = current["build"]["agents"] if current else {}
        head_versions = {r["path"]: r["version"] for r in head}

        files = []
        for path in sorted(set(head_versions) | set(live_files)):
            new, old = head_versions.get(path), live_files.get(path)
            if new != old:
                change = "added" if old is None else "deleted" if new is None else "edited"
                files.append({"path": path, "change": change, "version": new, "released_version": old})

        compiled = self.sopc.compile({r["path"]: r["content"] for r in head}) if head else None
        names = {}
        head_items = self._items(head) if compiled and compiled.valid else None
        if head_items:
            for key, kind in (("agents", "agent"), ("bases", "base"), ("procedures", "procedure")):
                for it in head_items[key]:
                    names[(kind, it["id"])] = it.get("name") or it["id"]
        items: dict[tuple[str, str], dict] = {}
        for f in files:
            where = forms.item_of_path(f["path"])
            if not where:
                continue
            prev = items.get(where)
            change = f["change"]
            if prev:  # a procedure moved between Markdown and YAML: one edit
                change = "edited" if {prev["change"], change} == {"added", "deleted"} else prev["change"]
            items[where] = {
                "kind": where[0],
                "id": where[1],
                "name": "Settings" if where[0] == "settings" else names.get(where, where[1]),
                "change": change,
                "version": f["version"] if f["version"] is not None else (prev or {}).get("version"),
                "released_version": f["released_version"] if f["released_version"] is not None else (prev or {}).get("released_version"),
            }
        # Each changed item's fields before (as released) and after (head), for a field-by-field summary.
        for (kind, id), item in items.items():
            after = forms.find(head_items, kind, id) if head_items else None
            item["after"] = _fields(kind, after) if after is not None and item["change"] != "deleted" else None
            item["before"] = None
            released = [p for p in forms.paths_of(kind, id) if p in live_files]
            if released:
                row = self.file(ws, released[0], live_files[released[0]])
                if row["content"] is not None:
                    item["before"] = _fields(kind, self._parse(kind, id, released[0], row["content"]))
        draft_agents = compiled.agents if compiled and compiled.valid else {}
        agents = []
        for agent_id in sorted(set(draft_agents) | set(live_agents)):
            new, old = draft_agents.get(agent_id), live_agents.get(agent_id)
            status = "added" if old is None else "removed" if new is None else "unchanged" if new["hash"] == old["hash"] else "changed"
            agents.append(
                {
                    "agent": agent_id,
                    "platform_ref": (new or old)["platform_ref"],
                    "status": status,
                    "hash": new["hash"] if new else None,
                    "released_hash": old["hash"] if old else None,
                    "prompt": new["prompt"] if new else None,
                    "released_prompt": old["prompt"] if old else None,
                    "components": _components(new["blocks"], head_versions) if new else [],
                }
            )
        return {
            "workspace": ws,
            "release": current["number"] if current else None,
            "valid": compiled.valid if compiled else False,
            "issues": [forms.locate(i).to_dict() for i in compiled.issues] if compiled else [],
            "files": files,
            "items": list(items.values()),
            "agents": agents,
        }

    def release(self, workspace: str, author: str | None = None, note: str | None = None) -> tuple[dict, bool]:
        """Release head and make it current. If head is already current, returns that release and False."""
        with self._locked(workspace) as conn:
            return self._release(conn, workspace, author, note)

    def publish(self, workspace: str, files: Files, author: str | None = None, note: str | None = None) -> tuple[dict, bool, list[dict]]:
        """Replace head with `files` and release it, in one transaction."""
        _check_paths(files)
        with self._locked(workspace) as conn:
            changed = self._write(conn, workspace, files, author, note, replace=True)
            release, created = self._release(conn, workspace, author, note)
            return release, created, changed

    def releases(self, workspace: str) -> list[dict]:
        ws = _ws(workspace)
        q = select(releases.c.number, releases.c.created_at, releases.c.author, releases.c.note, releases.c.files)
        with self.engine.connect() as conn:
            current = self._current_number(conn, ws)
            rows = conn.execute(q.where(releases.c.workspace == ws).order_by(releases.c.number.desc())).mappings()
            return [_summary(r, r["number"] == current) for r in rows]

    def get_release(self, workspace: str, number: int | None = None) -> dict:
        """A release with its build; the current one if `number` is None."""
        ws = _ws(workspace)
        with self.engine.connect() as conn:
            current = self._current_number(conn, ws)
            number = number or current
            if number is None:
                raise NotFound(f"workspace '{ws}' has no release")
            row = conn.execute(select(releases).where(releases.c.workspace == ws, releases.c.number == number)).mappings().first()
        if not row:
            raise NotFound(f"release {number} not found")
        return {**_summary(row, row["number"] == current), "build": row["build"]}

    def activate(self, workspace: str, number: int) -> dict:
        """Serve an existing release, e.g. to roll back."""
        ws = _ws(workspace)
        with self.engine.begin() as conn:
            if not conn.execute(select(releases.c.number).where(releases.c.workspace == ws, releases.c.number == number)).first():
                raise NotFound(f"release {number} not found")
            conn.execute(update(workspaces).where(workspaces.c.workspace == ws).values(current_release=number))
        return {"workspace": ws, "current_release": number}

    # --- serving ---------------------------------------------------------------

    def agent(self, workspace: str, agent: str, release: int | None = None) -> dict:
        """The agent's build in a release (default: current). `agent` is the sopc id or platform ref."""
        rel = self.get_release(workspace, release)
        built = rel["build"]["agents"]
        agent_id = agent if agent in built else next((a for a, b in built.items() if b["platform_ref"] == agent), None)
        if agent_id is None:
            raise NotFound(f"agent '{agent}' not found in release {rel['number']}")
        return {"agent": agent_id, "release": rel["number"], **built[agent_id]}

    def log_fetch(self, workspace: str, agent: str, release: int, hash: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(fetches).values(workspace=workspace, agent=agent, release=release, hash=hash, at=_now()))

    def fetches(self, workspace: str, agent: str | None = None, limit: int = 100) -> list[dict]:
        """Newest first."""
        q = select(fetches.c.id, fetches.c.agent, fetches.c.release, fetches.c.hash, fetches.c.at).where(fetches.c.workspace == _ws(workspace))
        if agent:
            q = q.where(fetches.c.agent == agent)
        with self.engine.connect() as conn:
            return [{**r, "at": _utc(r["at"])} for r in conn.execute(q.order_by(fetches.c.id.desc()).limit(limit)).mappings()]

    # --- internals -------------------------------------------------------------

    @contextmanager
    def _locked(self, workspace: str) -> Iterator[Connection]:
        """A transaction holding the workspace's lock, so file versions and release numbers don't race."""
        ws = _ws(workspace)
        try:
            with self.engine.begin() as conn:
                dialect = postgresql if conn.dialect.name == "postgresql" else sqlite
                conn.execute(dialect.insert(workspaces).values(workspace=ws).on_conflict_do_nothing())
                if conn.dialect.name == "postgresql":
                    conn.execute(select(workspaces.c.workspace).where(workspaces.c.workspace == ws).with_for_update())
                yield conn
        except IntegrityError as e:
            raise Conflict("a concurrent write conflicted; retry") from e

    def _write(self, conn: Connection, workspace: str, changes: dict[str, str | None], author, note, replace: bool) -> list[dict]:
        head = {r["path"]: r for r in self._head_rows(conn, workspace)}
        current = {p: r["content"] for p, r in head.items()}
        target = {p: c for p, c in (changes.items() if replace else {**current, **changes}.items()) if c is not None}
        if target == current:
            return []
        compiled = self.sopc.compile(target)
        if not compiled.valid:
            raise Invalid(compiled.issues)
        now, written = _now(), []
        for path in sorted(set(target) | set(current)):
            new = target.get(path)
            if new == current.get(path):
                continue
            version = self._max_version(conn, workspace, path) + 1
            conn.execute(
                insert(file_versions).values(workspace=workspace, path=path, version=version, content=new, created_at=now, author=author, note=note)
            )
            written.append({"path": path, "version": version, "deleted": new is None})
        return written

    def _release(self, conn: Connection, workspace: str, author, note) -> tuple[dict, bool]:
        head = self._head_rows(conn, workspace)
        if not head:
            raise Invalid([Issue("missing_config", "the workspace has no files", "", "error")])
        versions = {r["path"]: r["version"] for r in head}
        current = self._current(conn, workspace)
        if current and current["files"] == versions:
            return _summary(current, True), False
        compiled = self.sopc.compile({r["path"]: r["content"] for r in head})
        if not compiled.valid:
            raise Invalid(compiled.issues)
        build = {
            "agents": {
                agent_id: {
                    "platform_ref": a["platform_ref"],
                    "hash": a["hash"],
                    "prompt": a["prompt"],
                    "tools": a["tools"],
                    "sops": a["sops"],
                    "components": _components(a["blocks"], versions),
                }
                for agent_id, a in compiled.agents.items()
            }
        }
        number = (conn.execute(select(func.max(releases.c.number)).where(releases.c.workspace == workspace)).scalar() or 0) + 1
        row = {"workspace": workspace, "number": number, "created_at": _now(), "author": author, "note": note, "files": versions, "build": build}
        conn.execute(insert(releases).values(**row))
        conn.execute(update(workspaces).where(workspaces.c.workspace == workspace).values(current_release=number))
        return _summary(row, True), True

    def _head_rows(self, conn: Connection, workspace: str) -> list[dict]:
        latest = (
            select(file_versions.c.path, func.max(file_versions.c.version).label("version"))
            .where(file_versions.c.workspace == workspace)
            .group_by(file_versions.c.path)
            .subquery()
        )
        q = (
            select(file_versions)
            .join(latest, and_(file_versions.c.path == latest.c.path, file_versions.c.version == latest.c.version))
            .where(file_versions.c.workspace == workspace, file_versions.c.content.is_not(None))
            .order_by(file_versions.c.path)
        )
        return [dict(r) for r in conn.execute(q).mappings()]

    def _max_version(self, conn: Connection, workspace: str, path: str) -> int:
        q = select(func.max(file_versions.c.version)).where(file_versions.c.workspace == workspace, file_versions.c.path == path)
        return conn.execute(q).scalar() or 0

    def _current_number(self, conn: Connection, workspace: str) -> int | None:
        return conn.execute(select(workspaces.c.current_release).where(workspaces.c.workspace == workspace)).scalar()

    def _current(self, conn: Connection, workspace: str) -> dict | None:
        number = self._current_number(conn, workspace)
        if number is None:
            return None
        return dict(conn.execute(select(releases).where(releases.c.workspace == workspace, releases.c.number == number)).mappings().one())


def _components(blocks: list[dict], versions: dict[str, int]) -> list[dict]:
    """Map lock.json blocks to the file versions they came from, plus sopc.yaml (defaults, headings)."""
    out = []
    for block in blocks:
        folder = _KIND_DIRS[block["kind"]]
        path = next((p for p in versions if p.startswith(folder + "/") and p.count("/") == 1 and PurePosixPath(p).stem == block["id"]), None)
        out.append({"kind": block["kind"], "id": block["id"], "path": path, "version": versions.get(path)})
    if CONFIG in versions:
        out.append({"kind": "config", "id": "sopc", "path": CONFIG, "version": versions[CONFIG]})
    return out


_KIND_NAMES = {"agent": "agent", "base": "shared instruction", "procedure": "procedure", "settings": "settings"}


def _name_taken(kind: str, id: str, other: str) -> Issue:
    text = f"'{id}' is already the name of {other}; pick another name."
    return Issue("duplicate_id", text, "", "error", kind=kind, id=id, field="id", text=text)


def _fields(kind: str, item: dict) -> dict:
    """Just what a form edits (no versions or file paths)."""
    return {"id": item.get("id", "settings"), **{f: item[f] for f in forms.FIELDS[kind]}}


def _check_paths(files: dict) -> None:
    bad = path_issues(files)
    if bad:
        raise Invalid(bad)


def _summary(row, current: bool) -> dict:
    return {
        "number": row["number"],
        "created_at": _utc(row["created_at"]),
        "author": row["author"],
        "note": row["note"],
        "files": row["files"],
        "current": current,
    }


def _file_out(row, content: bool = False) -> dict:
    out = {
        "path": row["path"],
        "version": row["version"],
        "deleted": row["content"] is None,
        "created_at": _utc(row["created_at"]),
        "author": row["author"],
        "note": row["note"],
    }
    if content:
        out["content"] = row["content"]
    return out


def _ws(workspace: str) -> str:
    if not _SAFE_ID.match(workspace):
        raise NotFound(f"invalid workspace id '{workspace}'")
    return workspace


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(at: datetime) -> datetime:
    """SQLite drops the timezone; everything is stored in UTC."""
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)
