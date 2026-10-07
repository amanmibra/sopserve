"""sopserve: versioned sopc workspaces over HTTP, and the prompt each agent gets at call start.

Its OpenAPI spec (/openapi.json) is what SDKs are generated from. Set SOPSERVE_TOKEN to require
`Authorization: Bearer <token>`, DATABASE_URL for the database (default sqlite:///sopserve.db),
and SOPC_BIN for the sopc binary (default: `sopc` on PATH).
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__
from .sopc import Invalid, Sopc, SopcUnavailable, without_build
from .store import Conflict, NotFound, Store

Files = dict[str, str]
STATIC = Path(__file__).parent / "static"


# --- models -------------------------------------------------------------------


class FilesRequest(BaseModel):
    files: Files = Field(description="Source files keyed by path relative to the sopc folder, e.g. 'bases/brand-voice.md'. Paths under build/ are ignored.")


class IssueOut(BaseModel):
    code: str
    message: str
    path: str = Field(description="Relative to the sopc folder; empty for folder-wide issues.")
    severity: Literal["error", "warning"]


class ValidateResponse(BaseModel):
    valid: bool
    issues: list[IssueOut]


class RenderedAgent(BaseModel):
    platform_ref: str
    hash: str
    prompt: str
    tools: list[str]
    sops: dict[str, dict[str, Any]] = Field(description="Tool-delivered SOPs (`delivery: auto | tool`) keyed by SOP id: what `get_sop` returns.")


class RenderResponse(BaseModel):
    agents: dict[str, RenderedAgent]
    lock: dict[str, Any] = Field(description="sopc's build/lock.json.")
    warnings: list[IssueOut]


class LintSource(BaseModel):
    block: str
    text: str


class LintFinding(BaseModel):
    code: str
    message: str
    sources: list[LintSource]
    agents: list[str]


class LintResponse(BaseModel):
    findings: list[LintFinding]


class WorkspaceOut(BaseModel):
    workspace: str
    current_release: int | None


class FileOut(BaseModel):
    path: str
    version: int
    deleted: bool
    created_at: datetime
    author: str | None
    note: str | None
    content: str | None = Field(None, description="Only when requested (`?content=true`, or a single file).")


class FilesResponse(BaseModel):
    workspace: str
    files: list[FileOut]


class EditRequest(BaseModel):
    changes: dict[str, str | None] = Field(description="{path: new content}, or null to delete the file.")
    author: str | None = None
    note: str | None = None


class ReplaceRequest(FilesRequest):
    author: str | None = None
    note: str | None = None


class WrittenFile(BaseModel):
    path: str
    version: int
    deleted: bool


class WriteResponse(BaseModel):
    workspace: str
    changed: list[WrittenFile] = Field(description="New file versions. Empty if nothing changed.")


class Component(BaseModel):
    kind: Literal["agent", "base", "sop", "config"]
    id: str
    path: str | None
    version: int | None


class FileChange(BaseModel):
    path: str
    change: Literal["added", "edited", "deleted"]
    version: int | None = Field(description="Head version; null if deleted.")
    released_version: int | None = Field(description="Version in the current release; null if added.")


class DraftAgent(BaseModel):
    agent: str
    platform_ref: str
    status: Literal["added", "changed", "removed", "unchanged"]
    hash: str | None
    released_hash: str | None
    prompt: str | None = Field(description="The draft prompt; null if removed.")
    components: list[Component]


class DraftResponse(BaseModel):
    workspace: str
    release: int | None = Field(description="The current release the draft is compared with.")
    valid: bool
    issues: list[IssueOut]
    files: list[FileChange] = Field(description="Files changed since the current release.")
    agents: list[DraftAgent]


class ReleaseRequest(BaseModel):
    author: str | None = None
    note: str | None = None


class ReleaseSummary(BaseModel):
    number: int
    created_at: datetime
    author: str | None
    note: str | None
    files: dict[str, int] = Field(description="{path: version} snapshot.")
    current: bool


class ReleaseResponse(BaseModel):
    release: ReleaseSummary
    created: bool = Field(description="False if head was already the current release.")


class PublishRequest(ReplaceRequest):
    pass


class PublishResponse(ReleaseResponse):
    changed: list[WrittenFile]


class ReleasedAgent(BaseModel):
    platform_ref: str
    hash: str
    prompt: str
    tools: list[str]
    tool_sops: list[str]
    components: list[Component]


class ReleaseDetail(ReleaseSummary):
    agents: dict[str, ReleasedAgent]


class AgentSummary(BaseModel):
    agent: str
    platform_ref: str
    hash: str


class AgentsResponse(BaseModel):
    workspace: str
    release: int
    agents: list[AgentSummary]


class AgentOut(BaseModel):
    agent: str = Field(description="sopc agent id.")
    platform_ref: str
    release: int
    hash: str
    prompt: str
    tools: list[str]
    tool_sops: list[str] = Field(description="Ids of SOPs to fetch with `get_sop` (`/sops/{sop_id}`).")
    components: list[Component] = Field(description="Each file the prompt was built from, at its version. Put this and `release` in call metadata.")


class FetchOut(BaseModel):
    id: int
    agent: str
    release: int
    hash: str
    at: datetime


# --- app ----------------------------------------------------------------------


def create_app(store: Store, token: str | None = None) -> FastAPI:
    app = FastAPI(
        title="sopserve",
        version=__version__,
        description="Versioned sopc workspaces: edit files, release them, and serve each agent its compiled prompt at call start.",
    )
    invalid = {422: {"model": ValidateResponse, "description": "The files don't compile; sopc's issues."}}

    def auth(authorization: str | None = Header(None)) -> None:
        if token and not (authorization and secrets.compare_digest(authorization, f"Bearer {token}")):
            raise HTTPException(401, "missing or invalid bearer token")

    @app.exception_handler(Invalid)
    def _invalid(_, exc: Invalid) -> JSONResponse:
        return JSONResponse(status_code=422, content={"valid": False, "issues": [i.to_dict() for i in exc.issues]})

    @app.exception_handler(NotFound)
    def _not_found(_, exc: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(Conflict)
    def _conflict(_, exc: Conflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(SopcUnavailable)
    def _sopc_unavailable(_, exc: SopcUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.get("/", include_in_schema=False)
    def ui() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True}

    v1 = [Depends(auth)]

    # stateless

    @app.post("/v1/validate", response_model=ValidateResponse, dependencies=v1, tags=["stateless"])
    def validate(req: FilesRequest) -> dict:
        issues = store.sopc.validate(without_build(req.files))
        return {"valid": not any(i.severity == "error" for i in issues), "issues": [i.to_dict() for i in issues]}

    @app.post("/v1/render", response_model=RenderResponse, responses=invalid, dependencies=v1, tags=["stateless"])
    def render(req: FilesRequest) -> dict:
        compiled = store.sopc.compile(without_build(req.files))
        if not compiled.valid:
            raise Invalid(compiled.issues)
        return {
            "agents": {a: {k: v for k, v in b.items() if k != "blocks"} for a, b in compiled.agents.items()},
            "lock": compiled.lock,
            "warnings": [i.to_dict() for i in compiled.issues],
        }

    @app.post("/v1/lint", response_model=LintResponse, responses=invalid, dependencies=v1, tags=["stateless"])
    def lint(req: FilesRequest) -> dict:
        """Duplicated and conflicting instructions in each agent's prompt. Advisory."""
        return {"findings": store.sopc.lint(without_build(req.files))}

    # files

    @app.get("/v1/workspaces", response_model=list[WorkspaceOut], dependencies=v1, tags=["workspaces"])
    def list_workspaces() -> list[dict]:
        return store.workspaces()

    @app.get("/v1/workspaces/{workspace}/files", response_model=FilesResponse, dependencies=v1, tags=["files"])
    def list_files(workspace: str, content: bool = Query(False, description="Include each file's content, e.g. to export to git.")) -> dict:
        """Head: the latest version of every file."""
        return {"workspace": workspace, "files": store.head(workspace, content)}

    @app.post("/v1/workspaces/{workspace}/files", response_model=WriteResponse, responses=invalid, dependencies=v1, tags=["files"])
    def edit_files(workspace: str, req: EditRequest) -> dict:
        """Change some files atomically. Rejected if the result doesn't compile. Nothing goes live until a release."""
        return {"workspace": workspace, "changed": store.edit(workspace, req.changes, req.author, req.note)}

    @app.put("/v1/workspaces/{workspace}/files", response_model=WriteResponse, responses=invalid, dependencies=v1, tags=["files"])
    def replace_files(workspace: str, req: ReplaceRequest) -> dict:
        """Make head exactly these files (e.g. synced from git); files not listed are deleted."""
        return {"workspace": workspace, "changed": store.replace(workspace, without_build(req.files), req.author, req.note)}

    @app.get("/v1/workspaces/{workspace}/files/{path:path}", response_model=FileOut, dependencies=v1, tags=["files"])
    def get_file(workspace: str, path: str, version: int | None = None) -> dict:
        """A file at head, or at `version`."""
        return store.file(workspace, path, version)

    @app.get("/v1/workspaces/{workspace}/history/{path:path}", response_model=list[FileOut], dependencies=v1, tags=["files"])
    def file_history(workspace: str, path: str) -> list[dict]:
        """Every version of a file, newest first, without content."""
        return store.history(workspace, path)

    # releases

    @app.get("/v1/workspaces/{workspace}/draft", response_model=DraftResponse, dependencies=v1, tags=["releases"])
    def draft(workspace: str) -> dict:
        """Unpublished changes: head compiled and compared with the current release."""
        return store.draft(workspace)

    @app.post("/v1/workspaces/{workspace}/releases", response_model=ReleaseResponse, responses=invalid, dependencies=v1, tags=["releases"])
    def create_release(workspace: str, req: ReleaseRequest | None = None) -> dict:
        """Release head and serve it."""
        req = req or ReleaseRequest()
        release, created = store.release(workspace, req.author, req.note)
        return {"release": release, "created": created}

    @app.get("/v1/workspaces/{workspace}/releases", response_model=list[ReleaseSummary], dependencies=v1, tags=["releases"])
    def list_releases(workspace: str) -> list[dict]:
        return store.releases(workspace)

    @app.get("/v1/workspaces/{workspace}/releases/{number}", response_model=ReleaseDetail, dependencies=v1, tags=["releases"])
    def get_release(workspace: str, number: int) -> dict:
        """A release with every agent's prompt. Doesn't log a fetch."""
        rel = store.get_release(workspace, number)
        return {**rel, "agents": {a: _released(b) for a, b in rel["build"]["agents"].items()}}

    @app.post("/v1/workspaces/{workspace}/releases/{number}/activate", response_model=WorkspaceOut, dependencies=v1, tags=["releases"])
    def activate(workspace: str, number: int) -> dict:
        """Serve this release, e.g. to roll back."""
        return store.activate(workspace, number)

    @app.post("/v1/workspaces/{workspace}/publish", response_model=PublishResponse, responses=invalid, dependencies=v1, tags=["releases"])
    def publish(workspace: str, req: PublishRequest) -> dict:
        """Replace head with these files and release it: the CI path for git workspaces."""
        release, created, changed = store.publish(workspace, without_build(req.files), req.author, req.note)
        return {"release": release, "created": created, "changed": changed}

    # serving

    @app.get("/v1/workspaces/{workspace}/agents", response_model=AgentsResponse, dependencies=v1, tags=["serving"])
    def list_agents(workspace: str, release: int | None = None) -> dict:
        rel = store.get_release(workspace, release)
        agents = [{"agent": a, "platform_ref": b["platform_ref"], "hash": b["hash"]} for a, b in sorted(rel["build"]["agents"].items())]
        return {"workspace": workspace, "release": rel["number"], "agents": agents}

    @app.get("/v1/workspaces/{workspace}/agents/{agent}", response_model=AgentOut, dependencies=v1, tags=["serving"])
    def get_agent(workspace: str, agent: str, release: int | None = Query(None, description="Pin a release (e.g. for A/B); default: current.")) -> dict:
        """What to load at call start. `agent` is the sopc id or platform ref (`livekit:tonys-pizza`). Logs a fetch."""
        served = store.agent(workspace, agent, release)
        store.log_fetch(workspace, served["agent"], served["release"], served["hash"])
        return {"agent": served["agent"], "release": served["release"], **_released(served)}

    @app.get(
        "/v1/workspaces/{workspace}/agents/{agent}/prompt",
        response_class=PlainTextResponse,
        dependencies=v1,
        tags=["serving"],
        responses={200: {"content": {"text/plain": {}}, "description": "The full prompt, with X-Sopc-Release and X-Sopc-Hash headers."}},
    )
    def get_prompt(workspace: str, agent: str, response: Response, release: int | None = None) -> str:
        """Just the prompt text. Logs a fetch."""
        served = store.agent(workspace, agent, release)
        store.log_fetch(workspace, served["agent"], served["release"], served["hash"])
        response.headers["X-Sopc-Release"] = str(served["release"])
        response.headers["X-Sopc-Hash"] = served["hash"]
        return served["prompt"]

    @app.get("/v1/workspaces/{workspace}/agents/{agent}/sops/{sop_id}", response_model=dict[str, Any], dependencies=v1, tags=["serving"])
    def get_sop(workspace: str, agent: str, sop_id: str, release: int | None = None) -> dict:
        """What the `get_sop` tool returns for SOPs with `delivery: auto | tool`."""
        served = store.agent(workspace, agent, release)
        if sop_id not in served["sops"]:
            raise NotFound(f"SOP '{sop_id}' is not tool-delivered to '{served['agent']}'")
        return served["sops"][sop_id]

    @app.get("/v1/workspaces/{workspace}/fetches", response_model=list[FetchOut], dependencies=v1, tags=["serving"])
    def list_fetches(workspace: str, agent: str | None = Query(None, description="sopc agent id."), limit: int = Query(100, ge=1, le=1000)) -> list[dict]:
        """Which release and prompt hash each agent was served, newest first."""
        return store.fetches(workspace, agent, limit)

    return app


def _released(b: dict) -> dict:
    return {
        "platform_ref": b["platform_ref"],
        "hash": b["hash"],
        "prompt": b["prompt"],
        "tools": b["tools"],
        "tool_sops": sorted(b["sops"]),
        "components": b["components"],
    }


def app_from_env() -> FastAPI:
    return create_app(Store(os.environ.get("DATABASE_URL", "sqlite:///sopserve.db"), Sopc()), os.environ.get("SOPSERVE_TOKEN"))

