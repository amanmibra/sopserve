from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sopkit.loader import read_files
from sopserve import FileStore, create_app

FIXTURE = Path(__file__).parent / "fixtures" / "restaurants" / "sops"


# --- server -------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(FileStore(tmp_path / "data"), token="secret"), headers={"Authorization": "Bearer secret"})


def test_server_requires_token(tmp_path):
    client = TestClient(create_app(FileStore(tmp_path), token="secret"))
    assert client.post("/v1/validate", json={"files": {}}).status_code == 401


def test_validate_and_render_endpoints(client):
    files = read_files(FIXTURE)
    assert client.post("/v1/validate", json={"files": files}).json() == {"valid": True, "issues": []}

    body = client.post("/v1/render", json={"files": files}).json()
    expected = (FIXTURE.parent / "expected" / "tonys-pizza.prompt.md").read_text()
    assert body["agents"]["tonys-pizza"]["prompt"] == expected

    broken = {**files, "agents/tonys-pizza.yaml": files["agents/tonys-pizza.yaml"].replace("pizza-context", "nope")}
    assert client.post("/v1/validate", json={"files": broken}).json()["valid"] is False
    res = client.post("/v1/render", json={"files": broken})
    assert res.status_code == 422
    assert res.json()["issues"][0]["code"] == "unknown_base"


def test_plan_endpoint(client):
    base = read_files(FIXTURE)
    head = {**base, "bases/brand-voice.md": base["bases/brand-voice.md"].replace("briefly", "concisely")}
    body = client.post("/v1/plan", json={"base": base, "head": head}).json()
    assert body["by_block"] == [
        {"block": "base:brand-voice", "change": "edited", "agents": ["luigis-trattoria", "sakura-sushi", "tonys-pizza"]}
    ]
    assert body["markdown"].startswith("**sopkit plan:** 3 agents change")


def test_publish_then_fetch_prompt_logs_the_version(client):
    files = read_files(FIXTURE)
    published = client.post("/v1/workspaces/demo/publish", json={"files": files}).json()

    res = client.get("/v1/workspaces/demo/agents/tonys-pizza/prompt")
    assert res.status_code == 200
    assert res.text == (FIXTURE.parent / "expected" / "tonys-pizza.prompt.md").read_text()
    assert res.headers["X-Sopkit-Build"] == published["build_id"]

    by_ref = client.get("/v1/workspaces/demo/agents/livekit:tonys-pizza/prompt")
    assert by_ref.text == res.text

    fetches = client.get("/v1/workspaces/demo/fetches", params={"agent": "tonys-pizza"}).json()
    assert [f["hash"] for f in fetches] == [res.headers["X-Sopkit-Hash"]] * 2

    edited = {**files, "bases/brand-voice.md": files["bases/brand-voice.md"].replace("briefly", "concisely")}
    client.post("/v1/workspaces/demo/publish", json={"files": edited})
    assert "concisely" in client.get("/v1/workspaces/demo/agents/tonys-pizza/prompt").text


def test_get_sop_and_not_found(client):
    client.post("/v1/workspaces/demo/publish", json={"files": read_files(FIXTURE)})
    sop = client.get("/v1/workspaces/demo/agents/luigis-trattoria/sops/large-orders").json()
    assert sop["procedureSteps"][1]["tool"] == "check_capacity"
    assert client.get("/v1/workspaces/demo/agents/luigis-trattoria/sops/allergen-check").status_code == 404
    assert client.get("/v1/workspaces/demo/agents/nobody/prompt").status_code == 404
    assert client.get("/v1/workspaces/unpublished/agents/tonys-pizza/prompt").status_code == 404


def test_openapi_lists_the_customer_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/v1/workspaces/{workspace}/agents/{agent}/prompt" in paths
    assert "/v1/plan" in paths
