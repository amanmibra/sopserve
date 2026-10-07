import json
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sopserve import Sopc, Store, create_app
from sopserve.store import engine_from_url, metadata

FIXTURE = Path(__file__).parent / "fixtures" / "restaurants"
SOPC_BIN = os.environ.get("SOPC_BIN") or str(Path.home() / "Desktop/repos/sopc/target/release/sopc")
if not Path(SOPC_BIN).exists():
    SOPC_BIN = shutil.which("sopc")

pytestmark = pytest.mark.skipif(not SOPC_BIN, reason="needs a sopc binary with `validate --json` (set SOPC_BIN)")

WS = "/v1/workspaces/demo"


def read_files(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


def expected(name: str) -> str:
    return (FIXTURE / "expected" / name).read_text()


@pytest.fixture
def files():
    return read_files(FIXTURE / "sops")


@pytest.fixture
def client(tmp_path):
    """SQLite by default; set SOPSERVE_TEST_DATABASE_URL to run against Postgres (its tables are dropped first)."""
    url = os.environ.get("SOPSERVE_TEST_DATABASE_URL")
    if url:
        metadata.drop_all(engine_from_url(url))
    store = Store(url or f"sqlite:///{tmp_path / 'test.db'}", Sopc(SOPC_BIN))
    return TestClient(create_app(store, token="secret"), headers={"Authorization": "Bearer secret"})


def edit(client, changes, **kw):
    return client.post(f"{WS}/files", json={"changes": changes, **kw})


# --- stateless ------------------------------------------------------------------


def test_requires_token(tmp_path):
    client = TestClient(create_app(Store(f"sqlite:///{tmp_path / 'a.db'}", Sopc(SOPC_BIN)), token="secret"))
    assert client.post("/v1/validate", json={"files": {}}).status_code == 401
    assert client.get("/v1/workspaces").status_code == 401
    assert client.get("/v1/workspaces", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/").status_code == 200  # the UI shell has no data in it


def test_validate_render_lint(client, files):
    assert client.post("/v1/validate", json={"files": files}).json() == {"valid": True, "issues": []}

    body = client.post("/v1/render", json={"files": {**files, "build/stale.md": "x"}}).json()
    for agent in ("tonys-pizza", "luigis-trattoria", "sakura-sushi"):
        assert body["agents"][agent]["prompt"] == expected(f"{agent}.prompt.md")
    assert body["lock"] == json.loads(expected("lock.json"))
    assert body["agents"]["luigis-trattoria"]["sops"] == json.loads(expected("luigis-trattoria.tool.json"))
    assert body["agents"]["sakura-sushi"]["sops"] == {}

    broken = {**files, "agents/tonys-pizza.yaml": files["agents/tonys-pizza.yaml"].replace("pizza-context", "nope")}
    res = client.post("/v1/validate", json={"files": broken}).json()
    assert res["valid"] is False and res["issues"][0]["code"] == "unknown_base"
    res = client.post("/v1/render", json={"files": broken})
    assert res.status_code == 422 and res.json()["issues"][0]["path"] == "agents/tonys-pizza.yaml"

    no_config = {p: c for p, c in files.items() if p != "sopc.yaml"}
    assert client.post("/v1/validate", json={"files": no_config}).json()["issues"][0]["code"] == "missing_config"

    dup = {**files, "bases/closing.md": files["bases/closing.md"] + "\nSpeak warmly and briefly.\n"}
    findings = client.post("/v1/lint", json={"files": dup}).json()["findings"]
    assert findings and findings[0]["code"] == "duplicate_text"
    assert client.post("/v1/lint", json={"files": broken}).status_code == 422


@pytest.mark.parametrize("path", ["/etc/passwd", "../x.md", "bases/../../x.md", "bases//x.md", "./sopc.yaml", "bases\\x.md", "C:/x.md"])
def test_unsafe_paths_are_rejected(client, files, path):
    res = client.post("/v1/validate", json={"files": {**files, path: "x"}}).json()
    assert res["valid"] is False and res["issues"][0]["code"] == "unsafe_path"
    res = edit(client, {path: "x"})
    assert res.status_code == 422 and res.json()["issues"][0]["code"] == "unsafe_path"
    assert edit(client, {"build/x.md": "x"}).json()["issues"][0]["code"] == "reserved_path"


# --- managed workflow --------------------------------------------------------------


def test_edit_draft_release_fetch_rollback(client, files):
    # Seed head; nothing is live yet.
    res = client.put(f"{WS}/files", json={"files": files, "author": "ana"})
    assert res.status_code == 200 and len(res.json()["changed"]) == len(files)
    assert client.get(f"{WS}/agents/tonys-pizza").status_code == 404
    assert client.get("/v1/workspaces").json() == [{"workspace": "demo", "current_release": None}]

    draft = client.get(f"{WS}/draft").json()
    assert draft["release"] is None and {a["status"] for a in draft["agents"]} == {"added"}

    r1 = client.post(f"{WS}/releases", json={"note": "first"}).json()
    assert r1["created"] and r1["release"]["number"] == 1
    assert client.post(f"{WS}/releases", json={}).json() == {**r1, "created": False}

    # Invalid edits are rejected and leave head untouched.
    res = edit(client, {"agents/tonys-pizza.yaml": "livekit: tonys-pizza\ninherits: [nope]\n"})
    assert res.status_code == 422 and res.json()["issues"][0]["code"] == "unknown_base"
    res = edit(client, {"bases/brand-voice.md": "Be brief.", "bases/pizza-context.md": None})
    assert res.status_code == 422  # tonys-pizza still inherits pizza-context
    assert client.get(f"{WS}/files/bases/brand-voice.md").json()["version"] == 1

    # A valid edit bumps only that file's version, and isn't live until released.
    voice = files["bases/brand-voice.md"].replace("briefly", "concisely")
    res = edit(client, {"bases/brand-voice.md": voice, "bases/closing.md": files["bases/closing.md"]}, author="ana", note="tone")
    assert res.json()["changed"] == [{"path": "bases/brand-voice.md", "version": 2, "deleted": False}]
    assert edit(client, {"bases/brand-voice.md": voice}).json()["changed"] == []
    assert "briefly" in client.get(f"{WS}/agents/tonys-pizza/prompt").text

    draft = client.get(f"{WS}/draft").json()
    assert draft["release"] == 1
    assert draft["files"] == [{"path": "bases/brand-voice.md", "change": "edited", "version": 2, "released_version": 1}]
    assert {a["agent"]: a["status"] for a in draft["agents"]} == {"luigis-trattoria": "changed", "sakura-sushi": "changed", "tonys-pizza": "changed"}
    assert "concisely" in draft["agents"][0]["prompt"]

    r2 = client.post(f"{WS}/releases", json={"author": "ana", "note": "tone"}).json()["release"]
    assert r2["number"] == 2 and r2["files"]["bases/brand-voice.md"] == 2

    # The call-start lookup: prompt plus the component versions it was built from.
    agent = client.get(f"{WS}/agents/livekit:tonys-pizza").json()
    assert agent["agent"] == "tonys-pizza" and agent["release"] == 2 and "concisely" in agent["prompt"]
    assert agent["tool_sops"] == ["large-orders"]
    components = {c["path"]: c["version"] for c in agent["components"]}
    assert components["bases/brand-voice.md"] == 2 and components["bases/pizza-context.md"] == 1
    assert components["procedures/large-orders.yaml"] == 1 and components["sopc.yaml"] == 1
    assert components["agents/tonys-pizza.yaml"] == 1
    assert "procedures/reservations.yaml" not in components

    # Pinning an older release (A/B) serves it unchanged.
    pinned = client.get(f"{WS}/agents/tonys-pizza", params={"release": 1}).json()
    assert pinned["prompt"] == expected("tonys-pizza.prompt.md") and pinned["release"] == 1
    res = client.get(f"{WS}/agents/tonys-pizza/prompt", params={"release": 1})
    assert res.text == expected("tonys-pizza.prompt.md") and res.headers["X-Sopc-Release"] == "1"

    fetches = client.get(f"{WS}/fetches", params={"agent": "tonys-pizza"}).json()
    assert [(f["release"], f["hash"]) for f in fetches] == [(1, pinned["hash"]), (1, pinned["hash"]), (2, agent["hash"]), (1, pinned["hash"])]
    assert client.get(f"{WS}/fetches", params={"agent": "sakura-sushi"}).json() == []

    # Roll back.
    assert client.post(f"{WS}/releases/1/activate").json() == {"workspace": "demo", "current_release": 1}
    res = client.get(f"{WS}/agents/tonys-pizza/prompt")
    assert res.text == expected("tonys-pizza.prompt.md") and res.headers["X-Sopc-Hash"] == pinned["hash"]
    assert [(r["number"], r["current"]) for r in client.get(f"{WS}/releases").json()] == [(2, False), (1, True)]
    assert client.post(f"{WS}/releases/9/activate").status_code == 404

    detail = client.get(f"{WS}/releases/2").json()
    assert "concisely" in detail["agents"]["sakura-sushi"]["prompt"] and detail["current"] is False

    # History and old versions.
    history = client.get(f"{WS}/history/bases/brand-voice.md").json()
    assert [(h["version"], h["author"], h["note"]) for h in history] == [(2, "ana", "tone"), (1, "ana", None)]
    old = client.get(f"{WS}/files/bases/brand-voice.md", params={"version": 1}).json()
    assert old["content"] == files["bases/brand-voice.md"]


def test_delete_and_add_files(client, files):
    client.put(f"{WS}/files", json={"files": files})
    client.post(f"{WS}/releases")

    sakura, reservations = files["agents/sakura-sushi.yaml"], files["procedures/reservations.yaml"]
    res = edit(client, {"agents/sakura-sushi.yaml": None, "procedures/reservations.yaml": reservations.replace("sakura-sushi", "new-place")})
    assert res.status_code == 422 and res.json()["issues"][0]["code"] == "unknown_agent"  # all or nothing
    res = edit(
        client,
        {
            "agents/sakura-sushi.yaml": None,
            "agents/new-place.yaml": sakura.replace("sakura-sushi", "new-place"),
            "procedures/reservations.yaml": reservations.replace("sakura-sushi", "new-place"),
        },
    )
    assert res.status_code == 200, res.json()
    assert {c["path"]: (c["version"], c["deleted"]) for c in res.json()["changed"]} == {
        "agents/new-place.yaml": (1, False),
        "agents/sakura-sushi.yaml": (2, True),
        "procedures/reservations.yaml": (2, False),
    }

    draft = client.get(f"{WS}/draft").json()
    statuses = {a["agent"]: a["status"] for a in draft["agents"]}
    assert statuses["sakura-sushi"] == "removed" and statuses["new-place"] == "added"
    assert {f["path"]: f["change"] for f in draft["files"]} == {
        "agents/new-place.yaml": "added",
        "agents/sakura-sushi.yaml": "deleted",
        "procedures/reservations.yaml": "edited",
    }

    assert client.get(f"{WS}/files/agents/sakura-sushi.yaml").status_code == 404
    assert client.get(f"{WS}/files/agents/sakura-sushi.yaml", params={"version": 2}).json()["deleted"] is True

    # Restoring a deleted file continues its version numbers.
    restored = edit(client, {"agents/sakura-sushi.yaml": sakura, "procedures/reservations.yaml": reservations}).json()["changed"]
    assert restored[0] == {"path": "agents/sakura-sushi.yaml", "version": 3, "deleted": False}


def test_git_publish_replace_and_export(client, files):
    res = client.post(f"{WS}/publish", json={"files": {**files, "build/lock.json": "{}"}, "author": "ci", "note": "abc123"})
    body = res.json()
    assert res.status_code == 200 and body["created"] and body["release"]["number"] == 1
    assert client.get(f"{WS}/agents/tonys-pizza/prompt").text == expected("tonys-pizza.prompt.md")

    again = client.post(f"{WS}/publish", json={"files": files}).json()
    assert not again["created"] and again["changed"] == [] and again["release"]["number"] == 1

    # A path missing from a full replace is deleted.
    without = {p: c for p, c in files.items() if p != "procedures/reservations.yaml"}
    body = client.post(f"{WS}/publish", json={"files": without}).json()
    assert body["changed"] == [{"path": "procedures/reservations.yaml", "version": 2, "deleted": True}]
    assert "### Reservations" not in client.get(f"{WS}/agents/sakura-sushi/prompt").text

    broken = {**files, "sopc.yaml": "version: [\n"}
    res = client.post(f"{WS}/publish", json={"files": broken})
    assert res.status_code == 422 and res.json()["issues"][0]["code"] == "invalid_yaml"
    assert client.get(f"{WS}/releases").json()[0]["number"] == 2

    exported = client.get(f"{WS}/files", params={"content": True}).json()["files"]
    assert {f["path"]: f["content"] for f in exported} == without
    listed = client.get(f"{WS}/files").json()["files"]
    assert all(f["content"] is None for f in listed) and len(listed) == len(without)

    sop = client.get(f"{WS}/agents/luigis-trattoria/sops/large-orders").json()
    assert sop == json.loads(expected("luigis-trattoria.tool.json"))["large-orders"]
    assert client.get(f"{WS}/agents/luigis-trattoria/sops/allergen-check").status_code == 404
    assert client.get(f"{WS}/agents/nobody/prompt").status_code == 404
    assert client.get("/v1/workspaces/unpublished/agents/tonys-pizza").status_code == 404


def test_openapi(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/v1/workspaces/{workspace}/agents/{agent}" in paths
    assert "/v1/workspaces/{workspace}/publish" in paths
    assert "/" not in paths and "/v1/plan" not in paths
