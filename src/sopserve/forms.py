"""Agents, shared instructions, procedures (SOPs), groups and settings as structured data.

Reading goes through `sopc export`, so there is one parser (sopc's). Writing generates the sopc file
for one item: YAML for agents, YAML procedures and sopc.yaml (settings and groups), Markdown with front
matter for instructions and Markdown procedures. Every generated file is read back with `sopc export`
and must give exactly the values that were asked for; if the readable form doesn't, a fully quoted one
is used instead.

sopc's issues name files; `locate` maps them back to the item and field they're about, in plain words.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable

from .sopc import Files, Invalid, Issue

CONFIG = "sopc.yaml"
PLATFORMS = ("livekit", "vapi", "elevenlabs", "retell")
DELIVERIES = ("prompt", "auto", "tool")
DEFAULT_HEADING = "## Procedures"
KINDS = ("agent", "instruction", "procedure", "group", "settings")
FOLDERS = {"agent": "agents", "instruction": "instructions", "procedure": "procedures"}
PLURALS = {"agent": "agents", "instruction": "instructions", "procedure": "procedures", "group": "groups"}
BLOCK_KINDS = ("instruction", "procedure", "group")  # what an agent's `blocks` can list; one namespace
LEGACY_FOLDERS = {"bases": "instruction"}  # sopc v0.0.8 and earlier

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
VAR_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
TOOL_RE = re.compile(r"^\S+$")

Item = dict[str, Any]


# --- files <-> items ------------------------------------------------------------------


def item_of_path(path: str) -> tuple[str, str] | None:
    """(kind, id) for a source path, e.g. ('procedure', 'large-orders'); None for other files.
    sopc.yaml is ('settings', 'settings'), though it also holds the groups."""
    if path == CONFIG:
        return ("settings", "settings")
    p = PurePosixPath(path)
    if len(p.parts) != 2:
        return None
    if p.parts[0] in LEGACY_FOLDERS and p.suffix == ".md":
        return (LEGACY_FOLDERS[p.parts[0]], p.stem)
    for kind, folder in FOLDERS.items():
        if p.parts[0] == folder and p.suffix in ((".md", ".yaml") if kind == "procedure" else (".md",) if kind == "instruction" else (".yaml",)):
            return (kind, p.stem)
    return None


def paths_of(kind: str, id: str) -> list[str]:
    """Every path an item can live at; the first is where a new one is written."""
    if kind in ("settings", "group"):
        return [CONFIG]
    if kind == "procedure":
        return [f"procedures/{id}.md", f"procedures/{id}.yaml"]
    return [f"{FOLDERS[kind]}/{id}.{'md' if kind == 'instruction' else 'yaml'}"]


def _step(raw: Any) -> dict:
    if isinstance(raw, str):
        return {"text": raw, "tool": None, "required": False}
    return {"text": raw.get("text", ""), "tool": raw.get("tool") or None, "required": bool(raw.get("required"))}


def from_export(exported: dict) -> dict:
    """`sopc export` output as items: {settings, agents, instructions, procedures, groups}.
    Agents, instructions and procedures are sorted by id; groups keep their order in sopc.yaml."""
    cfg = exported["config"]
    return {
        "settings": {"variables": dict(cfg["variables"]), "procedures_heading": cfg["sops_heading"]},
        "agents": [
            {
                "id": a["id"],
                "platform": a["platform"],
                "platform_id": a["platform_id"],
                "context": a["context"],
                "blocks": list(a["blocks"]),
                "variables": dict(a["variables"]),
                "file": a["file"],
            }
            for a in exported["agents"]
        ],
        "instructions": [{"id": b["id"], "text": b["text"], "file": b["file"]} for b in exported["instructions"]],
        "procedures": [
            {
                "id": s["id"],
                "name": s["name"],
                "goal": s["description"],
                "when": s["scope"],
                "guidance": s["guidance"],
                "steps": [_step(x) for x in s["procedureSteps"]],
                "never": [_step(x) for x in s["forbiddenActions"]],
                "warning_signs": [_step(x) for x in s["warningSigns"]],
                "delivery": s["delivery"],
                "format": "markdown" if s["file"].endswith(".md") else "yaml",
                "file": s["file"],
            }
            for s in exported["sops"]
        ],
        "groups": [{"id": name, "blocks": list(blocks), "file": CONFIG} for name, blocks in cfg["groups"].items()],
    }


def config_of(items: dict) -> Item:
    """What sopc.yaml holds: the settings and every group, in order."""
    return {**items["settings"], "groups": {g["id"]: list(g["blocks"]) for g in items["groups"]}}


def find(items: dict, kind: str, id: str) -> Item | None:
    if kind == "settings":
        return items["settings"]
    if kind == "config":
        return config_of(items)
    return next((x for x in items[PLURALS[kind]] if x["id"] == id), None)


def block_kind(items: dict, id: str) -> str | None:
    """'instruction', 'procedure' or 'group' for a block id, or None."""
    for kind in BLOCK_KINDS:
        if any(x["id"] == id for x in items[PLURALS[kind]]):
            return kind
    return None


def expand(items: dict, blocks: list[str], via: str | None = None, seen: frozenset = frozenset()) -> list[tuple[str, str | None]]:
    """An agent's blocks with groups expanded in place: [(block id, the group it came through or None)].
    `via` is the group the agent itself lists, however deeply the block is nested."""
    groups = {g["id"]: g["blocks"] for g in items["groups"]}
    out: list[tuple[str, str | None]] = []
    for b in blocks:
        if b in groups:
            if b not in seen:
                out += expand(items, groups[b], via or b, seen | {b})
        else:
            out.append((b, via))
    return out


def used_by(items: dict) -> dict[str, list[dict]]:
    """{block or group id: [{agent, via}]}: which agents use it, and the group they list it through (None if directly)."""
    out: dict[str, list[dict]] = {}
    groups = {g["id"]: g["blocks"] for g in items["groups"]}

    def groups_in(names: list[str], seen: frozenset) -> list[str]:
        found = []
        for n in names:
            if n in groups and n not in seen:
                found += [n] + groups_in(groups[n], seen | {n})
        return found

    for agent in items["agents"]:
        rows: list[tuple[str, str | None]] = expand(items, agent["blocks"])
        for top in agent["blocks"]:
            if top in groups:
                rows.append((top, None))
                rows += [(g, top) for g in groups_in(groups[top], frozenset({top}))]
        for block, via in rows:
            entry = {"agent": agent["id"], "via": via}
            if entry not in out.setdefault(block, []):
                out[block].append(entry)
    return out


# The fields each kind is compared on (what a form edits). "config" is all of sopc.yaml.
FIELDS = {
    "agent": ("platform", "platform_id", "context", "blocks", "variables"),
    "instruction": ("text",),
    "procedure": ("name", "goal", "when", "guidance", "steps", "never", "warning_signs", "delivery"),
    "group": ("blocks",),
    "settings": ("variables", "procedures_heading"),
    "config": ("variables", "procedures_heading", "groups"),
}


def same(kind: str, a: Item, b: Item) -> bool:
    """Whether two items have the same values (variable and group order count too)."""
    def norm(item: Item) -> list:
        out = []
        for f in FIELDS[kind]:
            v = item.get(f)
            if f in ("variables", "groups"):
                v = list((v or {}).items())
            elif f in ("steps", "never", "warning_signs"):
                v = [(s["text"], s.get("tool") or None, bool(s.get("required"))) for s in v or []]
            out.append(v)
        return out

    return norm(a) == norm(b)


# --- checks before sopc ---------------------------------------------------------------


def check(kind: str, id: str, item: Item) -> list[Issue]:
    """Problems with the values themselves; sopc then checks how items fit together."""
    issues: list[Issue] = []
    path = paths_of(kind, id)[0]

    def bad(field: str, message: str, index: int | None = None) -> None:
        issues.append(Issue("invalid_value", message, path, "error", kind=kind, id=id, field=field, index=index, text=message))

    if kind != "settings" and not ID_RE.match(id):
        bad("id", "Use letters, digits, '-' and '_' (up to 100), starting with a letter or digit.")
    if kind == "agent":
        if item["platform"] not in PLATFORMS:
            bad("platform", "Choose a platform.")
        if not item["platform_id"].strip():
            bad("platform_id", "Enter the agent's id on its platform.")
    if kind in ("agent", "group"):
        for i, b in enumerate(item["blocks"]):
            if not ID_RE.match(b):
                bad("blocks", f"'{b}' isn't the name of a shared instruction, procedure or group.", i)
    if kind in ("agent", "settings"):
        for name in item["variables"]:
            if not VAR_RE.match(name):
                bad("variables", f"'{name}' isn't a valid variable name; use letters, digits, '_', '-' and '.'.")
    if kind == "instruction" and not item["text"].strip():
        bad("text", "Write the instructions.")
    if kind == "procedure":
        if not item["name"].strip():
            bad("name", "Give the procedure a name.")
        if not item["steps"]:
            bad("steps", "Add at least one step.")
        for field in ("steps", "never", "warning_signs"):
            for i, s in enumerate(item[field]):
                if not s["text"].strip():
                    bad(field, "This line is empty.", i)
                if s.get("tool") and not TOOL_RE.match(s["tool"]):
                    bad(field, "A tool name is one word, exactly as the agent's code names it.", i)
    return issues


# --- YAML and Markdown writing ----------------------------------------------------------

_SPECIAL_WORDS = {"true", "false", "yes", "no", "on", "off", "y", "n", "null", "~", ""}
_NUMBERISH = re.compile(r"^[-+]?(\.?[0-9]|\.(inf|nan)$)", re.I)
# Characters YAML reads as line breaks or doesn't allow unescaped.
_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f  ﻿￾￿]")


def _dq(s: str) -> str:
    """A double-quoted YAML scalar (JSON strings are valid YAML), escaping what YAML can't hold raw."""
    out = json.dumps(s, ensure_ascii=False)
    return _UNSAFE.sub(lambda m: f"\\u{ord(m.group()):04x}", out)


