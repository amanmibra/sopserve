"""The /config endpoints: agents, shared instructions (bases), procedures and settings as forms."""

import pytest

from conftest import WS, expected

CFG = f"{WS}/config"

ALLERGEN_MD = """---
agents: "*"
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


def test_config_reads_every_item(seeded):
    cfg = seeded.get(CFG).json()
    assert cfg["settings"]["variables"] == {"staff_transfer": "the manager on duty"}
    assert cfg["settings"]["procedure_order"] == ["allergen-check"]
    assert cfg["settings"]["procedures_heading"] == "## Procedures"

    sakura = next(a for a in cfg["agents"] if a["id"] == "sakura-sushi")
    assert sakura["platform"] == "livekit" and sakura["platform_id"] == "sakura-sushi"
    assert sakura["inherits"] == ["restaurant-host"] and sakura["exclude"] == ["delivery-handling"]
    assert list(sakura["variables"]) == ["restaurant_name", "menu_allergen_link", "staff_transfer"]
    assert sakura["version"] == 1 and sakura["file"] == "agents/sakura-sushi.yaml"

    voice = seeded.get(f"{CFG}/bases/brand-voice").json()
    assert voice["agents"] == "*" and voice["locked"] is True and voice["position"] == "top"
    assert voice["text"].startswith("Speak warmly")

    large = seeded.get(f"{CFG}/procedures/large-orders").json()
    assert large["agents"] == ["tonys-pizza", "luigis-trattoria"] and large["delivery"] == "auto"
    assert large["steps"][1] == {"text": "Check kitchen capacity for the requested time", "tool": "check_capacity", "required": False}
    assert large["goal"].startswith("Orders over 10 items") and large["format"] == "yaml"
    assert seeded.get(f"{CFG}/procedures/nope").status_code == 404


def test_saving_unchanged_items_writes_nothing(seeded):
    cfg = seeded.get(CFG).json()
    for plural in ("agents", "bases", "procedures"):
        for item in cfg[plural]:
            res = seeded.put(f"{CFG}/{plural}/{item['id']}", json=item)
            assert res.status_code == 200 and res.json()["changed"] == [], (plural, item["id"], res.json())
    assert seeded.put(f"{CFG}/settings", json=cfg["settings"]).json()["changed"] == []
    assert seeded.get(f"{WS}/draft").json()["files"] == []


def test_create_edit_delete_agent(seeded):
    new = {
        "id": "pho-corner",
        "platform": "vapi",
        "platform_id": "asst_9f3e",
        "inherits": ["restaurant-host"],
        "variables": {"restaurant_name": "Pho Corner", "menu_allergen_link": "pho.example/allergens"},
        "instructions": "Pho Corner is a noodle shop.\nNo reservations.\n",
        "exclude": ["delivery-handling"],
        "author": "ana",
        "note": "new location",
    }
    res = seeded.post(f"{CFG}/agents", json=new)
    assert res.status_code == 201, res.json()
    assert res.json()["changed"] == [{"path": "agents/pho-corner.yaml", "version": 1, "deleted": False}]
    assert res.json()["item"]["instructions"] == new["instructions"]
    assert seeded.post(f"{CFG}/agents", json=new).status_code == 409

    draft = seeded.get(f"{WS}/draft").json()
    [added] = draft["items"]
    assert {k: added[k] for k in ("kind", "id", "name", "change", "version", "released_version", "before")} == {"kind": "agent", "id": "pho-corner", "name": "pho-corner", "change": "added", "version": 1, "released_version": None, "before": None}
    assert added["after"]["platform_id"] == "asst_9f3e" and added["after"]["exclude"] == ["delivery-handling"] and "file" not in added["after"]
    pho = next(a for a in draft["agents"] if a["agent"] == "pho-corner")
    assert pho["status"] == "added" and pho["platform_ref"] == "vapi:asst_9f3e" and "Pho Corner" in pho["prompt"] and pho["released_prompt"] is None
    assert "### Delivery" not in pho["prompt"]

    edited = {**new, "instructions": "Pho Corner is a noodle shop.", "exclude": [], "author": None, "note": None}
    res = seeded.put(f"{CFG}/agents/pho-corner", json=edited)
    assert res.json()["changed"][0]["version"] == 2
    assert "### Delivery" in next(a for a in seeded.get(f"{WS}/draft").json()["agents"] if a["agent"] == "pho-corner")["prompt"]

    history = seeded.get(f"{CFG}/agents/pho-corner/history").json()
    assert [(h["version"], h["author"], h["note"]) for h in history] == [(2, None, None), (1, "ana", "new location")]
    old = seeded.get(f"{CFG}/agents/pho-corner/versions/1").json()
    assert old["exclude"] == ["delivery-handling"] and old["instructions"] == new["instructions"]

    res = seeded.delete(f"{CFG}/agents/pho-corner")
    assert res.json()["changed"] == [{"path": "agents/pho-corner.yaml", "version": 3, "deleted": True}]
    assert seeded.get(f"{CFG}/agents/pho-corner").status_code == 404
    assert seeded.get(f"{WS}/draft").json()["items"] == []


def test_deleting_an_agent_removes_it_from_targeting(seeded):
    res = seeded.delete(f"{CFG}/agents/tonys-pizza", params={"author": "ana"})
    assert res.status_code == 200, res.json()
    changed = {c["path"] for c in res.json()["changed"]}
    assert changed == {"agents/tonys-pizza.yaml", "procedures/large-orders.yaml"}
    assert seeded.get(f"{CFG}/procedures/large-orders").json()["agents"] == ["luigis-trattoria"]
    draft = seeded.get(f"{WS}/draft").json()
    items = {(i["kind"], i["id"]): i for i in draft["items"]}
    assert {k: i["change"] for k, i in items.items()} == {("agent", "tonys-pizza"): "deleted", ("procedure", "large-orders"): "edited"}
    assert items[("agent", "tonys-pizza")]["after"] is None and items[("agent", "tonys-pizza")]["before"]["platform_id"] == "tonys-pizza"
    large = items[("procedure", "large-orders")]
    assert large["before"]["agents"] == ["tonys-pizza", "luigis-trattoria"] and large["after"]["agents"] == ["luigis-trattoria"]
    assert large["before"]["steps"] == large["after"]["steps"]
    removed = next(a for a in draft["agents"] if a["agent"] == "tonys-pizza")
    assert removed["status"] == "removed" and removed["prompt"] is None and removed["released_prompt"] == expected("tonys-pizza.prompt.md")


def test_shared_instructions(seeded):
    base = {"id": "hours", "text": "  We open at {{opening_time}}.  \n", "agents": ["sakura-sushi"], "exclude": [], "inherits": ["brand-voice"], "position": "bottom", "locked": False}
    res = seeded.post(f"{CFG}/bases", json=base)
    issue_ = issue(res, "variables")
    assert issue_["kind"] == "agent" and issue_["id"] == "sakura-sushi" and "{{opening_time}}" in issue_["text"]

    settings = seeded.get(f"{CFG}/settings").json()
    settings["variables"]["opening_time"] = "11am"
    assert seeded.put(f"{CFG}/settings", json=settings).status_code == 200
    res = seeded.post(f"{CFG}/bases", json=base)
    assert res.status_code == 201, res.json()
    assert res.json()["item"]["text"] == "We open at {{opening_time}}."  # trimmed, as sopc reads it
    prompt = next(a for a in seeded.get(f"{WS}/draft").json()["agents"] if a["agent"] == "sakura-sushi")["prompt"]
    assert prompt.rstrip().endswith("We open at 11am.")

    # All agents, with one left out.
    res = seeded.put(f"{CFG}/bases/hours", json={**base, "agents": "*", "exclude": ["tonys-pizza"]})
    assert res.status_code == 200, res.json()
    agents = {a["agent"]: a["prompt"] for a in seeded.get(f"{WS}/draft").json()["agents"]}
    assert "11am" in agents["luigis-trattoria"] and "11am" not in agents["tonys-pizza"]

    # Invalid edits name the field.
    assert issue(seeded.put(f"{CFG}/bases/hours", json={**base, "inherits": ["nope"]}), "inherits")["text"] == "Uses shared instructions 'nope', which don't exist."
    assert issue(seeded.put(f"{CFG}/bases/hours", json={**base, "agents": ["nobody"]}), "agents")["kind"] == "base"
    assert issue(seeded.put(f"{CFG}/bases/restaurant-host", json={**base, "inherits": ["pizza-context"]}), "inherits")["code"] == "inheritance_cycle"
    assert issue(seeded.put(f"{CFG}/bases/hours", json={**base, "text": " "}), "text")["code"] == "invalid_value"
    assert issue(seeded.post(f"{CFG}/bases", json={**base, "id": "large-orders"}), "id")["code"] == "duplicate_id"
    assert issue(seeded.post(f"{CFG}/bases", json={**base, "id": "../x"}), "id")["code"] == "invalid_value"

    # A locked base can't be skipped by an agent.
    sakura = seeded.get(f"{CFG}/agents/sakura-sushi").json()
    res = seeded.put(f"{CFG}/agents/sakura-sushi", json={**sakura, "exclude": ["delivery-handling", "brand-voice"]})
    assert issue(res, "exclude")["code"] == "locked"

    # Deleting a base removes it from whoever inherits it.
    res = seeded.delete(f"{CFG}/bases/pizza-context")
    assert res.status_code == 200, res.json()
    assert seeded.get(f"{CFG}/agents/tonys-pizza").json()["inherits"] == []


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
        "agents": "*",
        "exclude": ["sakura-sushi"],
        "delivery": "prompt",
        "locked": False,
    }
    res = seeded.post(f"{CFG}/procedures", json=proc)
    assert res.status_code == 201, res.json()
    assert res.json()["changed"] == [{"path": "procedures/takeout.md", "version": 1, "deleted": False}]  # new ones are Markdown
    got = seeded.get(f"{CFG}/procedures/takeout").json()
    assert got["format"] == "markdown" and got["guidance"] == proc["guidance"]
    assert got["steps"] == [{"text": "Take the order", "tool": "place_order", "required": True}, {"text": "Give the pickup time: about 20 minutes", "tool": None, "required": False}]
    content = seeded.get(f"{WS}/files/procedures/takeout.md").json()["content"]
    assert "1. Take the order `tool: place_order` `required`" in content

    agents = {a["agent"]: a["prompt"] for a in seeded.get(f"{WS}/draft").json()["agents"]}
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
    assert issue(seeded.put(f"{CFG}/procedures/takeout", json={**proc, "exclude": ["nobody"]}), "exclude")["code"] == "unknown_agent"
    res = seeded.put(f"{CFG}/procedures/takeout", json={**proc, "goal": ""})
    assert res.status_code == 200  # a missing goal is only a warning
    draft = seeded.get(f"{WS}/draft").json()
    warning = next(i for i in draft["issues"] if i["code"] == "missing_goal")
    assert warning["kind"] == "procedure" and warning["id"] == "takeout" and warning["field"] == "goal"

    # Deleting a procedure removes it from the order and from agents that skip it.
    res = seeded.delete(f"{CFG}/procedures/delivery-handling")
    assert res.status_code == 200, res.json()
    assert seeded.get(f"{CFG}/agents/sakura-sushi").json()["exclude"] == []
    res = seeded.delete(f"{CFG}/procedures/allergen-check")
    assert res.status_code == 200, res.json()
    assert seeded.get(f"{CFG}/settings").json()["procedure_order"] == []


def test_markdown_procedure_from_git_round_trips(client, files):
    files = {p: c for p, c in files.items() if p != "procedures/allergen-check.yaml"}
    files["procedures/allergen-check.md"] = ALLERGEN_MD
    assert client.post(f"{WS}/publish", json={"files": files}).status_code == 200
    # The Markdown SOP compiles to the same prompt as the YAML one did.
    assert client.get(f"{WS}/agents/tonys-pizza/prompt").text == expected("tonys-pizza.prompt.md")

    proc = client.get(f"{CFG}/procedures/allergen-check").json()
    assert proc["format"] == "markdown"
    assert proc["steps"][2] == {"text": "Check each item the customer ordered against {{menu_allergen_link}}", "tool": "lookup_allergens", "required": True}
    assert client.put(f"{CFG}/procedures/allergen-check", json=proc).json()["changed"] == []

    # Editing through the form rewrites the file in canonical Markdown; only the edited text changes.
    proc["never"].append({"text": "Never guess", "tool": None, "required": False})
    res = client.put(f"{CFG}/procedures/allergen-check", json=proc)
    assert res.json()["changed"] == [{"path": "procedures/allergen-check.md", "version": 2, "deleted": False}]
    content = client.get(f"{WS}/files/procedures/allergen-check.md").json()["content"]
    assert content.startswith('---\nagents: "*"\n---\n# Allergen check\n') and content.endswith("## Never\n- Never say an item is \"allergen-free\" or \"safe\"\n- Never place the order before allergens are confirmed `tool: place_order`\n- Never guess\n\n## Warning signs\n- Customer mentions anaphylaxis or an EpiPen; transfer to {{staff_transfer}} `tool: transfer_to_staff`\n")
    assert client.get(f"{CFG}/procedures/allergen-check").json()["never"][-1]["text"] == "Never guess"
    draft = client.get(f"{WS}/draft").json()
    assert {a["agent"] for a in draft["agents"] if a["status"] == "changed"} == {"tonys-pizza", "luigis-trattoria", "sakura-sushi"}

    # Text Markdown can't hold (a guidance line that reads as a heading) switches the file to YAML.
    proc["guidance"] = "## Not a section\nJust guidance."
    res = client.put(f"{CFG}/procedures/allergen-check", json=proc)
    assert {(c["path"], c["deleted"]) for c in res.json()["changed"]} == {("procedures/allergen-check.md", True), ("procedures/allergen-check.yaml", False)}
    got = client.get(f"{CFG}/procedures/allergen-check").json()
    assert got["format"] == "yaml" and got["guidance"] == "## Not a section\nJust guidance."
    history = client.get(f"{CFG}/procedures/allergen-check/history").json()
    assert [(h["file"], h["version"], h["deleted"]) for h in history][:2] in (
        [("procedures/allergen-check.yaml", 1, False), ("procedures/allergen-check.md", 3, True)],
        [("procedures/allergen-check.md", 3, True), ("procedures/allergen-check.yaml", 1, False)],
    )
    old = client.get(f"{CFG}/procedures/allergen-check/versions/1", params={"file": "procedures/allergen-check.md"}).json()
    assert old["never"][-1]["text"] == "Never place the order before allergens are confirmed"
    [item] = client.get(f"{WS}/draft").json()["items"]
    assert (item["kind"], item["change"]) == ("procedure", "edited")
    assert item["before"]["guidance"].startswith("Parents often ask") and item["after"]["guidance"] == "## Not a section\nJust guidance."
    assert len(item["after"]["never"]) == len(item["before"]["never"]) + 1  # read from the released Markdown file


TRICKY = [
    "yes", "No", "10", "1.5", "-3", "0x1f", "true", "null", "~", "", " leading", "trailing ", "a: b", "#hash", "a #b", "- dash",
    "[list]", "{map}", "x, y", "'single'", '"double"', "back\\slash", "tab\there", "Café ☕ 東京", "line\nbreak", "two\n\nparagraphs\n",
    "ends with newlines\n\n", "\nstarts with newline", "  indented\nblock", "colon:", "@at", "%pct", "*star", "&amp", "!bang", "|pipe", ">gt",
    "unicode sep here", "nel\x85x", "{{restaurant_name}}",
]


def test_any_text_round_trips_exactly(seeded):
    sakura = seeded.get(f"{CFG}/agents/sakura-sushi").json()
    variables = {**sakura["variables"], **{f"v{i}": t for i, t in enumerate(TRICKY)}}
    for text in TRICKY:
        res = seeded.put(f"{CFG}/agents/sakura-sushi", json={**sakura, "variables": variables, "instructions": text})
        assert res.status_code == 200, (text, res.json())
        got = seeded.get(f"{CFG}/agents/sakura-sushi").json()
        assert got["instructions"] == text and got["variables"] == variables, text

    settings = seeded.get(f"{CFG}/settings").json()
    for text in TRICKY[:12]:
        if not text:
            continue
        res = seeded.put(f"{CFG}/settings", json={**settings, "procedures_heading": text})
        assert res.status_code == 200, (text, res.json())
        assert seeded.get(f"{CFG}/settings").json()["procedures_heading"] == text

    for text in [t for t in TRICKY if t.strip()]:
        proc = {"id": "tricky", "name": text.strip(), "goal": "g", "when": "", "guidance": text, "steps": [{"text": text}], "never": [], "warning_signs": [], "agents": [], "exclude": [], "delivery": "prompt", "locked": False}
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
    res = client.put(f"{CFG}/settings", json={"variables": {"brand": "Acme"}, "procedures_heading": "## How to handle calls", "procedure_order": []})
    assert res.status_code == 200, res.json()
    res = client.post(f"{CFG}/agents", json={"id": "front-desk", "platform": "retell", "platform_id": "agent_1", "instructions": "You answer for {{brand}}."})
    assert res.status_code == 201, res.json()
    res = client.post(f"{CFG}/procedures", json={"id": "callback", "name": "Callback", "goal": "A callback is booked.", "steps": [{"text": "Take a number"}], "agents": "*"})
    assert res.status_code == 201, res.json()
    agent = client.get(f"{WS}/draft").json()["agents"][0]
    assert agent["platform_ref"] == "retell:agent_1" and "You answer for Acme." in agent["prompt"] and "## How to handle calls" in agent["prompt"]

    res = client.put(f"{CFG}/settings", json={"variables": {}, "procedures_heading": "## Procedures", "procedure_order": ["nope"]})
    bad = issue(res, "procedure_order")
    assert bad["kind"] == "settings" and bad["code"] == "unknown_sop"
    bad = issue(client.put(f"{CFG}/settings", json={"variables": {}, "procedures_heading": "## Procedures", "procedure_order": []}), "variables")
    assert bad["kind"] == "agent" and bad["id"] == "front-desk" and "{{brand}}" in bad["text"]

    assert issue(client.post(f"{CFG}/agents", json={"id": "x", "platform": "retell", "platform_id": "agent_1"}), "platform_id")["code"] == "duplicate_platform_ref"
    assert issue(client.post(f"{CFG}/agents", json={"id": "y", "platform": "retell", "platform_id": " "}), "platform_id")["code"] == "invalid_value"
    assert issue(client.post(f"{CFG}/agents", json={"id": "z", "platform": "vapi", "platform_id": "a", "variables": {"bad name": "x"}}), "variables")

    history = client.get(f"{CFG}/settings/history").json()
    assert [h["version"] for h in history] == [1]
    assert client.get(f"{CFG}/settings/versions/1").json()["procedures_heading"] == "## How to handle calls"
    assert client.delete(f"{CFG}/agents/nobody").status_code == 404
