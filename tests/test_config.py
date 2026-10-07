"""The /config endpoints: agents compose shared instructions, procedures and groups; settings."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import insert

from conftest import FIXTURE, WS, expected, read_files
from sopserve.store import file_versions, releases, workspaces

CFG = f"{WS}/config"
OLD = FIXTURE.parent / "old-format" / "sops"

ALLERGEN_MD = """---
# a note for people: front-matter comments never reach a prompt
---
# Allergen check

**Goal:** Customer leaves knowing whether their order is safe for their allergy.
**When:** Any order where the customer mentions a food allergy or dietary restriction.

Parents often ask on behalf of a child. Confirm who the allergy is for before checking items.

## Steps
1. Ask if anyone in the order has a food allergy
2. Name the specific allergen back to the customer
3. Check each item the customer ordered against {{menu_allergen_link}} `tool: lookup_allergens` `required`

## Never
- Never say an item is "allergen-free" or "safe"
- Never place the order before allergens are confirmed `tool: place_order`

## Warning signs
- Customer mentions anaphylaxis or an EpiPen; transfer to {{staff_transfer}} `tool: transfer_to_staff`
"""


@pytest.fixture
def seeded(client, files):
    assert client.put(f"{WS}/files", json={"files": files}).status_code == 200
    assert client.post(f"{WS}/releases", json={"note": "first"}).status_code == 200
    return client


def issue(res, field=None):
    assert res.status_code == 422, res.json()
    issues = res.json()["issues"]
    if field is None:
        return issues[0]
    return next(i for i in issues if i["field"] == field)


def prompts(client):
    return {a["agent"]: a["prompt"] for a in client.get(f"{WS}/draft").json()["agents"]}


def test_config_reads_every_item(seeded):
    cfg = seeded.get(CFG).json()
    assert cfg["settings"]["variables"] == {"staff_transfer": "the manager on duty"}
    assert cfg["settings"]["procedures_heading"] == "## Procedures" and "procedure_order" not in cfg["settings"]

    tonys = next(a for a in cfg["agents"] if a["id"] == "tonys-pizza")
    assert tonys["platform"] == "livekit" and tonys["platform_id"] == "tonys-pizza"
    assert tonys["blocks"] == ["restaurant-host", "pizza-context", "brand-voice", "allergen-check", "delivery-orders", "closing"]
    assert tonys["context"].startswith("Tony's is a wood-fired pizza shop")
    assert list(tonys["variables"]) == ["restaurant_name", "menu_allergen_link"]
    assert tonys["version"] == 1 and tonys["file"] == "agents/tonys-pizza.yaml"

    voice = seeded.get(f"{CFG}/instructions/brand-voice").json()
    assert "locked" not in voice and voice["text"].startswith("Speak warmly") and voice["file"] == "instructions/brand-voice.md"
    assert {u["agent"] for u in voice["used_by"]} == {"tonys-pizza", "luigis-trattoria", "sakura-sushi"}

    [group] = cfg["groups"]
    assert (group["id"], group["blocks"], group["file"], group["version"]) == ("delivery-orders", ["delivery-handling", "large-orders"], "sopc.yaml", 1)
    assert group["used_by"] == [{"agent": "luigis-trattoria", "via": None}, {"agent": "tonys-pizza", "via": None}]

    large = seeded.get(f"{CFG}/procedures/large-orders").json()
    assert large["delivery"] == "auto" and "agents" not in large and "exclude" not in large
    assert large["steps"][1] == {"text": "Check kitchen capacity for the requested time", "tool": "check_capacity", "required": False}
    assert large["used_by"] == [{"agent": "luigis-trattoria", "via": "delivery-orders"}, {"agent": "tonys-pizza", "via": "delivery-orders"}]
    assert seeded.get(f"{CFG}/procedures/reservations").json()["used_by"] == [{"agent": "luigis-trattoria", "via": None}, {"agent": "sakura-sushi", "via": None}]
    assert seeded.get(f"{CFG}/procedures/nope").status_code == 404
    assert seeded.get(f"{CFG}/bases").status_code == 404


def test_saving_unchanged_items_writes_nothing(seeded):
    cfg = seeded.get(CFG).json()
    for plural in ("agents", "instructions", "procedures", "groups"):
        for item in cfg[plural]:
            res = seeded.put(f"{CFG}/{plural}/{item['id']}", json=item)
            assert res.status_code == 200 and res.json()["changed"] == [], (plural, item["id"], res.json())
    assert seeded.put(f"{CFG}/settings", json=cfg["settings"]).json()["changed"] == []
    assert seeded.get(f"{WS}/draft").json()["files"] == []


def test_agent_blocks_are_the_prompt_order(seeded):
    tonys = seeded.get(f"{CFG}/agents/tonys-pizza").json()
    assert prompts(seeded)["tonys-pizza"] == expected("tonys-pizza.prompt.md")
    # Context first, then blocks in list order: move closing to the top.
    blocks = ["closing", *[b for b in tonys["blocks"] if b != "closing"]]
    res = seeded.put(f"{CFG}/agents/tonys-pizza", json={**tonys, "blocks": blocks, "note": "closing first"})
    assert res.status_code == 200, res.json()
    assert res.json()["item"]["blocks"] == blocks
    content = seeded.get(f"{WS}/files/agents/tonys-pizza.yaml").json()["content"]
    assert "blocks:\n  - closing\n  - restaurant-host\n" in content
    prompt = prompts(seeded)["tonys-pizza"]
    assert prompt.index("Tony's is a wood-fired") < prompt.index("Before hanging up") < prompt.index("You are the phone host")

    draft = seeded.get(f"{WS}/draft").json()
    [item] = draft["items"]
    assert (item["kind"], item["id"], item["change"]) == ("agent", "tonys-pizza", "edited")
    assert item["before"]["blocks"] == tonys["blocks"] and item["after"]["blocks"] == blocks

    # The prompt's components follow the expanded order (the group's blocks in place).
    agent = next(a for a in draft["agents"] if a["agent"] == "tonys-pizza")
    assert [(c["kind"], c["id"]) for c in agent["components"]][:3] == [("agent", "tonys-pizza"), ("instruction", "closing"), ("instruction", "restaurant-host")]
    assert [c["id"] for c in agent["components"] if c["kind"] == "sop"] == ["allergen-check", "delivery-handling", "large-orders"]

    # A block listed twice, or unknown, is reported on the blocks field.
    bad = issue(seeded.put(f"{CFG}/agents/tonys-pizza", json={**tonys, "blocks": [*tonys["blocks"], "large-orders"]}), "blocks")
    assert bad["code"] == "duplicate_block" and bad["text"] == "Includes Large orders twice (through the group delivery-orders and directly). List each block once."
    bad = issue(seeded.put(f"{CFG}/agents/tonys-pizza", json={**tonys, "blocks": [*tonys["blocks"], "nope"]}), "blocks")
    assert bad["code"] == "unknown_block" and bad["kind"] == "agent" and bad["id"] == "tonys-pizza"


def test_create_edit_delete_agent(seeded):
    new = {
        "id": "pho-corner",
        "platform": "vapi",
        "platform_id": "asst_9f3e",
        "context": "Pho Corner is a noodle shop.\nNo reservations.\n",
        "blocks": ["restaurant-host", "brand-voice", "allergen-check", "closing"],
        "variables": {"restaurant_name": "Pho Corner", "menu_allergen_link": "pho.example/allergens"},
        "author": "ana",
        "note": "new location",
    }
    res = seeded.post(f"{CFG}/agents", json=new)
    assert res.status_code == 201, res.json()
    assert res.json()["changed"] == [{"path": "agents/pho-corner.yaml", "version": 1, "deleted": False}]
    assert res.json()["item"]["context"] == new["context"]
    assert seeded.post(f"{CFG}/agents", json=new).status_code == 409

    draft = seeded.get(f"{WS}/draft").json()
    [added] = draft["items"]
    assert {k: added[k] for k in ("kind", "id", "name", "change", "version", "released_version", "before")} == {"kind": "agent", "id": "pho-corner", "name": "pho-corner", "change": "added", "version": 1, "released_version": None, "before": None}
    assert added["after"]["blocks"] == new["blocks"] and "file" not in added["after"]
    pho = next(a for a in draft["agents"] if a["agent"] == "pho-corner")
    assert pho["status"] == "added" and pho["platform_ref"] == "vapi:asst_9f3e" and pho["prompt"].startswith("Pho Corner is a noodle shop.") and pho["released_prompt"] is None
    assert "### Delivery" not in pho["prompt"]

    edited = {**new, "blocks": [*new["blocks"][:3], "delivery-orders", "closing"], "author": None, "note": None}
    res = seeded.put(f"{CFG}/agents/pho-corner", json=edited)
    assert res.json()["changed"][0]["version"] == 2
    assert "### Delivery" in prompts(seeded)["pho-corner"]
    assert {"agent": "pho-corner", "via": None} in seeded.get(f"{CFG}/groups/delivery-orders").json()["used_by"]

    history = seeded.get(f"{CFG}/agents/pho-corner/history").json()
    assert [(h["version"], h["author"], h["note"]) for h in history] == [(2, None, None), (1, "ana", "new location")]
    old = seeded.get(f"{CFG}/agents/pho-corner/versions/1").json()
    assert old["blocks"] == new["blocks"] and old["context"] == new["context"]

    res = seeded.delete(f"{CFG}/agents/pho-corner")
    assert res.json()["changed"] == [{"path": "agents/pho-corner.yaml", "version": 3, "deleted": True}]
    assert seeded.get(f"{CFG}/agents/pho-corner").status_code == 404
    assert seeded.get(f"{WS}/draft").json()["items"] == []


def test_any_block_can_be_left_out(seeded):
    sakura = seeded.get(f"{CFG}/agents/sakura-sushi").json()
    res = seeded.put(f"{CFG}/agents/sakura-sushi", json={**sakura, "blocks": [b for b in sakura["blocks"] if b != "brand-voice"]})
    assert res.status_code == 200, res.json()
    assert seeded.delete(f"{CFG}/agents/sakura-sushi/blocks/allergen-check").status_code == 200
    assert "Speak warmly" not in prompts(seeded)["sakura-sushi"]
    # `locked` isn't a field any more; one sent anyway is ignored.
    proc = seeded.get(f"{CFG}/procedures/reservations").json()
    assert seeded.put(f"{CFG}/procedures/reservations", json={**proc, "locked": True}).json()["changed"] == []


def test_locked_from_sopc_v009_is_refused_then_converted(client, files):
    locked = {**files, "instructions/brand-voice.md": "---\nlocked: true\n---\n" + files["instructions/brand-voice.md"]}
    res = client.post(f"{WS}/publish", json={"files": locked})
    bad = issue(res)
    assert (bad["code"], bad["kind"], bad["id"]) == ("removed_field", "instruction", "brand-voice")
    assert "`locked`" in bad["text"] and "sopc migrate" in bad["text"]

    # A workspace stored with it (by a sopserve that spoke sopc v0.0.9) converts like an old-format one.
    now = datetime.now(timezone.utc)
    with client.app.state.store.engine.begin() as conn:
        for path, content in locked.items():
            conn.execute(insert(file_versions).values(workspace="demo", path=path, version=1, content=content, created_at=now, author="old"))
    assert issue(client.get(CFG))["code"] == "removed_field"
    plan = client.post(f"{WS}/migrate", json={}).json()
    assert plan["needed"] and plan["plan"].startswith("Removing fields the format no longer has (1 file(s) change):\n  instructions/brand-voice.md: removed locked\n")
    res = client.post(f"{WS}/migrate", json={"apply": True}).json()
    assert res["applied"] and res["changed"] == [{"path": "instructions/brand-voice.md", "version": 2, "deleted": False}]
    assert client.get(f"{CFG}/instructions/brand-voice").json()["text"].startswith("Speak warmly")
    assert client.post(f"{WS}/migrate", json={}).json()["needed"] is False
    # The version with the lock reads as converted, not as legacy.
    old = client.get(f"{CFG}/instructions/brand-voice/versions/1").json()
    assert "legacy" not in old and old["text"] == files["instructions/brand-voice.md"].strip()


def test_draft_lists_lint_findings(seeded):
    assert seeded.get(f"{WS}/draft").json()["lint"] == []
    tonys = seeded.get(f"{CFG}/agents/tonys-pizza").json()
    context = tonys["context"].rstrip() + " Never upsell more than twice per call.\n"
    assert seeded.put(f"{CFG}/agents/tonys-pizza", json={**tonys, "context": context}).status_code == 200  # advisory: saving works
    [finding] = seeded.get(f"{WS}/draft").json()["lint"]
    assert finding["code"] == "numeric_conflict" and finding["agents"] == ["tonys-pizza"]
    assert [s["text"] for s in finding["sources"]] == ["Never upsell more than twice per call.", "Never upsell more than once per call."]
    assert seeded.post(f"{WS}/releases", json={}).status_code == 200  # and so does publishing


def test_shared_instructions(seeded):
    hours = {"id": "hours", "text": "  We open at {{opening_time}}.  \n"}
    res = seeded.post(f"{CFG}/instructions", json=hours)
    assert res.status_code == 201, res.json()  # nobody uses it yet, so no variable is missing
    assert res.json()["item"]["text"] == "We open at {{opening_time}}." and res.json()["item"]["used_by"] == []
    warning = next(i for i in seeded.get(f"{WS}/draft").json()["issues"] if i["code"] == "unused_block")
    assert (warning["kind"], warning["id"], warning["severity"]) == ("instruction", "hours", "warning")

    res = seeded.post(f"{CFG}/agents/sakura-sushi/blocks", json={"block": "hours"})
    bad = issue(res, "variables")
    assert bad["kind"] == "agent" and bad["id"] == "sakura-sushi" and "{{opening_time}}" in bad["text"]
    settings = seeded.get(f"{CFG}/settings").json()
    assert seeded.put(f"{CFG}/settings", json={**settings, "variables": {**settings["variables"], "opening_time": "11am"}}).status_code == 200
    assert seeded.post(f"{CFG}/agents/sakura-sushi/blocks", json={"block": "hours"}).status_code == 200
    assert prompts(seeded)["sakura-sushi"].rstrip().endswith("We open at 11am.")
    assert seeded.get(f"{CFG}/instructions/hours").json()["used_by"] == [{"agent": "sakura-sushi", "via": None}]

    # Names are shared by instructions, procedures and groups.
    assert issue(seeded.post(f"{CFG}/instructions", json={**hours, "id": "large-orders"}), "id")["code"] == "duplicate_id"
    assert issue(seeded.post(f"{CFG}/instructions", json={**hours, "id": "delivery-orders"}), "id")["text"] == "'delivery-orders' is already the name of a group; pick another name."
    assert issue(seeded.post(f"{CFG}/instructions", json={**hours, "id": "../x"}), "id")["code"] == "invalid_value"
    assert issue(seeded.put(f"{CFG}/instructions/hours", json={**hours, "text": " "}), "text")["code"] == "invalid_value"

    # Deleting one takes it out of every agent and group that lists it.
    group = seeded.get(f"{CFG}/groups/delivery-orders").json()
    assert seeded.put(f"{CFG}/groups/delivery-orders", json={"blocks": [*group["blocks"], "hours"]}).status_code == 200
    res = seeded.delete(f"{CFG}/instructions/hours")
    assert res.status_code == 200, res.json()
    assert {c["path"] for c in res.json()["changed"]} == {"instructions/hours.md", "agents/sakura-sushi.yaml", "sopc.yaml"}
    assert seeded.get(f"{CFG}/groups/delivery-orders").json()["blocks"] == group["blocks"]
    assert seeded.delete(f"{CFG}/instructions/brand-voice").status_code == 200
    assert all("brand-voice" not in a["blocks"] for a in seeded.get(CFG).json()["agents"])


def test_groups(seeded):
    res = seeded.post(f"{CFG}/groups", json={"id": "front-of-house", "blocks": ["restaurant-host", "brand-voice"], "author": "ana"})
    assert res.status_code == 201, res.json()
    assert res.json()["changed"] == [{"path": "sopc.yaml", "version": 2, "deleted": False}]
    content = seeded.get(f"{WS}/files/sopc.yaml").json()["content"]
    assert "groups:\n  delivery-orders:\n    - delivery-handling\n    - large-orders\n  front-of-house:\n    - restaurant-host\n    - brand-voice\n" in content
    assert seeded.post(f"{CFG}/groups", json={"id": "front-of-house", "blocks": []}).status_code == 409

    # An agent lists it in place of its blocks: the prompt doesn't change.
    luigi = seeded.get(f"{CFG}/agents/luigis-trattoria").json()
    assert seeded.put(f"{CFG}/agents/luigis-trattoria", json={**luigi, "blocks": ["front-of-house", *luigi["blocks"][2:]]}).status_code == 200
    assert prompts(seeded)["luigis-trattoria"] == expected("luigis-trattoria.prompt.md")
    assert {"agent": "luigis-trattoria", "via": "front-of-house"} in seeded.get(f"{CFG}/instructions/brand-voice").json()["used_by"]

    # Groups nest; problems are reported on the group.
    bad = issue(seeded.put(f"{CFG}/groups/front-of-house", json={"blocks": ["restaurant-host", "brand-voice", "delivery-orders"]}), "blocks")
    assert bad["code"] == "duplicate_block" and bad["id"] == "luigis-trattoria"  # luigi would get delivery-orders twice
    bad = issue(seeded.post(f"{CFG}/groups", json={"id": "loop", "blocks": ["loop"]}), "blocks")
    assert bad["code"] == "group_cycle" and bad["kind"] == "group" and bad["id"] == "loop"
    bad = issue(seeded.post(f"{CFG}/groups", json={"id": "odd", "blocks": ["nope"]}), "blocks")
    assert (bad["code"], bad["kind"], bad["id"]) == ("unknown_block", "group", "odd")
    assert issue(seeded.post(f"{CFG}/groups", json={"id": "closing", "blocks": []}), "id")["code"] == "duplicate_id"

    # Draft: each group change is its own item, though they live in sopc.yaml.
    assert seeded.put(f"{CFG}/groups/delivery-orders", json={"blocks": ["large-orders", "delivery-handling"]}).status_code == 200
    items = {(i["kind"], i["id"]): i for i in seeded.get(f"{WS}/draft").json()["items"]}
    assert set(items) == {("group", "front-of-house"), ("group", "delivery-orders"), ("agent", "luigis-trattoria")}
    assert items[("group", "front-of-house")]["change"] == "added" and items[("group", "front-of-house")]["after"] == {"id": "front-of-house", "blocks": ["restaurant-host", "brand-voice"]}
    moved = items[("group", "delivery-orders")]
    assert moved["change"] == "edited" and moved["before"]["blocks"] == ["delivery-handling", "large-orders"] and moved["after"]["blocks"] == ["large-orders", "delivery-handling"]

    # History lists only the versions of sopc.yaml that changed that group (or the settings).
    assert [(h["version"], h["author"], h["deleted"]) for h in seeded.get(f"{CFG}/groups/front-of-house/history").json()] == [(2, "ana", False)]
    assert [h["version"] for h in seeded.get(f"{CFG}/groups/delivery-orders/history").json()] == [3, 1]
    assert seeded.get(f"{CFG}/groups/delivery-orders/versions/1").json()["blocks"] == ["delivery-handling", "large-orders"]
    assert [h["version"] for h in seeded.get(f"{CFG}/settings/history").json()] == [1]

    # Deleting a group lists its blocks in its place, so no prompt changes.
    before = prompts(seeded)
    res = seeded.delete(f"{CFG}/groups/front-of-house")
    assert res.status_code == 200, res.json()
    assert seeded.get(f"{CFG}/agents/luigis-trattoria").json()["blocks"][:2] == ["restaurant-host", "brand-voice"]
    assert prompts(seeded) == before
    assert seeded.get(f"{CFG}/groups/front-of-house").status_code == 404
    assert seeded.get(f"{CFG}/groups/front-of-house/history").json()[0]["deleted"] is True


def test_matrix_toggles(seeded):
    # Add: at the end of the agent's blocks.
    res = seeded.post(f"{CFG}/agents/sakura-sushi/blocks", json={"block": "delivery-orders", "author": "ana"})
    assert res.status_code == 200, res.json()
    assert res.json()["item"]["blocks"][-2:] == ["closing", "delivery-orders"]
    assert res.json()["changed"] == [{"path": "agents/sakura-sushi.yaml", "version": 2, "deleted": False}]
    assert "### Delivery" in prompts(seeded)["sakura-sushi"]

    assert seeded.post(f"{CFG}/agents/sakura-sushi/blocks", json={"block": "delivery-orders"}).status_code == 409
    res = seeded.post(f"{CFG}/agents/sakura-sushi/blocks", json={"block": "large-orders"})
    assert res.status_code == 409 and res.json()["detail"] == "sakura-sushi already gets Large orders through the group delivery-orders"
    assert seeded.post(f"{CFG}/agents/sakura-sushi/blocks", json={"block": "nope"}).status_code == 404
    assert seeded.post(f"{CFG}/agents/nobody/blocks", json={"block": "closing"}).status_code == 404

    # Remove: only what the agent lists itself.
    res = seeded.delete(f"{CFG}/agents/tonys-pizza/blocks/large-orders")
    assert res.status_code == 409 and "through the group delivery-orders" in res.json()["detail"]
    assert "brand-voice" not in seeded.delete(f"{CFG}/agents/tonys-pizza/blocks/brand-voice").json()["item"]["blocks"]
    assert seeded.delete(f"{CFG}/agents/tonys-pizza/blocks/reservations").status_code == 404
    res = seeded.delete(f"{CFG}/agents/tonys-pizza/blocks/pizza-context", params={"author": "ana"})
    assert res.status_code == 200 and "pizza-context" not in res.json()["item"]["blocks"]
    assert seeded.delete(f"{CFG}/agents/sakura-sushi/blocks/delivery-orders").json()["item"]["blocks"][-1] == "closing"


def test_preview(seeded):
    tonys = seeded.get(f"{CFG}/agents/tonys-pizza").json()
    res = seeded.post(f"{CFG}/agents/tonys-pizza/preview", json={**tonys, "context": "Closed Mondays."}).json()
    assert res["valid"] and res["prompt"].startswith("Closed Mondays.\n\nYou are the phone host for Tony's Pizza.")
    assert res["released_prompt"] == expected("tonys-pizza.prompt.md") and res["release"] == 1
    assert [b["id"] for b in res["blocks"]][-3:] == ["delivery-handling", "large-orders", "closing"]
    assert seeded.get(f"{WS}/draft").json()["files"] == []  # nothing saved

    res = seeded.post(f"{CFG}/agents/tonys-pizza/preview", json={**tonys, "blocks": ["closing"]}).json()
    assert res["valid"] and res["prompt"].startswith(tonys["context"].strip()) and res["prompt"].rstrip().endswith("delivery time.")
    res = seeded.post(f"{CFG}/agents/tonys-pizza/preview", json={**tonys, "blocks": ["closing", "nope"]}).json()
    assert not res["valid"] and res["prompt"] is None and {i["code"] for i in res["issues"] if i["severity"] == "error"} == {"unknown_block"}
    res = seeded.post(f"{CFG}/agents/new-one/preview", json={"platform": "vapi", "platform_id": "x", "blocks": ["restaurant-host", "brand-voice", "allergen-check"]}).json()
    assert not res["valid"] and any(i["code"] == "unset_variable" for i in res["issues"]) and res["released_prompt"] is None


def test_procedures(seeded):
    proc = {
        "id": "takeout",
        "name": "Takeout",
        "goal": "The order is placed with a pickup time.",
        "when": "The customer wants to pick up.",
        "guidance": "Most takeout calls are short.\n\nKeep them that way.",
        "steps": [{"text": "Take the order", "tool": "place_order", "required": True}, {"text": "Give the pickup time: about 20 minutes"}],
        "never": [{"text": "Never promise an exact time"}],
        "warning_signs": [{"text": "Caller sounds unwell; transfer to {{staff_transfer}}", "tool": "transfer_to_staff"}],
        "delivery": "prompt",
    }
    res = seeded.post(f"{CFG}/procedures", json=proc)
    assert res.status_code == 201, res.json()
    assert res.json()["changed"] == [{"path": "procedures/takeout.md", "version": 1, "deleted": False}]  # new ones are Markdown
    got = seeded.get(f"{CFG}/procedures/takeout").json()
    assert got["format"] == "markdown" and got["guidance"] == proc["guidance"] and got["used_by"] == []
    assert got["steps"] == [{"text": "Take the order", "tool": "place_order", "required": True}, {"text": "Give the pickup time: about 20 minutes", "tool": None, "required": False}]
    content = seeded.get(f"{WS}/files/procedures/takeout.md").json()["content"]
    assert content.startswith("# Takeout\n") and "1. Take the order `tool: place_order` `required`" in content

    group = seeded.get(f"{CFG}/groups/delivery-orders").json()
    assert seeded.put(f"{CFG}/groups/delivery-orders", json={"blocks": [*group["blocks"], "takeout"]}).status_code == 200
    agents = prompts(seeded)
    assert "### Takeout" in agents["tonys-pizza"] and "### Takeout" not in agents["sakura-sushi"]

    # Reorder steps, add one; still Markdown.
    steps = [proc["steps"][1], proc["steps"][0], {"text": "Thank them", "tool": None, "required": False}]
    res = seeded.put(f"{CFG}/procedures/takeout", json={**proc, "steps": steps})
    assert res.json()["changed"] == [{"path": "procedures/takeout.md", "version": 2, "deleted": False}]
    assert [s["text"] for s in seeded.get(f"{CFG}/procedures/takeout").json()["steps"]] == ["Give the pickup time: about 20 minutes", "Take the order", "Thank them"]

    # Invalid edits name the field (and the list entry).
    assert issue(seeded.put(f"{CFG}/procedures/takeout", json={**proc, "steps": []}), "steps")["text"] == "Add at least one step."
    bad = issue(seeded.put(f"{CFG}/procedures/takeout", json={**proc, "never": [{"text": "ok"}, {"text": "  "}]}), "never")
    assert bad["index"] == 1 and bad["kind"] == "procedure" and bad["id"] == "takeout"
    assert issue(seeded.put(f"{CFG}/procedures/takeout", json={**proc, "steps": [{"text": "x", "tool": "two words"}]}), "steps")["index"] == 0
    assert issue(seeded.put(f"{CFG}/procedures/takeout", json={**proc, "name": ""}), "name")
    res = seeded.put(f"{CFG}/procedures/takeout", json={**proc, "goal": ""})
    assert res.status_code == 200  # a missing goal is only a warning
    warning = next(i for i in seeded.get(f"{WS}/draft").json()["issues"] if i["code"] == "missing_goal")
    assert warning["kind"] == "procedure" and warning["id"] == "takeout" and warning["field"] == "goal"

    # Deleting a procedure takes it out of agents and groups.
    res = seeded.delete(f"{CFG}/procedures/takeout")
    assert {c["path"] for c in res.json()["changed"]} == {"procedures/takeout.md", "sopc.yaml"}
    assert seeded.get(f"{CFG}/groups/delivery-orders").json()["blocks"] == group["blocks"]
    assert seeded.delete(f"{CFG}/procedures/reservations").status_code == 200
    assert "reservations" not in seeded.get(f"{CFG}/agents/sakura-sushi").json()["blocks"]


def test_markdown_procedure_from_git_round_trips(client, files):
    files = {p: c for p, c in files.items() if p != "procedures/allergen-check.yaml"}
    files["procedures/allergen-check.md"] = ALLERGEN_MD
    assert client.post(f"{WS}/publish", json={"files": files}).status_code == 200
    # The Markdown SOP compiles to the same prompt as the YAML one did.
    assert client.get(f"{WS}/agents/tonys-pizza/prompt").text == expected("tonys-pizza.prompt.md")

    proc = client.get(f"{CFG}/procedures/allergen-check").json()
    assert proc["format"] == "markdown" and "locked" not in proc
    assert proc["steps"][2] == {"text": "Check each item the customer ordered against {{menu_allergen_link}}", "tool": "lookup_allergens", "required": True}
    assert client.put(f"{CFG}/procedures/allergen-check", json=proc).json()["changed"] == []

    # Editing through the form rewrites the file in canonical Markdown; only the edited text changes.
    proc["never"].append({"text": "Never guess", "tool": None, "required": False})
    res = client.put(f"{CFG}/procedures/allergen-check", json=proc)
    assert res.json()["changed"] == [{"path": "procedures/allergen-check.md", "version": 2, "deleted": False}]
    content = client.get(f"{WS}/files/procedures/allergen-check.md").json()["content"]
    assert content.startswith("# Allergen check\n") and content.endswith("## Never\n- Never say an item is \"allergen-free\" or \"safe\"\n- Never place the order before allergens are confirmed `tool: place_order`\n- Never guess\n\n## Warning signs\n- Customer mentions anaphylaxis or an EpiPen; transfer to {{staff_transfer}} `tool: transfer_to_staff`\n")
    draft = client.get(f"{WS}/draft").json()
    assert {a["agent"] for a in draft["agents"] if a["status"] == "changed"} == {"tonys-pizza", "luigis-trattoria", "sakura-sushi"}

    # Text Markdown can't hold (a guidance line that reads as a heading) switches the file to YAML.
    proc["guidance"] = "## Not a section\nJust guidance."
    res = client.put(f"{CFG}/procedures/allergen-check", json=proc)
    assert {(c["path"], c["deleted"]) for c in res.json()["changed"]} == {("procedures/allergen-check.md", True), ("procedures/allergen-check.yaml", False)}
    got = client.get(f"{CFG}/procedures/allergen-check").json()
    assert got["format"] == "yaml" and got["guidance"] == "## Not a section\nJust guidance."
    old = client.get(f"{CFG}/procedures/allergen-check/versions/1", params={"file": "procedures/allergen-check.md"}).json()
    assert old["never"][-1]["text"] == "Never place the order before allergens are confirmed"
    [item] = client.get(f"{WS}/draft").json()["items"]
    assert (item["kind"], item["change"]) == ("procedure", "edited")
    assert item["before"]["guidance"].startswith("Parents often ask") and item["after"]["guidance"] == "## Not a section\nJust guidance."


TRICKY = [
    "yes", "No", "10", "1.5", "-3", "0x1f", "true", "null", "~", "", " leading", "trailing ", "a: b", "#hash", "a #b", "- dash",
    "[list]", "{map}", "x, y", "'single'", '"double"', "back\\slash", "tab\there", "Café ☕ 東京", "line\nbreak", "two\n\nparagraphs\n",
    "ends with newlines\n\n", "\nstarts with newline", "  indented\nblock", "colon:", "@at", "%pct", "*star", "&amp", "!bang", "|pipe", ">gt",
    "unicode sep here", "nel\x85x", "{{restaurant_name}}",
]


def test_any_text_round_trips_exactly(seeded):
    sakura = seeded.get(f"{CFG}/agents/sakura-sushi").json()
    variables = {**sakura["variables"], **{f"v{i}": t for i, t in enumerate(TRICKY)}}
    for text in TRICKY:
        res = seeded.put(f"{CFG}/agents/sakura-sushi", json={**sakura, "variables": variables, "context": text})
        assert res.status_code == 200, (text, res.json())
        got = seeded.get(f"{CFG}/agents/sakura-sushi").json()
        assert got["context"] == text and got["variables"] == variables, text

    settings = seeded.get(f"{CFG}/settings").json()
    for text in TRICKY[:12]:
        res = seeded.put(f"{CFG}/settings", json={**settings, "procedures_heading": text})
        assert res.status_code == 200, (text, res.json())
        assert seeded.get(f"{CFG}/settings").json()["procedures_heading"] == text
    assert seeded.get(f"{CFG}/groups/delivery-orders").json()["blocks"] == ["delivery-handling", "large-orders"]

    for text in [t for t in TRICKY if t.strip()]:
        proc = {"id": "tricky", "name": text.strip(), "goal": "g", "when": "", "guidance": text, "steps": [{"text": text}], "never": [], "warning_signs": [], "delivery": "prompt"}
        res = seeded.put(f"{CFG}/procedures/tricky", json=proc)
        assert res.status_code == 200, (text, res.json())
        got = seeded.get(f"{CFG}/procedures/tricky").json()
        saved = res.json()["item"]
        assert got["guidance"] == saved["guidance"] and got["steps"] == saved["steps"], text
        # Markdown trims and joins lines; anything else must come back exactly.
        if got["format"] == "yaml":
            assert got["guidance"] == text and got["steps"][0]["text"] == text, text
        else:
            assert got["guidance"].split() == text.split() and got["steps"][0]["text"] == " ".join(text.split()), text


def test_settings_and_new_workspace(client):
    # A new workspace starts from the first form save; sopc.yaml is written with it.
    res = client.put(f"{CFG}/settings", json={"variables": {"brand": "Acme"}, "procedures_heading": "## How to handle calls"})
    assert res.status_code == 200, res.json()
    res = client.post(f"{CFG}/procedures", json={"id": "callback", "name": "Callback", "goal": "A callback is booked.", "steps": [{"text": "Take a number"}]})
    assert res.status_code == 201, res.json()
    res = client.post(f"{CFG}/agents", json={"id": "front-desk", "platform": "retell", "platform_id": "agent_1", "context": "You answer for {{brand}}.", "blocks": ["callback"]})
    assert res.status_code == 201, res.json()
    agent = client.get(f"{WS}/draft").json()["agents"][0]
    assert agent["platform_ref"] == "retell:agent_1" and agent["prompt"].startswith("You answer for Acme.") and "## How to handle calls" in agent["prompt"]

    bad = issue(client.put(f"{CFG}/settings", json={"variables": {}, "procedures_heading": "## Procedures"}), "variables")
    assert bad["kind"] == "agent" and bad["id"] == "front-desk" and "{{brand}}" in bad["text"]

    assert issue(client.post(f"{CFG}/agents", json={"id": "x", "platform": "retell", "platform_id": "agent_1"}), "platform_id")["code"] == "duplicate_platform_ref"
    assert issue(client.post(f"{CFG}/agents", json={"id": "y", "platform": "retell", "platform_id": " "}), "platform_id")["code"] == "invalid_value"
    assert issue(client.post(f"{CFG}/agents", json={"id": "z", "platform": "vapi", "platform_id": "a", "variables": {"bad name": "x"}}), "variables")
    assert issue(client.post(f"{CFG}/agents", json={"id": "z", "platform": "vapi", "platform_id": "a", "blocks": ["no spaces please"]}), "blocks")["index"] == 0

    assert [h["version"] for h in client.get(f"{CFG}/settings/history").json()] == [1]
    assert client.get(f"{CFG}/settings/versions/1").json()["procedures_heading"] == "## How to handle calls"
    assert client.delete(f"{CFG}/agents/nobody").status_code == 404


# --- the sopc v0.0.8 format -------------------------------------------------------------


def store_old(client, release: bool = False) -> None:
    """Head (and optionally release 1) in the old format, as a sopserve that spoke sopc v0.0.8 saved it."""
    old = read_files(OLD)
    now = datetime.now(timezone.utc)
    with client.app.state.store.engine.begin() as conn:
        for path, content in old.items():
            conn.execute(insert(file_versions).values(workspace="demo", path=path, version=1, content=content, created_at=now, author="ana"))
        if release:
            conn.execute(insert(workspaces).values(workspace="demo", current_release=1))
            conn.execute(insert(releases).values(workspace="demo", number=1, created_at=now, files={p: 1 for p in old}, build={"agents": {}}))


def test_old_format_upload_says_to_migrate(client):
    old = read_files(OLD)
    res = client.post(f"{WS}/publish", json={"files": old})
    assert res.status_code == 422
    issues = res.json()["issues"]
    assert {i["code"] for i in issues} == {"old_format"}
    assert "sopc migrate" in issues[0]["text"] and "POST /v1/migrate" in issues[0]["text"]

    # The stateless converter returns files that publish.
    res = client.post("/v1/migrate", json={"files": {**old, "build/lock.json": "{}"}})
    assert res.status_code == 200, res.json()
    body = res.json()
    assert body["migrated"] and "agents/tonys-pizza.yaml: instructions → context" in body["plan"] and "bases/closing.md → instructions/closing.md" in body["plan"]
    assert "instructions/closing.md" in body["files"] and not any(p.startswith(("bases/", "build/")) for p in body["files"])
    assert client.post(f"{WS}/publish", json={"files": body["files"]}).status_code == 200
    assert client.post("/v1/migrate", json={"files": body["files"]}).json() == {"migrated": False, "plan": "", "files": body["files"]}


def test_stored_old_workspace_is_converted_in_place(client):
    store_old(client)
    res = client.get(CFG)
    assert res.status_code == 422 and res.json()["issues"][0]["code"] == "old_format"

    plan = client.post(f"{WS}/migrate", json={}).json()
    assert plan["needed"] and not plan["applied"] and plan["changed"] == [] and "sopc.yaml: removed sop_order" in plan["plan"]
    assert client.get(CFG).status_code == 422  # only the plan so far

    res = client.post(f"{WS}/migrate", json={"apply": True, "author": "ana", "note": "sopc v0.0.9"})
    assert res.status_code == 200 and res.json()["applied"]
    changed = {c["path"]: c["deleted"] for c in res.json()["changed"]}
    assert changed["bases/closing.md"] is True and changed["instructions/closing.md"] is False
    cfg = client.get(CFG).json()
    assert next(a for a in cfg["agents"] if a["id"] == "tonys-pizza")["blocks"][-1] == "closing"
    assert client.post(f"{WS}/migrate", json={}).json()["needed"] is False

    # An agent's version from before reads as legacy content in its history.
    old_agent = client.get(f"{CFG}/agents/tonys-pizza/versions/1").json()
    assert old_agent["legacy"] is True and "inherits" in old_agent["content"]


def test_draft_compares_with_a_released_old_workspace_as_converted(client):
    store_old(client, release=True)
    assert client.post(f"{WS}/migrate", json={"apply": True}).status_code == 200
    items = {(i["kind"], i["id"]): i for i in client.get(f"{WS}/draft").json()["items"]}
    closing = items[("instruction", "closing")]  # bases/closing.md became instructions/closing.md
    assert closing["change"] == "edited" and closing["before"] == closing["after"]
    tonys = items[("agent", "tonys-pizza")]
    assert tonys["before"] == tonys["after"]  # the old release is read as `sopc migrate` converts it
    assert ("settings", "settings") not in items  # dropping sop_order changes no setting