def _plain_ok(s: str, flow: bool) -> bool:
    return (
        s == s.strip()
        and s.lower() not in _SPECIAL_WORDS
        and not _NUMBERISH.match(s)
        and s[0] not in "-?:,[]{}#&*!|>'\"%@`<=\\"
        and ": " not in s
        and " #" not in s
        and not s.endswith(":")
        and not _UNSAFE.search(s)
        and "\n" not in s
        and "\t" not in s
        and not (flow and any(c in s for c in ",[]{}"))
    )


@dataclass
class Yaml:
    """Emits YAML; `quoted` writes every string double-quoted (the fallback that always reads back exactly)."""

    quoted: bool = False

    def scalar(self, s: str, flow: bool = False) -> str:
        return s if not self.quoted and _plain_ok(s, flow) else _dq(s)

    def flow(self, items: list[str]) -> str:
        return "[" + ", ".join(self.scalar(x, True) for x in items) + "]"

    def kv(self, head: str, value: str, indent: int = 0) -> str:
        """`head value` (head is e.g. `name:` or `  -`); multi-line text as a `|` block indented by indent + 2."""
        body = value.rstrip("\n")
        trailing = len(value) - len(body)
        block = (
            not self.quoted
            and "\n" in value
            and body
            and not body.startswith((" ", "\t"))
            and not _UNSAFE.search(value)
            and "\r" not in value
            and trailing <= 1
        )
        if not block:
            return f"{head} {self.scalar(value)}"
        pad = " " * (indent + 2)
        lines = [f"{pad}{line}" if line else "" for line in body.split("\n")]
        return f"{head} |{'' if trailing else '-'}\n" + "\n".join(lines)

    def mapping(self, key: str, values: dict[str, str]) -> list[str]:
        if not values:
            return []
        return [f"{key}:"] + [self.kv(f"  {self.scalar(k)}:", v, 2) for k, v in values.items()]

    def ids(self, key: str, values: list[str], indent: int = 0) -> list[str]:
        """A list of ids, one per line (how sopc's docs write `blocks` and groups)."""
        pad = " " * indent
        if not values:
            return [f"{pad}{key}: []"]
        return [f"{pad}{key}:"] + [f"{pad}  - {self.scalar(v)}" for v in values]


