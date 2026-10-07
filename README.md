# 📋🧩 sopserve

The server for [sopc](https://github.com/amanmibra/sopc). sopc compiles shared bases and SOPs into one full prompt per agent; sopserve stores those files with versions, releases them, and hands each agent its prompt when a call starts, so a change goes live without redeploying the agent.

Prompts are compiled by the `sopc` binary itself, so what sopserve serves matches `sopc` on the command line byte for byte.

Status: v0. Single-token auth; see the [roadmap](#roadmap).

## Two ways to use it

**Managed mode.** The files live in sopserve's database and are edited over the API (or the built-in UI at `/`). Each file is versioned on its own. Edits are drafts: every write must compile, but nothing reaches agents until someone publishes a release. A release is an immutable snapshot of `{path: version}` plus the compiled prompts, and you can pin, compare or roll back to any release. At call start, the agent asks for its prompt and gets back the release number and the exact file versions it was built from, for call metadata.

**Git mode.** The files live in a repo; CI pushes the whole sopc folder on merge with `POST /v1/workspaces/<ws>/publish` (replace + release in one call). Anything edited in sopserve can be exported back with `GET /v1/workspaces/<ws>/files?content=true`.

Both modes use the same workspaces, releases and serving endpoints, so you can start in one and move to the other.

## Run locally

Needs the `sopc` binary with `validate --json` (the release after v0.0.7). Until it ships, build sopc from source and point `SOPC_BIN` at it.

```sh
uv tool install 'sopserve @ git+https://github.com/amanmibra/sopserve'
SOPC_BIN=/path/to/sopc SOPSERVE_TOKEN=dev sopserve --port 8484
```

Open http://localhost:8484 for the UI, `/docs` for the API, `/openapi.json` for the spec SDKs are generated from.

| Variable | Default | |
|---|---|---|
| `DATABASE_URL` | `sqlite:///sopserve.db` | Any SQLAlchemy URL. `postgres://…` URLs use psycopg 3 (`pip install 'sopserve[postgres]'`). |
| `SOPSERVE_TOKEN` | unset (no auth) | Require `Authorization: Bearer <token>` on every `/v1` endpoint |
| `SOPC_BIN` | `sopc` on `PATH` | The sopc binary |
| `HOST`, `PORT` | `127.0.0.1`, `8484` | Also `--host`, `--port` |

Tables are created on startup.

## Deploy (Docker + Postgres)

```sh
docker build -t sopserve .            # installs sopc with SOPC_REF (build arg, default v0.0.8)
docker run -p 8484:8484 \
  -e DATABASE_URL='postgres://user:pass@host:5432/db' \
  -e SOPSERVE_TOKEN=... sopserve
```

Any Postgres works (Supabase, Neon, RDS): paste the connection string as `DATABASE_URL`. Without it the container uses SQLite in the `/data` volume.

## API

Paths below are under `/v1/workspaces/<ws>`. Errors that come from sopc return `422 {"valid": false, "issues": [{code, message, path, severity}]}`.

| Endpoint | What it does |
|---|---|
| `GET /v1/workspaces` | List workspaces and their current release |
| `GET …/files` | Head: every file with its version. `?content=true` includes content (export) |
| `POST …/files` | `{changes: {path: content \| null}, author?, note?}`: edit or delete files atomically. Rejected if the result doesn't compile |
| `PUT …/files` | `{files, author?, note?}`: replace head; files not listed are deleted |
| `GET …/files/<path>?version=N` | A file at head or at a version |
| `GET …/history/<path>` | Every version of a file |
| `GET …/draft` | Unpublished changes: files changed since the current release, and agents added, changed, removed or unchanged, with draft prompts |
| `POST …/releases` | `{author?, note?}`: release head and serve it (returns the current release if nothing changed) |
| `GET …/releases`, `GET …/releases/<n>` | Release history; one release with every agent's prompt |
| `POST …/releases/<n>/activate` | Serve an older release (roll back) |
| `POST …/publish` | `{files, author?, note?}`: replace + release in one call, for CI |
| `GET …/agents` | Agents in the current release (`?release=N` for another) |
| `GET …/agents/<agent>` | **Call start.** `{agent, platform_ref, release, hash, prompt, tools, tool_sops, components}`. `?release=N` pins a release (A/B). Logs a fetch |
| `GET …/agents/<agent>/prompt` | Just the prompt as text, with `X-Sopc-Release` and `X-Sopc-Hash`. Logs a fetch |
| `GET …/agents/<agent>/sops/<id>` | What a `get_sop` tool returns, for SOPs with `delivery: auto` or `tool` |
| `GET …/fetches?agent=` | Which release and prompt hash each agent was served, newest first |
| `POST /v1/validate`, `/v1/render`, `/v1/lint` | `{files}`: sopc's checks over HTTP, nothing stored |

`<agent>` is the sopc agent id (`tonys-pizza`) or its platform ref (`livekit:tonys-pizza`). `components` lists each file the prompt was built from at its version (`agent`, `base`, `sop`, plus `sopc.yaml` as `config`). Paths under `build/` in uploads are ignored; absolute paths and `..` are rejected.

## LiveKit: load the prompt at room start

```python
import json, os, httpx
from livekit import api
from livekit.agents import Agent, AgentSession, JobContext

SOPSERVE = os.environ["SOPSERVE_URL"]  # e.g. https://sopserve.internal/v1/workspaces/prod
HEADERS = {"Authorization": f"Bearer {os.environ['SOPSERVE_TOKEN']}"}

async def entrypoint(ctx: JobContext):
    await ctx.connect()
    params = {"release": pinned} if (pinned := os.environ.get("SOPC_RELEASE")) else {}  # e.g. chosen by your A/B router
    async with httpx.AsyncClient(headers=HEADERS, timeout=5) as http:
        res = await http.get(f"{SOPSERVE}/agents/livekit:{ctx.job.agent_name}", params=params)
        res.raise_for_status()
        sop = res.json()

    # Record what ran on this call: the release and every component at its version.
    async with api.LiveKitAPI() as lk:  # LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
        await lk.room.update_room_metadata(api.UpdateRoomMetadataRequest(
            room=ctx.room.name,
            metadata=json.dumps({"sopc": {"release": sop["release"], "hash": sop["hash"], "components": sop["components"]}}),
        ))

    session = AgentSession(...)
    await session.start(room=ctx.room, agent=Agent(instructions=sop["prompt"]))
```

For A/B tests, have your router pick a release per call (e.g. 70% current, 30% a candidate) and pass it as `?release=N`; the room metadata and the fetch log record which one ran.

## Roadmap

- Per-key API keys with scopes (read-only keys for agents, write keys for editors and CI), replacing the single `SOPSERVE_TOKEN`
- A webhook on publish, so a release can be synced into your own tables
- Schema migrations (tables are created on startup today)

## Develop

```sh
uv sync && SOPC_BIN=/path/to/sopc uv run pytest   # sopc with validate --json
```
