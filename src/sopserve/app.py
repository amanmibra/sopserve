"""sopserve: versioned sopc workspaces over HTTP, and the prompt each agent gets at call start.

Its OpenAPI spec (/openapi.json) is what SDKs are generated from. Set DATABASE_URL for the database
(default sqlite:///sopserve.db) and SOPC_BIN for the sopc binary (default: `sopc` on PATH).
There is no authentication yet: run it on a private network.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Query, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, create_model

from . import __version__
from . import forms
from .sopc import Invalid, Sopc, SopcUnavailable, without_build
from .store import Conflict, NotFound, Store

Files = dict[str, str]
STATIC = Path(__file__).parent / "static"


# --- models -------------------------------------------------------------------


class FilesRequest(BaseModel):
    files: Files = Field(description="Source files keyed by path relative to the sopc folder, e.g. 'instructions/brand-voice.md'. Paths under build/ are ignored.")


class IssueOut(BaseModel):
    code: str
    message: str = Field(description="sopc's message.")
    path: str = Field(description="Relative to the sopc folder; empty for folder-wide issues.")
    severity: Literal["error", "warning"]
    kind: Literal["agent", "instruction", "procedure", "group", "settings"] | None = Field(None, description="The item the issue is about, if known.")
    id: str | None = None
    field: str | None = Field(None, description="The item's field, as the /config endpoints name it (e.g. `steps`, `blocks`).")
    index: int | None = Field(None, description="For list fields (steps, never, warning_signs, blocks): which entry.")
    text: str | None = Field(None, description="The issue in plain words, for showing next to the field.")


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
    kind: Literal["agent", "instruction", "sop", "config", "base"] = Field(description="`base` only in releases made before sopc v0.0.9.")
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
    released_prompt: str | None = Field(description="The prompt in the current release; null if added.")
    components: list[Component]


class DraftResponse(BaseModel):
    workspace: str
    release: int | None = Field(description="The current release the draft is compared with.")
    valid: bool
    issues: list[IssueOut]
    files: list[FileChange] = Field(description="Files changed since the current release.")
    items: list[DraftItem] = Field(description="The same changes as agents, shared instructions, procedures, groups and settings.")
    agents: list[DraftAgent]
    lint: list[LintFinding] = Field([], description="`sopc lint` on the draft: duplicated or conflicting instructions. Advisory; publishing isn't blocked.")


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


# --- items (the /config endpoints) ----------------------------------------------------

class Step(BaseModel):
    text: str
    tool: str | None = Field(None, description="Exact name of a tool the agent has.")
    required: bool = Field(False, description="Steps only: the tool call must happen.")


class Saved(BaseModel):
    author: str | None = Field(None, description="Recorded with the new version.")
    note: str | None = None


class ItemMeta(BaseModel):
    version: int | None = Field(None, description="Version of the item's file at head.")
    updated_at: datetime | None = None
    updated_by: str | None = None
    file: str = Field(description="Where it lives in the sopc folder (for git export).")


class AgentFields(BaseModel):
    platform: Literal["livekit", "vapi", "elevenlabs", "retell"]
    platform_id: str = Field(description="The agent's id on its platform, e.g. LiveKit agent_name.")
    context: str = Field("", description="Text only this agent gets. It goes first in the prompt.")
    blocks: list[str] = Field([], description="Shared instructions, procedures and groups, in prompt order.")
    variables: dict[str, str] = Field({}, description="Values for {{placeholders}}; override the defaults in settings.")


class InstructionFields(BaseModel):
    text: str


class ProcedureFields(BaseModel):
    name: str
    goal: str = ""
    when: str = ""
    guidance: str = ""
    steps: list[Step] = Field(description="In order; at least one.")
    never: list[Step] = []
    warning_signs: list[Step] = []
    delivery: Literal["prompt", "auto", "tool"] = "prompt"


class GroupFields(BaseModel):
    blocks: list[str] = Field(description="Shared instructions, procedures and other groups, in order.")


class SettingsFields(BaseModel):
    variables: dict[str, str] = Field({}, description="Default values for {{placeholders}}, for every agent.")
    procedures_heading: str = Field(forms.DEFAULT_HEADING, description="Heading before the first procedure in each prompt; empty for none.")


class AgentIn(AgentFields, Saved):
    pass


class InstructionIn(InstructionFields, Saved):
    pass


class ProcedureIn(ProcedureFields, Saved):
    pass


class GroupIn(GroupFields, Saved):
    pass


class SettingsIn(SettingsFields, Saved):
    pass


class NewAgent(AgentIn):
    id: str


class NewInstruction(InstructionIn):
    id: str


class NewProcedure(ProcedureIn):
    id: str


class NewGroup(GroupIn):
    id: str


class Use(BaseModel):
    agent: str
    via: str | None = Field(description="The group the agent lists it through; null if the agent lists it directly.")


class UsedBy(BaseModel):
    used_by: list[Use] = Field([], description="Which agents use it.")


class AgentConfig(AgentFields, ItemMeta):
    id: str


class InstructionConfig(InstructionFields, UsedBy, ItemMeta):
    id: str


class ProcedureConfig(ProcedureFields, UsedBy, ItemMeta):
    id: str
    format: Literal["markdown", "yaml"] = Field(description="How the file is written. New procedures are Markdown.")


class GroupConfig(GroupFields, UsedBy, ItemMeta):
    id: str


class SettingsConfig(SettingsFields, ItemMeta):
    pass


class ConfigResponse(BaseModel):
    workspace: str
    settings: SettingsConfig
    agents: list[AgentConfig]
    instructions: list[InstructionConfig]
    procedures: list[ProcedureConfig]
    groups: list[GroupConfig] = Field(description="In the order sopc.yaml lists them.")


class BlockRequest(Saved):
    block: str = Field(description="A shared instruction, procedure or group id.")


class AgentSaved(BaseModel):
    item: AgentConfig
    changed: list[WrittenFile]


class PreviewBlock(BaseModel):
    kind: Literal["agent", "instruction", "sop"]
    id: str


class PreviewResponse(BaseModel):
    valid: bool
    issues: list[IssueOut]
    prompt: str | None = Field(description="The prompt the agent would get with these fields; null if it doesn't compile.")
    hash: str | None
    blocks: list[PreviewBlock] = Field(description="What the prompt is built from, in order, groups expanded.")
    release: int | None
    released_prompt: str | None = Field(description="The agent's prompt in the current release, to compare with.")


class MigrateRequest(Saved):
    apply: bool = Field(False, description="Write the converted files as a draft. Without it, only the plan is returned.")


class MigrateResponse(BaseModel):
    workspace: str
    needed: bool = Field(description="False if head is already in the current format.")
    plan: str = Field(description="sopc migrate's plan: what changes in each file, and any warnings.")
    applied: bool
    changed: list[WrittenFile]


class MigrateFilesResponse(BaseModel):
    migrated: bool = Field(description="False if the files were already in the current format.")
    plan: str
    files: Files = Field(description="The converted files (the input if nothing changed).")


class ItemVersion(BaseModel):
    version: int
    deleted: bool
    created_at: datetime
    author: str | None
    note: str | None
    file: str


class DraftItem(BaseModel):
    kind: Literal["agent", "instruction", "procedure", "group", "settings"]
    id: str
    name: str
    change: Literal["added", "edited", "deleted"]
    version: int | None
    released_version: int | None
    before: dict[str, Any] | None = Field(description="Its fields in the current release; null if added.")
    after: dict[str, Any] | None = Field(description="Its fields in the draft; null if deleted.")


# --- app ----------------------------------------------------------------------


def create_app(store: Store) -> FastAPI:
    app = FastAPI(
        title="sopserve",
        version=__version__,
        description="Versioned sopc workspaces: edit files, release them, and serve each agent its compiled prompt at call start.",
    )
    app.state.store = store
    invalid = {422: {"model": ValidateResponse, "description": "The files don't compile; sopc's issues."}}

    @app.exception_handler(Invalid)
    def _invalid(_, exc: Invalid) -> JSONResponse:
        return JSONResponse(status_code=422, content={"valid": False, "issues": [forms.locate(i).to_dict() for i in exc.issues]})

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
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True}

    v1: list = []  # no authentication yet (v0); keep sopserve on a private network

    # stateless

    @app.post("/v1/validate", response_model=ValidateResponse, dependencies=v1, tags=["stateless"])
    def validate(req: FilesRequest) -> dict:
        issues = store.sopc.validate(without_build(req.files))
        return {"valid": not any(i.severity == "error" for i in issues), "issues": [forms.locate(i).to_dict() for i in issues]}

    @app.post("/v1/render", response_model=RenderResponse, responses=invalid, dependencies=v1, tags=["stateless"])
    def render(req: FilesRequest) -> dict:
        compiled = store.sopc.compile(without_build(req.files))
        if not compiled.valid:
            raise Invalid(compiled.issues)
        return {
            "agents": {a: {k: v for k, v in b.items() if k != "blocks"} for a, b in compiled.agents.items()},
            "lock": compiled.lock,
            "warnings": [forms.locate(i).to_dict() for i in compiled.issues],
        }

    @app.post("/v1/migrate", response_model=MigrateFilesResponse, responses=invalid, dependencies=v1, tags=["stateless"])
    def migrate_files(req: FilesRequest) -> dict:
        """Convert a folder in an older sopc format with `sopc migrate`: the v0.0.8 format (bases/, agent targeting),
        or `locked` (v0.0.9), which sopc no longer has. Nothing stored."""
        return store.sopc.migrate(without_build(req.files))

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

    # items: the structured view the UI's forms edit

    @app.get("/v1/workspaces/{workspace}/config", response_model=ConfigResponse, dependencies=v1, tags=["config"])
    def get_config(workspace: str) -> dict:
        """Head as structured items: settings, agents, shared instructions, procedures (SOPs) and groups.
        422 with `old_format` or `removed_field` issues if the workspace is in an older sopc format: see `…/migrate`."""
        return {"workspace": workspace, **store.items(workspace)}

    def item_routes(kind: str, plural: str, In: type[BaseModel], New: type[BaseModel], Out: type[BaseModel]) -> None:
        """GET/POST {plural}, GET/PUT/DELETE {plural}/{id}, and its history and old versions."""
        path = f"/v1/workspaces/{{workspace}}/config/{plural}"
        what = {"agent": "agent", "instruction": "shared instruction", "procedure": "procedure (SOP)", "group": "group (in sopc.yaml)"}[kind]
        Result = create_model(f"{Out.__name__}Saved", item=(Out, ...), changed=(list[WrittenFile], ...))

        def save(workspace: str, id: str, req: BaseModel, create: bool) -> dict:
            data = req.model_dump(exclude={"author", "note", "id"})
            item, changed = store.put_item(workspace, kind, id, data, req.author, req.note, create=create)
            return {"item": item, "changed": changed}

        def list_items(workspace: str) -> list[dict]:
            return store.items(workspace)[plural]

        def create(workspace: str, req) -> dict:
            return save(workspace, req.id, req, create=True)

        def get(workspace: str, id: str) -> dict:
            return store.item(workspace, kind, id)

        def put(workspace: str, id: str, req) -> dict:
            return save(workspace, id, req, create=False)

        def delete(workspace: str, id: str, author: str | None = None, note: str | None = None) -> dict:
            return {"workspace": workspace, "changed": store.delete_item(workspace, kind, id, author, note)}

        def history(workspace: str, id: str) -> list[dict]:
            return store.item_history(workspace, kind, id)

        def version(workspace: str, id: str, version: int, file: str | None = Query(None, description="From history; for a procedure that changed format.")) -> dict:
            return store.item_version(workspace, kind, id, version, file)

        create.__annotations__ = {"workspace": str, "req": New, "return": dict}
        put.__annotations__ = {"workspace": str, "id": str, "req": In, "return": dict}
        routes = [
            ("GET", "", list_items, list[Out], f"Every {what} at head.", {}),
            ("POST", "", create, Result, f"Create a {what}. Rejected if the result doesn't compile; 409 if it exists.", {**invalid, 409: {"description": "Already exists."}}),
            ("GET", "/{id}", get, Out, f"One {what} at head.", {}),
            ("PUT", "/{id}", put, Result, f"Create or replace a {what}. Rejected if the result doesn't compile. Nothing goes live until a release.", invalid),
            ("DELETE", "/{id}", delete, WriteResponse, f"Delete a {what}. {DELETES[kind]}", invalid),
            ("GET", "/{id}/history", history, list[ItemVersion], f"Every version of a {what}, newest first.", {}),
            ("GET", "/{id}/versions/{version}", version, dict[str, Any], f"An older version of a {what}, as fields.", {}),
        ]
        for method, suffix, fn, model, doc, responses in routes:
            app.add_api_route(
                path + suffix, fn, methods=[method], response_model=model, description=doc, responses=responses,
                dependencies=v1, tags=["config"], name=f"{fn.__name__}_{kind}", status_code=201 if method == "POST" else 200,
            )

    DELETES = {
        "agent": "Nothing else changes.",
        "instruction": "It's taken out of every agent and group that lists it.",
        "procedure": "It's taken out of every agent and group that lists it.",
        "group": "Wherever it's listed, its blocks are listed in its place, so no prompt changes.",
    }
    item_routes("agent", "agents", AgentIn, NewAgent, AgentConfig)
    item_routes("instruction", "instructions", InstructionIn, NewInstruction, InstructionConfig)
    item_routes("procedure", "procedures", ProcedureIn, NewProcedure, ProcedureConfig)
    item_routes("group", "groups", GroupIn, NewGroup, GroupConfig)

    @app.post("/v1/workspaces/{workspace}/config/agents/{id}/blocks", response_model=AgentSaved, responses={**invalid, 409: {"description": "The agent already uses it."}}, dependencies=v1, tags=["config"])
    def add_block(workspace: str, id: str, req: BlockRequest) -> dict:
        """Add a shared instruction, procedure or group at the end of the agent's blocks. 409 if the agent already uses it."""
        item, changed = store.add_block(workspace, id, req.block, req.author, req.note)
        return {"item": item, "changed": changed}

    @app.delete("/v1/workspaces/{workspace}/config/agents/{id}/blocks/{block}", response_model=AgentSaved, responses={**invalid, 409: {"description": "The agent gets it through a group."}}, dependencies=v1, tags=["config"])
    def remove_block(workspace: str, id: str, block: str, author: str | None = None, note: str | None = None) -> dict:
        """Take a block out of the agent's blocks. 409 if the agent gets it through a group."""
        item, changed = store.remove_block(workspace, id, block, author, note)
        return {"item": item, "changed": changed}

    @app.post("/v1/workspaces/{workspace}/config/agents/{id}/preview", response_model=PreviewResponse, dependencies=v1, tags=["config"])
    def preview_agent(workspace: str, id: str, req: AgentFields) -> dict:
        """The prompt this agent would get with these fields, compiled with the rest of head. Saves nothing."""
        return store.preview_agent(workspace, id, req.model_dump())

    @app.post("/v1/workspaces/{workspace}/migrate", response_model=MigrateResponse, responses=invalid, dependencies=v1, tags=["config"])
    def migrate(workspace: str, req: MigrateRequest | None = None) -> dict:
        """Convert a workspace stored in an older sopc format with `sopc migrate`: the v0.0.8 format, or
        `locked` (v0.0.9), which sopc no longer has. Returns the plan;
        with `apply`, writes the converted files as a draft (publish to release them)."""
        req = req or MigrateRequest()
        return store.migrate(workspace, req.apply, req.author, req.note)

    @app.get("/v1/workspaces/{workspace}/config/settings", response_model=SettingsConfig, dependencies=v1, tags=["config"])
    def get_settings(workspace: str) -> dict:
        return store.items(workspace)["settings"]

    @app.put("/v1/workspaces/{workspace}/config/settings", response_model=dict[str, Any], responses=invalid, dependencies=v1, tags=["config"])
    def put_settings(workspace: str, req: SettingsIn) -> dict:
        item, changed = store.put_item(workspace, "settings", "settings", req.model_dump(exclude={"author", "note"}), req.author, req.note)
        return {"item": item, "changed": changed}

    @app.get("/v1/workspaces/{workspace}/config/settings/history", response_model=list[ItemVersion], dependencies=v1, tags=["config"])
    def settings_history(workspace: str) -> list[dict]:
        return store.item_history(workspace, "settings", "settings")

    @app.get("/v1/workspaces/{workspace}/config/settings/versions/{version}", response_model=dict[str, Any], dependencies=v1, tags=["config"])
    def settings_version(workspace: str, version: int) -> dict:
        return store.item_version(workspace, "settings", "settings", version)

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
    return create_app(Store(os.environ.get("DATABASE_URL", "sqlite:///sopserve.db"), Sopc()))