def agent_yaml(item: Item, y: Yaml) -> str:
    lines = [f"{item['platform']}: {y.scalar(item['platform_id'])}"]
    if item["context"]:
        lines.append(y.kv("context:", item["context"]))
    if item["blocks"]:
        lines += y.ids("blocks", item["blocks"])
    lines += y.mapping("variables", item["variables"])
    return "\n".join(lines) + "\n"


def config_yaml(item: Item, y: Yaml) -> str:
    lines = ["version: 1"]
    lines += y.mapping("variables", item["variables"])
    if item["groups"]:
        lines.append("groups:")
        for name, blocks in item["groups"].items():
            lines += y.ids(y.scalar(name), blocks, 2)
    if item["procedures_heading"] != DEFAULT_HEADING:
        lines.append(y.kv("sops_heading:", item["procedures_heading"]))
    return "\n".join(lines) + "\n"


def instruction_md(item: Item, y: Yaml) -> str:
    text = item["text"].strip()
    if text.startswith("---"):  # empty front matter, so the text isn't read as front matter
        return "---\n---\n" + text + "\n"
    return text + "\n"


def _settings(item: Item, y: Yaml) -> list[str]:
    out = []
    if item["delivery"] != "prompt":
        out.append(f"delivery: {item['delivery']}")
    return out


