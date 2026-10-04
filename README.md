# 🧩 sopserve

The server for [OpenSOP](https://github.com/amanmibra/opensop). OpenSOP builds one full prompt per agent from shared bases and SOPs; sopserve hands each agent its prompt when a call starts, so a merged change goes live without redeploying the agent.

Status: early. The API and a file-based store work; publishing on merge (GitHub App) and a TypeScript client are next.

## Run

```sh
uv tool install 'sopserve @ git+https://github.com/amanmibra/sopserve'
SOPSERVE_TOKEN=... sopserve --port 8484
```

OpenAPI at `/openapi.json`. Data is stored under `SOPSERVE_DATA_DIR` (default `.sopserve-data`).

## API

| Endpoint | What it does |
|---|---|
| `POST /v1/workspaces/<ws>/publish` | Render an OpenSOP workspace (files in the body) and make it the version agents get |
| `GET /v1/workspaces/<ws>/agents/<agent>/prompt` | The agent's full prompt. `<agent>` is the OpenSOP agent id or platform ref (`livekit:tonys-pizza`). Responds with `X-OpenSOP-Hash` and logs the fetch. |
| `GET /v1/workspaces/<ws>/agents/<agent>/sops/<id>` | What a `get_sop` tool returns, for SOPs with `delivery: auto` or `tool` |
| `GET /v1/workspaces/<ws>/fetches` | Which prompt version each agent got, and when, to trace calls to prompts |
| `POST /v1/validate`, `/v1/render`, `/v1/plan`, `/v1/check` | OpenSOP's checks over HTTP, for tools that aren't in Python |

## Develop

```sh
uv sync && uv run pytest
```
