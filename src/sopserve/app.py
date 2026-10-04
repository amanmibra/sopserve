"""sopserve: the HTTP API for sopkit workspaces. Its OpenAPI spec (/openapi.json) is what SDKs are generated from.

Run with `sopserve`. Set SOPSERVE_TOKEN to require `Authorization: Bearer <token>`.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from sopkit import analyze
from sopkit.issues import Issue, SopkitError
from sopkit.loader import load_workspace_files
from sopkit.plan import make_plan, snapshot
from sopkit.render import Build, render_workspace
from sopkit.validate import validate

from .store import FileStore, NotFound

Files = dict[str, str]


class FilesRequest(BaseModel):
    files: Files = Field(description="Source files keyed by path relative to the sopkit root, e.g. 'bases/brand-voice.md'.")


class PlanRequest(BaseModel):
    base: Files | None = Field(None, description="Files before the change (e.g. main). Omit for a new workspace.")
    head: Files = Field(description="Files after the change (e.g. the PR branch).")


class IssueOut(BaseModel):
    code: str
    message: str
    path: str
    severity: str


class ValidateResponse(BaseModel):
    valid: bool
    issues: list[IssueOut]


class RenderedAgentOut(BaseModel):
    platform_ref: str
    prompt: str
    hash: str
    tools: list[str]


class RenderResponse(BaseModel):
    agents: dict[str, RenderedAgentOut]
    lock: dict
    warnings: list[IssueOut]


class PlanResponse(BaseModel):
    changes: list[dict]
    by_block: list[dict]
    markdown: str


class PublishResponse(BaseModel):
    build_id: str
    agents: list[str]


def create_app(store: FileStore, token: str | None = None) -> FastAPI:
    app = FastAPI(title="sopserve", version="0.0.1", description="Serves sopkit-built agent prompts at call start, and validates, renders, plans and checks sopkit workspaces over HTTP.")

    def auth(authorization: str | None = Header(None)) -> None:
        if token and not (authorization and secrets.compare_digest(authorization, f"Bearer {token}")):
            raise HTTPException(401, "missing or invalid bearer token")

    @app.exception_handler(SopkitError)
    def sopkit_error(_, exc: SopkitError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"valid": False, "issues": [_issue(i) for i in exc.issues]})

    @app.exception_handler(NotFound)
    def not_found(_, exc: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/v1/validate", response_model=ValidateResponse, dependencies=[Depends(auth)])
    def validate_files(req: FilesRequest) -> dict:
        try:
            issues = validate(load_workspace_files(req.files))
        except SopkitError as e:
            issues = e.issues
        return {"valid": not any(i.severity == "error" for i in issues), "issues": [_issue(i) for i in issues]}

    @app.post("/v1/render", response_model=RenderResponse, dependencies=[Depends(auth)])
    def render(req: FilesRequest) -> dict:
        build = _render(req.files)
        return {
            "agents": {
                agent_id: {"platform_ref": r.agent.platform_ref, "prompt": r.prompt, "hash": r.hash, "tools": r.tools}
                for agent_id, r in build.agents.items()
            },
            "lock": build.lock(),
            "warnings": [_issue(i) for i in build.warnings],
        }

    @app.post("/v1/plan", response_model=PlanResponse, dependencies=[Depends(auth)])
    def plan(req: PlanRequest) -> dict:
        before = snapshot(_render(req.base)) if req.base else {}
        result = make_plan(before, snapshot(_render(req.head)))
        return {**result.to_dict(), "markdown": result.markdown()}

    @app.post("/v1/check", dependencies=[Depends(auth)])
    def check(req: FilesRequest) -> list[dict]:
        """Duplicated text and mechanical conflicts (numbers, always/never) in each agent's prompt. Advisory."""
        return [f.to_dict() for f in analyze.check(load_workspace_files(req.files))]

    @app.post("/v1/workspaces/{workspace}/publish", response_model=PublishResponse, dependencies=[Depends(auth)])
    def publish(workspace: str, req: FilesRequest) -> dict:
        build = _render(req.files)
        return {"build_id": store.publish(workspace, build), "agents": sorted(build.agents)}

    @app.get(
        "/v1/workspaces/{workspace}/agents/{agent}/prompt",
        response_class=PlainTextResponse,
        dependencies=[Depends(auth)],
        responses={200: {"content": {"text/plain": {}}, "description": "The agent's full rendered prompt."}},
    )
    def prompt(workspace: str, agent: str, response: Response) -> str:
        """The full prompt to give the agent at call start. `agent` is the alias or platform ref."""
        served = store.prompt(workspace, agent)
        store.log_fetch(workspace, served)
        response.headers["X-Sopkit-Hash"] = served.hash
        response.headers["X-Sopkit-Build"] = served.build_id
        return served.prompt

    @app.get("/v1/workspaces/{workspace}/agents/{agent}/sops/{sop_id}", dependencies=[Depends(auth)])
    def get_sop(workspace: str, agent: str, sop_id: str) -> dict:
        """What the `get_sop` tool returns for SOPs with `delivery: auto | tool`."""
        return store.sop(workspace, agent, sop_id)

    @app.get("/v1/workspaces/{workspace}/fetches", dependencies=[Depends(auth)])
    def fetches(workspace: str, agent: str | None = None) -> list[dict]:
        """Which prompt version each agent was served, and when. Used to tie calls to versions."""
        return store.fetches(workspace, agent)

    return app


def _render(files: Files) -> Build:
    return render_workspace(load_workspace_files(files))


def _issue(i: Issue) -> dict:
    return {"code": i.code, "message": i.message, "path": i.path, "severity": i.severity}


def app_from_env() -> FastAPI:
    return create_app(FileStore(Path(os.environ.get("SOPSERVE_DATA_DIR", ".sopserve-data"))), os.environ.get("SOPSERVE_TOKEN"))