def procedure_yaml(item: Item, y: Yaml) -> str:
    lines = [y.kv("name:", item["name"])] + _settings(item, y)
    for key, field in (("description", "goal"), ("scope", "when"), ("guidance", "guidance")):
        if item[field]:
            lines.append(y.kv(f"{key}:", item[field]))
    for key, field in (("procedureSteps", "steps"), ("forbiddenActions", "never"), ("warningSigns", "warning_signs")):
        if not item[field]:
            continue
        lines.append(f"{key}:")
        for s in item[field]:
            if not s.get("tool") and not s.get("required"):
                lines.append(y.kv("  -", s["text"], 2))
                continue
            lines.append(y.kv("  - text:", s["text"], 4))
            if s.get("tool"):
                lines.append(f"    tool: {y.scalar(s['tool'])}")
            if s.get("required"):
                lines.append("    required: true")
    return "\n".join(lines) + "\n"


def procedure_md(item: Item, y: Yaml) -> str:
    front = _settings(item, y)
    head = f"# {item['name']}"
    blocks = [("---\n" + "\n".join(front) + "\n---\n" + head) if front else head]
    fields = [f"**{label}:** {item[f]}" for label, f in (("Goal", "goal"), ("When", "when")) if item[f]]
    if fields:
        blocks.append("\n".join(fields))
    if item["guidance"]:
        blocks.append(item["guidance"])
    for k, (heading, field) in enumerate((("Steps", "steps"), ("Never", "never"), ("Warning signs", "warning_signs"))):
        if not item[field]:
            continue
        rows = []
        for i, s in enumerate(item[field]):
            text = s["text"]
            if s.get("tool"):
                text += f" `tool: {s['tool']}`"
            if s.get("required"):
                text += " `required`"
            rows.append(f"{f'{i + 1}.' if k == 0 else '-'} {text}")
        blocks.append(f"## {heading}\n" + "\n".join(rows))
    return "\n\n".join(blocks) + "\n"


def md_normalized(item: Item) -> Item:
    """What a Markdown procedure can hold: one-line name, goal, when and items; guidance paragraphs."""
    one = lambda s: " ".join(s.split())  # noqa: E731
    paras = [p for p in re.split(r"\n[ \t]*\n", item["guidance"].strip("\n")) if p.strip()]
    guidance = "\n\n".join("\n".join(line.rstrip() for line in p.split("\n")) for p in paras)
    step = lambda s: {"text": one(s["text"]), "tool": s.get("tool") or None, "required": bool(s.get("required"))}  # noqa: E731
    return {
        **item,
        "name": one(item["name"]),
        "goal": one(item["goal"]),
        "when": one(item["when"]),
        "guidance": guidance,
        "steps": [step(s) for s in item["steps"]],
        "never": [step(s) for s in item["never"]],
        "warning_signs": [step(s) for s in item["warning_signs"]],
    }


def normalized(kind: str, item: Item) -> Item:
    """Values as sopc keeps them whatever the file format: instruction text is trimmed."""
    if kind == "instruction":
        return {**item, "text": item["text"].strip()}
    return item


# --- writing an item, checked by reading it back ------------------------------------------


Export = Callable[[Files], dict]  # files -> `sopc export` output; raises Invalid


def write(kind: str, id: str, item: Item, current_path: str | None, export: Export) -> tuple[str, str, Item]:
    """(path, text, the item as sopc reads it back). `kind` is agent, instruction, procedure or config
    (sopc.yaml: settings plus groups). Markdown procedures stay Markdown unless that would change a value,
    then they're written as YAML. Raises Invalid if sopc can't read the values."""
    item = normalized(kind, item)
    if kind == "procedure":
        candidates: list[tuple[str, Callable[[Item, Yaml], str], Item]] = []
        if current_path is None or current_path.endswith(".md"):
            candidates.append((f"procedures/{id}.md", procedure_md, md_normalized(item)))
        candidates.append((f"procedures/{id}.yaml", procedure_yaml, item))
    elif kind == "config":
        candidates = [(CONFIG, config_yaml, item)]
    else:
        writer = {"agent": agent_yaml, "instruction": instruction_md}[kind]
        candidates = [(current_path or paths_of(kind, id)[0], writer, item)]

    last: Invalid | None = None
    for path, writer, want in candidates:
        for y in (Yaml(), Yaml(quoted=True)):
            text = writer(want, y)
            files = {path: text} if kind == "config" else {CONFIG: "", path: text}
            try:
                back = find(from_export(export(files)), kind, id)
            except Invalid as e:
                last = e
                break  # quoting won't help a value sopc rejects
            if back is not None and same(kind, back, want):
                return path, text, back
    if last:
        raise last
    raise RuntimeError(f"could not write {kind} '{id}' so that sopc reads back the same values")


# --- issues -----------------------------------------------------------------------------

# sopc's field names, as the forms call them.
_FIELD_NAMES = {
    "description": "goal",
    "scope": "when",
    "procedureSteps": "steps",
    "forbiddenActions": "never",
    "warningSigns": "warning_signs",
    "sops_heading": "procedures_heading",
    **{p: "platform_id" for p in PLATFORMS},
}
_LOC = re.compile(r"^([A-Za-z_]+)(?:[.\[](\d+)\]?)?(?:\.[A-Za-z_]+)?:\s*(.*)$")
_LINE = re.compile(r"^line \d+:\s*")
_QUOTED = re.compile(r"'([^']*)'")
_GROUP = re.compile(r"^group '([^']*)'")
_VIA = re.compile(r"in group `([^`]*)`")
_STEP_REF = re.compile(r"^(procedureSteps|forbiddenActions|warningSigns)\[(\d+)\]")

OLD_FORMAT_TEXT = (
    "This folder is in the format of sopc v0.0.8 or earlier. Run `sopc migrate --yes` on it "
    "(or send it to POST /v1/migrate), then publish again."
)
REMOVED_FIELD_TEXT = (
    "Uses `locked`, which sopc no longer has. Run `sopc migrate --yes` on the folder "
    "(or convert the workspace) to remove it; no prompt changes."
)


def locate(issue: Issue, names: dict[str, str] | None = None) -> Issue:
    """The issue with the item (kind, id), field, list index and a plain-language `text` filled in.
    `names` maps block ids to what people call them (a procedure's name), for messages about blocks."""
    if issue.text is not None:
        return issue
    names = names or {}
    named = lambda b: names.get(b, b)  # noqa: E731
    where = item_of_path(issue.path) if issue.path else None
    kind, id = where if where else (None, None)
    msg = _LINE.sub("", issue.message)
    quoted = _QUOTED.findall(msg)
    first = quoted[0] if quoted else ""
    group = _GROUP.match(msg)
    if group and kind == "settings":
        kind, id = "group", group.group(1)
        quoted = quoted[1:]
        first = quoted[0] if quoted else ""
    field: str | None = None
    index: int | None = None
    text = msg
    code = issue.code

    if code == "invalid_field":
        m = _LOC.match(msg)
        if m:
            field, index, text = _FIELD_NAMES.get(m.group(1), m.group(1)), int(m.group(2)) if m.group(2) else None, m.group(3)
            if field == "id":
                field = None
        elif "must set exactly one of" in msg:
            field, text = "platform", "Choose one platform."
    elif code in ("colon_in_step", "unquoted_value", "empty_step"):
        m = _STEP_REF.match(msg)
        if m:
            field, index = _FIELD_NAMES[m.group(1)], int(m.group(2))
        else:
            field = "steps"
        text = "This line is empty." if code == "empty_step" else msg
    elif code == "unknown_block":
        field, text = "blocks", f"Lists '{first}', which isn't a shared instruction, procedure or group."
    elif code == "duplicate_block":
        via = _VIA.findall(msg)
        how = f" (through the group {via[0]} and directly)" if len(via) == 1 and "directly" in msg else f" (through the groups {' and '.join(via)})" if len(via) > 1 else ""
        field, text = "blocks", f"Includes {named(first)} twice{how}. List each block once."
    elif code == "group_cycle":
        chain = msg.split(":", 1)[-1].strip()
        kind, id, field = "group", chain.split(" ")[0] if chain else id, "blocks"
        text = f"These groups include each other in a loop: {chain}."
    elif code == "unused_block":
        text = "No agent uses it yet. Add it to an agent's blocks or to a group."
    elif code == "unset_variable":
        name = re.search(r"\{\{([^}]*)\}\}", msg)
        var = name.group(1) if name else first
        field, text = "variables", f"Uses {{{{{var}}}}}, but it has no value here and no default in Settings."
    elif code == "duplicate_platform_ref":
        field, text = "platform_id", "Another agent already uses this platform id."
    elif code == "missing_goal":
        field, text = "goal", "No goal yet. Without one, nobody can judge whether the procedure worked."
    elif code == "missing_steps":
        field, text = "steps", "Add at least one step."
    elif code == "id_mismatch":
        field = "id"
    elif code == "duplicate_id":
        if kind == "group":
            field, text = "id", f"'{id}' is already the name of a shared instruction or procedure; pick another name."
        else:
            kind, id, field = None, first, "id"
            text = f"'{first}' is the name of both a shared instruction and a procedure; names must be unique."
    elif code == "duplicate_file":
        kind, field = "procedure", "id"
        text = f"There are two files for the procedure '{id}'."
    elif code == "missing_config":
        kind, id, text = "settings", "settings", "The workspace has no settings yet."
    elif code == "old_format":
        kind, id, text = None, None, OLD_FORMAT_TEXT
    elif code == "removed_field":
        text = REMOVED_FIELD_TEXT
    if text:
        text = text[0].upper() + text[1:]
    return Issue(issue.code, issue.message, issue.path, issue.severity, kind=kind, id=id, field=field, index=index, text=text)


def located(e: Invalid, names: dict[str, str] | None = None) -> Invalid:
    return Invalid([locate(i, names) for i in e.issues])
