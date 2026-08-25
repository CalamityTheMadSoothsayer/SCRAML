"""
webui/app.py
SCRAML's Flask app: serves the Flow Editor and Queries tabs, and the
JSON API backing them (load/save/rename/delete a flow, lint every flow
and base file on disk, manage queries/*.sql and promoted "verbs").

Run from source with:
    python webui/app.py
then open http://127.0.0.1:5000/ (a browser tab is opened automatically
when frozen -- see __main__ below).
"""

from __future__ import annotations

import os
import re
import sys
import threading
import webbrowser
from pathlib import Path

import yaml
from flask import Flask, abort, jsonify, render_template, request

# When frozen by PyInstaller, sys._MEIPASS is the temp dir the bundled
# read-only resources (templates/, flows/, config/ defaults) were
# extracted into. Writable state -- an edited flow, a saved query --
# must instead live next to the exe (sys.executable's folder), or it
# would vanish the next time the temp extraction dir is recreated.
if getattr(sys, "frozen", False):
    BUNDLE_ROOT = Path(sys._MEIPASS)
    APP_ROOT = Path(sys.executable).resolve().parent
else:
    BUNDLE_ROOT = Path(__file__).resolve().parent.parent
    APP_ROOT = BUNDLE_ROOT

ROOT = APP_ROOT
FLOWS_DIR = BUNDLE_ROOT / "flows"
# Writable counterpart to FLOWS_DIR -- the Flow Editor's saves land here
# instead, since FLOWS_DIR is read-only (under sys._MEIPASS) in a frozen
# build. Reads check here FIRST, falling back to FLOWS_DIR, so an edited
# flow shadows the bundled default of the same name. From source,
# FLOWS_WRITE_DIR is simply flows/ itself (APP_ROOT == BUNDLE_ROOT), so
# editor saves go straight to the real flows/ directory in dev.
FLOWS_WRITE_DIR = APP_ROOT / "flows"
FLOWS_BASE_WRITE_DIR = FLOWS_WRITE_DIR / "base"
UI_PREFS_PATH = APP_ROOT / "config" / "ui_prefs.json"
# Named DB connections the Queries tab's Run button can target -- see
# load_connections/save_connections below. Plaintext, deliberately: this
# is a local single-user dev tool, and the tradeoff was made explicitly
# (see the file's own header comment) rather than adding keyring/prompt
# complexity. Keep this out of version control.
CONNECTIONS_PATH = APP_ROOT / "config" / "connections.json"

os.chdir(APP_ROOT)

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(BUNDLE_ROOT))

from webui.flow_blocks import (  # noqa: E402 -- must follow the chdir/sys.path setup above
    STEP_TYPES,
    blocks_to_flow,
    build_base_step_types,
    build_verb_step_types,
    flow_to_blocks,
    has_inject,
    parse_inject_points,
    references_base_file,
    validate_blocks,
)

# Explicit rather than Flask's default (relative to this file's own
# location) -- __file__ under a frozen exe sits inside the temp
# _MEIPASS extraction dir, but BUNDLE_ROOT/webui/templates is exactly
# where --add-data places it (see SCRAML.spec), so this works
# identically frozen or from source.
app = Flask(
    __name__,
    template_folder=str(BUNDLE_ROOT / "webui" / "templates"),
    static_folder=str(BUNDLE_ROOT / "webui" / "static"),
)


def _flow_metadata(path: Path) -> dict:
    """Best-effort name/description straight from the flow's own YAML, so
    the UI shows the same human-readable name the flow file already
    documents itself with instead of a raw filename."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        data = None
    data = data or {}
    return {
        "name": data.get("name", path.stem),
        "description": data.get("description", ""),
        "section": data.get("section", ""),
    }


def list_available_flows() -> list[dict]:
    flows = []
    for path in sorted(FLOWS_DIR.glob("*.yaml")):
        rel = f"flows/{path.name}"
        meta = _flow_metadata(path)
        flows.append({"path": rel, **meta})
    return flows


def load_ui_prefs() -> dict:
    import json

    if not UI_PREFS_PATH.exists():
        return {"theme": "light"}
    try:
        return json.loads(UI_PREFS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"theme": "light"}


def write_ui_prefs(prefs: dict) -> None:
    import json

    UI_PREFS_PATH.write_text(json.dumps(prefs, indent=4), encoding="utf-8")


@app.get("/")
def index():
    return render_template(
        "index.html",
        available=list_available_flows(),
        step_types=build_full_step_types(),
        ui_prefs=load_ui_prefs(),
    )


@app.get("/api/ui-prefs")
def api_ui_prefs_get():
    return jsonify(load_ui_prefs())


@app.post("/api/ui-prefs")
def api_ui_prefs_set():
    body = request.get_json(force=True, silent=True) or {}
    theme = body.get("theme")
    if theme not in ("light", "dark"):
        abort(400)
    prefs = load_ui_prefs()
    prefs["theme"] = theme
    write_ui_prefs(prefs)
    return jsonify(prefs)


@app.get("/api/state")
def api_state():
    return jsonify({"available": list_available_flows()})


# ---------------------------------------------------------------------
# Flow Editor
# ---------------------------------------------------------------------


def _resolve_flow_path(rel: str) -> Path | None:
    """Resolves a flow-editor-relative path (e.g. 'login.yaml' or
    'base/pallet_build_finish.yaml') to an on-disk file, checking the
    writable dir first so a locally-edited flow shadows the bundled
    read-only default of the same name. Returns None if neither
    location has it."""
    write_path = FLOWS_WRITE_DIR / rel
    if write_path.exists():
        return write_path
    bundle_path = FLOWS_DIR / rel
    if bundle_path.exists():
        return bundle_path
    return None


def _is_base_rel(rel: str) -> bool:
    return Path(rel).parts[:1] == ("base",)


def _list_flow_dir(write_dir: Path, bundle_dir: Path, rel_prefix: str) -> list[dict]:
    """Lists *.yaml under both write_dir and bundle_dir, deduped by
    filename with the write-dir version winning (it's the one that would
    actually be loaded -- see _resolve_flow_path)."""
    by_name: dict[str, Path] = {}
    for base_dir in (bundle_dir, write_dir):
        if not base_dir.exists():
            continue
        for path in sorted(base_dir.glob("*.yaml")):
            by_name[path.name] = path  # write_dir iterated last -- wins on collision

    entries = []
    for name, path in sorted(by_name.items()):
        meta = _flow_metadata(path)
        entries.append({"path": f"{rel_prefix}{name}", **meta})
    return entries


@app.get("/api/flows/list")
def api_flows_list():
    flows = _list_flow_dir(FLOWS_WRITE_DIR, FLOWS_DIR, "")
    base = _list_flow_dir(FLOWS_BASE_WRITE_DIR, FLOWS_DIR / "base", "base/")
    return jsonify({"flows": flows, "base": base, "step_types": build_full_step_types()})


@app.get("/api/step-types")
def api_step_types():
    """Lets the Flow Editor refetch the merged built-ins + verbs table
    live (see flow_editor.js's refreshStepTypes) after a verb is
    promoted/removed on the Queries tab, without a full page reload."""
    return jsonify(build_full_step_types())


@app.get("/api/flows/<path:rel>")
def api_flows_get(rel):
    path = _resolve_flow_path(rel)
    if path is None:
        return jsonify({"error": f"No such flow: {rel!r}"}), 404
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    envelope = flow_to_blocks(data, build_full_step_types())
    if _is_base_rel(rel):
        envelope["has_inject"] = has_inject(data)
    return jsonify(envelope)


@app.post("/api/flows/<path:rel>")
def api_flows_save(rel):
    body = request.get_json(force=True)
    steps = body.get("steps", [])
    full_step_types = build_full_step_types()

    problems = validate_blocks(steps, full_step_types)
    if problems:
        return jsonify({"error": "Invalid flow", "problems": problems}), 400

    try:
        data = blocks_to_flow(body, full_step_types)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    dest = (FLOWS_BASE_WRITE_DIR if _is_base_rel(rel) else FLOWS_WRITE_DIR) / Path(rel).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return jsonify({"saved": True, "path": rel})


_FILENAME_RE = re.compile(r"^[A-Za-z0-9_\-]+\.yaml$")


@app.post("/api/flows/<path:rel>/rename")
def api_flows_rename(rel):
    src = _resolve_flow_path(rel)
    if src is None:
        return jsonify({"error": f"No such flow: {rel!r}"}), 404

    body = request.get_json(force=True)
    new_filename = body.get("filename", "")
    if not _FILENAME_RE.match(new_filename):
        return jsonify({"error": "filename must be a bare '<name>.yaml' with no path separators"}), 400

    is_base = _is_base_rel(rel)
    new_rel = f"base/{new_filename}" if is_base else new_filename
    if new_rel != rel and _resolve_flow_path(new_rel) is not None:
        return jsonify({"error": f"{new_rel!r} already exists"}), 400

    dest_dir = FLOWS_BASE_WRITE_DIR if is_base else FLOWS_WRITE_DIR
    dest = dest_dir / new_filename
    dest.parent.mkdir(parents=True, exist_ok=True)

    # src may be the bundled read-only copy under FLOWS_DIR (not
    # FLOWS_WRITE_DIR) when nothing's been edited/saved locally yet --
    # write the new file fresh from its parsed contents rather than
    # assuming a plain os.rename source path exists in the writable dir.
    data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    dest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    if new_rel != rel:
        old_write_path = dest_dir / Path(rel).name
        if old_write_path.exists() and old_write_path != dest:
            old_write_path.unlink()

    return jsonify({"renamed": True, "path": new_rel})


@app.post("/api/flows/<path:rel>/delete")
def api_flows_delete(rel):
    if _resolve_flow_path(rel) is None:
        return jsonify({"error": f"No such flow: {rel!r}"}), 404

    write_path = (FLOWS_BASE_WRITE_DIR if _is_base_rel(rel) else FLOWS_WRITE_DIR) / Path(rel).name
    if write_path.exists():
        write_path.unlink()
    return jsonify({"deleted": True, "path": rel})


@app.post("/api/flows/new")
def api_flows_new():
    body = request.get_json(force=True)
    kind = body.get("kind")
    filename = body.get("filename", "")

    if kind not in ("flow", "base"):
        return jsonify({"error": "kind must be 'flow' or 'base'"}), 400
    if not _FILENAME_RE.match(filename):
        return jsonify({"error": "filename must be a bare '<name>.yaml' with no path separators"}), 400

    rel = f"base/{filename}" if kind == "base" else filename
    if _resolve_flow_path(rel) is not None:
        return jsonify({"error": f"{rel!r} already exists"}), 400

    dest = (FLOWS_BASE_WRITE_DIR if kind == "base" else FLOWS_WRITE_DIR) / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    empty = {"name": Path(filename).stem, "description": "", "steps": []}
    dest.write_text(yaml.safe_dump(empty, sort_keys=False), encoding="utf-8")

    envelope = flow_to_blocks(empty)
    if kind == "base":
        envelope["has_inject"] = False
    return jsonify(envelope)


# Configurable via SCRAML_QUERIES_DIR so a deployment can point queries
# somewhere other than the bundled default -- falls back to APP_ROOT /
# "queries" (a plain relative "queries/" dir next to the exe/source).
QUERIES_DIR = Path(os.environ.get("SCRAML_QUERIES_DIR", str(BUNDLE_ROOT / "queries")))
# Writable counterpart to QUERIES_DIR, same shadowing pattern as
# FLOWS_WRITE_DIR/FLOWS_DIR: from source these are the same directory
# (queries/), but in a frozen build QUERIES_DIR sits under the read-only
# _MEIPASS extraction, so a query created/edited from the Queries tab
# has to land here instead for it to actually persist.
QUERIES_WRITE_DIR = Path(os.environ.get("SCRAML_QUERIES_DIR", str(APP_ROOT / "queries")))

_QUERY_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _resolve_query_path(name: str) -> Path | None:
    write_path = QUERIES_WRITE_DIR / f"{name}.sql"
    if write_path.is_file():
        return write_path
    bundle_path = QUERIES_DIR / f"{name}.sql"
    if bundle_path.is_file():
        return bundle_path
    return None


@app.get("/api/queries")
def api_queries():
    names = set()
    if QUERIES_DIR.is_dir():
        names.update(p.stem for p in QUERIES_DIR.glob("*.sql"))
    if QUERIES_WRITE_DIR.is_dir():
        names.update(p.stem for p in QUERIES_WRITE_DIR.glob("*.sql"))
    # mtime of whichever copy would actually be loaded (write dir shadows
    # the bundled one, same as _resolve_query_path) -- feeds the Queries
    # sidebar's "Updated Xd ago" meta line.
    queries = []
    for name in sorted(names):
        path = _resolve_query_path(name)
        updated = path.stat().st_mtime if path else None
        queries.append({"name": name, "updated": updated})
    return jsonify({"queries": queries, "verbs": list_verb_names()})


@app.get("/api/queries/<name>")
def api_query_get(name):
    if not _QUERY_NAME_RE.match(name):
        return jsonify({"error": "invalid query name"}), 400
    path = _resolve_query_path(name)
    if path is None:
        return jsonify({"error": f"No such query: {name!r}"}), 404
    return jsonify({"name": name, "sql": path.read_text(encoding="utf-8")})


@app.post("/api/queries/<name>")
def api_query_save(name):
    if not _QUERY_NAME_RE.match(name):
        return jsonify({"error": "query name must be letters, digits, underscore, or hyphen only"}), 400
    body = request.get_json(force=True)
    sql = body.get("sql", "")
    QUERIES_WRITE_DIR.mkdir(parents=True, exist_ok=True)
    (QUERIES_WRITE_DIR / f"{name}.sql").write_text(sql, encoding="utf-8")
    return jsonify({"saved": True, "name": name})


@app.delete("/api/queries/<name>")
def api_query_delete(name):
    if not _QUERY_NAME_RE.match(name):
        return jsonify({"error": "invalid query name"}), 400
    if _resolve_query_path(name) is None:
        return jsonify({"error": f"No such query: {name!r}"}), 404
    write_path = QUERIES_WRITE_DIR / f"{name}.sql"
    if write_path.exists():
        write_path.unlink()
    return jsonify({"deleted": True, "name": name})


# --- Verbs: a query promoted to its own labeled/colored palette pill ---
# (see flow_blocks.build_verb_step_types) instead of generic "query" +
# an action dropdown. A verb is still just a plain queries/*.sql file --
# VERBS_DIR only marks "also show this one as its own block type".
VERBS_DIR = QUERIES_WRITE_DIR / "verbs"


def list_verb_names() -> list[str]:
    if not VERBS_DIR.is_dir():
        return []
    return sorted(p.stem for p in VERBS_DIR.glob("*.sql"))


def _verb_meta_path(name: str) -> Path:
    return VERBS_DIR / f"{name}.meta.json"


def load_verb_meta(name: str) -> dict:
    """{"label", "section"} overrides for one verb, saved by the Verb
    Editor -- both blank/absent by default, in which case
    build_verb_step_types falls back to an auto-derived label and the
    generic "Custom Verbs" palette section."""
    import json

    path = _verb_meta_path(name)
    if not path.is_file():
        return {"label": "", "section": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = {}
    return {"label": data.get("label", ""), "section": data.get("section", "")}


def save_verb_meta(name: str, label: str, section: str) -> None:
    import json

    path = _verb_meta_path(name)
    if not label and not section:
        # Nothing to override -- remove any stale sidecar rather than
        # writing an all-empty one, so a verb with no customization has
        # no meta file at all (matches how every other optional-key
        # convention in this app avoids clutter on disk).
        if path.is_file():
            path.unlink()
        return
    path.write_text(json.dumps({"label": label, "section": section}, indent=2), encoding="utf-8")


def list_verbs() -> list[dict]:
    """Every verb with its current (possibly overridden) label/section,
    for the Verb Editor tab and for build_full_step_types."""
    result = []
    for name in list_verb_names():
        meta = load_verb_meta(name)
        result.append({"name": name, "label": meta["label"], "section": meta["section"]})
    return result


def list_base_files() -> list[dict]:
    """flows/base/*.yaml, deduped the same write-dir-wins way as
    _list_flow_dir -- one {"filename", "label", "section",
    "inject_points"} dict per file, feeding build_base_step_types so
    each base file gets its own Flow Editor palette pill (with one
    __inject_slot__ per inject point it declares) instead of the generic
    "base" block."""
    by_name: dict[str, Path] = {}
    for base_dir in (FLOWS_DIR / "base", FLOWS_BASE_WRITE_DIR):
        if not base_dir.exists():
            continue
        for path in sorted(base_dir.glob("*.yaml")):
            by_name[path.name] = path
    result = []
    for name, path in sorted(by_name.items()):
        meta = _flow_metadata(path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        inject_points = parse_inject_points(data.get("steps") or [])
        result.append({
            "filename": name,
            "label": meta["name"] or name,
            "section": meta["section"],
            "inject_points": inject_points,
        })
    return result


# ---------------------------------------------------------------------
# Block types: persisted, user-editable versions of STEP_TYPES's plain
# (non-container-sugar) entries -- see the Blocks tab. Nothing on YAML's
# own is privileged; `tap`/`wait`/`group`/etc. are exactly as arbitrary
# as a SQL-backed verb, so they're all equally editable here. Read-write
# copy-on-first-edit: BLOCK_TYPES_PATH doesn't exist until something is
# actually saved/deleted through the Blocks tab, so an app nobody has
# customized behaves identically to the old hardcoded-STEP_TYPES version
# (see load_block_types' fallback).
# ---------------------------------------------------------------------

BLOCK_TYPES_PATH = APP_ROOT / "config" / "step_types.json"


def load_block_types() -> dict:
    import copy
    import json

    if BLOCK_TYPES_PATH.exists():
        try:
            return json.loads(BLOCK_TYPES_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return copy.deepcopy(STEP_TYPES)


def save_block_types(data: dict) -> None:
    import json

    BLOCK_TYPES_PATH.parent.mkdir(parents=True, exist_ok=True)
    BLOCK_TYPES_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


_BLOCK_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_RESERVED_BLOCK_KEYS = {"inject", "__unknown__"}
FIELD_KINDS = [
    "text", "resolvable", "int", "float", "bool", "select", "combo",
    "criteria", "list_resolvable", "store_as",
]


@app.get("/api/block-types")
def api_block_types_list():
    """Every block type currently known -- built-in defaults plus
    whatever's been added/edited/removed through the Blocks tab (see
    load_block_types). Distinct from /api/step-types, which also layers
    in SQL-verb and base-file sugar pills -- this is just the editable
    source table those are layered on top of."""
    return jsonify({"block_types": load_block_types(), "field_kinds": FIELD_KINDS})


@app.post("/api/block-types/<key>")
def api_block_type_save(key):
    if not _BLOCK_KEY_RE.match(key):
        return jsonify({"error": "key must start with a lowercase letter and contain only lowercase letters, digits, or underscore"}), 400
    if key in _RESERVED_BLOCK_KEYS or key.startswith("verb__") or key.startswith("base__"):
        return jsonify({"error": f"{key!r} is a reserved key"}), 400

    body = request.get_json(force=True)
    fields = body.get("fields") or []
    for f in fields:
        if not f.get("name") or f.get("kind") not in FIELD_KINDS:
            return jsonify({"error": f"invalid field spec: {f!r}"}), 400

    spec = {
        "section": (body.get("section") or "Custom Blocks").strip() or "Custom Blocks",
        "description": body.get("description", ""),
        "container": bool(body.get("container", False)),
        "fields": fields,
    }
    data = load_block_types()
    data[key] = spec
    save_block_types(data)
    return jsonify({"saved": True, "key": key})


@app.delete("/api/block-types/<key>")
def api_block_type_delete(key):
    data = load_block_types()
    if key not in data:
        return jsonify({"error": f"No such block type: {key!r}"}), 404
    del data[key]
    save_block_types(data)
    return jsonify({"deleted": True, "key": key})


def build_full_step_types() -> dict:
    return {
        **load_block_types(),
        **build_verb_step_types(list_verbs()),
        **build_base_step_types(list_base_files()),
    }


def _lint_all_yaml_files() -> list[tuple[str, Path]]:
    """Every flows/*.yaml and flows/base/*.yaml, write-dir-wins deduped
    like _list_flow_dir -- but returning the actual Path (not just
    metadata) since lint_flow needs to read+parse each one."""
    entries = []
    by_name: dict[str, Path] = {}
    for base_dir in (FLOWS_DIR, FLOWS_WRITE_DIR):
        if not base_dir.exists():
            continue
        for path in sorted(base_dir.glob("*.yaml")):
            by_name[path.name] = path
    for name, path in sorted(by_name.items()):
        entries.append((f"flows/{name}", path))

    by_name = {}
    for base_dir in (FLOWS_DIR / "base", FLOWS_BASE_WRITE_DIR):
        if not base_dir.exists():
            continue
        for path in sorted(base_dir.glob("*.yaml")):
            by_name[path.name] = path
    for name, path in sorted(by_name.items()):
        entries.append((f"flows/base/{name}", path))

    return entries


def lint_flow_steps(
    steps: list, known_base_injects: dict, known_queries: set, known_step_types: set
) -> list[str]:
    """Static checks over one flow's raw step list (recursing into every
    container -- group/loop/base's 'steps', and a base call's 'injects'
    lists) -- catches the "renamed/deleted something, forgot to update
    every caller" class of bug (a dangling base/query reference) before
    it's hit mid-run, plus the same empty-group/loop check
    validate_blocks does for the Flow Editor's own block form. Returns a
    list of human-readable problem strings, empty if none."""
    problems = []

    def walk(step_list):
        for step in step_list or []:
            if not isinstance(step, dict):
                continue
            step_type = step.get("type")
            if step_type not in known_step_types and step_type != "inject":
                problems.append(f"unknown step type {step_type!r}")
                continue

            if step_type in ("group", "loop") and not step.get("steps"):
                problems.append(f"{step_type} has no nested steps")

            if step_type == "base":
                name = step.get("name")
                if name not in known_base_injects:
                    problems.append(f"base: references missing file flows/base/{name}")
                else:
                    valid_points = {p for p in known_base_injects[name] if p}
                    for point_name in (step.get("injects") or {}):
                        if point_name not in valid_points:
                            problems.append(
                                f"base: {name!r} has no inject point named {point_name!r} "
                                f"(has: {sorted(valid_points) or 'none'})"
                            )
                for inject_steps in (step.get("injects") or {}).values():
                    walk(inject_steps)

            if step_type == "query" and step.get("name") not in known_queries:
                problems.append(f"query: references missing queries/{step.get('name')}.sql")

            if step_type == "assert_sql" and step.get("name") and not step.get("query"):
                if step["name"] not in known_queries:
                    problems.append(f"assert_sql: references missing queries/{step['name']}.sql")

            walk(step.get("steps") or [])

    walk(steps)
    return problems


@app.get("/api/lint")
def api_lint():
    """Dry-run/lint pass over every flow AND base file on disk -- just
    statically checks every step's references (base file/query names,
    empty group/loop) are actually valid right now. Meant to catch
    "renamed a query, forgot the 3 flows using it" before someone hits it
    later."""
    known_base_injects = {bf["filename"]: bf["inject_points"] for bf in list_base_files()}
    known_queries = set()
    if QUERIES_DIR.is_dir():
        known_queries.update(p.stem for p in QUERIES_DIR.glob("*.sql"))
    if QUERIES_WRITE_DIR.is_dir():
        known_queries.update(p.stem for p in QUERIES_WRITE_DIR.glob("*.sql"))
    if VERBS_DIR.is_dir():
        known_queries.update(p.stem for p in VERBS_DIR.glob("*.sql"))
    known_step_types = set(load_block_types().keys())

    results = []
    for rel, path in _lint_all_yaml_files():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            results.append({"path": rel, "problems": [f"could not parse YAML: {exc}"]})
            continue
        problems = lint_flow_steps(data.get("steps") or [], known_base_injects, known_queries, known_step_types)
        if problems:
            results.append({"path": rel, "problems": problems})

    return jsonify({"results": results, "checked": len(_lint_all_yaml_files())})


@app.get("/api/base-files/<filename>/used-by")
def api_base_file_used_by(filename):
    """Every flow/base file whose steps actually reference base file
    <filename> (via {type: base, name: <filename>}, at any nesting depth
    -- see references_base_file) -- shown in the Flow Editor while
    editing a base file so renaming/deleting it doesn't silently break
    something else that calls it."""
    usages = []
    for flow_meta in list_available_flows():
        rel = flow_meta["path"].removeprefix("flows/")
        path = _resolve_flow_path(rel)
        if path is None:
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if references_base_file(data.get("steps") or [], filename):
            usages.append({"path": flow_meta["path"], "name": flow_meta["name"]})

    for base_meta in list_base_files():
        if base_meta["filename"] == filename:
            continue
        path = _resolve_flow_path(f"base/{base_meta['filename']}")
        if path is None:
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if references_base_file(data.get("steps") or [], filename):
            usages.append({"path": f"base/{base_meta['filename']}", "name": base_meta["label"]})

    return jsonify({"usages": usages})


@app.post("/api/queries/<name>/promote-verb")
def api_query_promote_verb(name):
    """Copies queries/<name>.sql's current text into queries/verbs/ --
    the query still exists (and is still usable via the generic "query"
    step's action dropdown) on top of also being its own pill now."""
    if not _QUERY_NAME_RE.match(name):
        return jsonify({"error": "invalid query name"}), 400
    path = _resolve_query_path(name)
    if path is None:
        return jsonify({"error": f"No such query: {name!r}"}), 404
    VERBS_DIR.mkdir(parents=True, exist_ok=True)
    (VERBS_DIR / f"{name}.sql").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return jsonify({"promoted": True, "name": name})


@app.delete("/api/verbs/<name>")
def api_verb_delete(name):
    """Un-promotes a verb -- only removes it from queries/verbs/, the
    underlying queries/<name>.sql (if any) is untouched."""
    if not _QUERY_NAME_RE.match(name):
        return jsonify({"error": "invalid verb name"}), 400
    path = VERBS_DIR / f"{name}.sql"
    if not path.is_file():
        return jsonify({"error": f"No such verb: {name!r}"}), 404
    path.unlink()
    meta_path = _verb_meta_path(name)
    if meta_path.is_file():
        meta_path.unlink()
    return jsonify({"deleted": True, "name": name})


@app.get("/api/verbs")
def api_verbs_list():
    """Every verb's {name, label, section, sql}, for the Verb Editor
    tab -- label/section are "" when not overridden (see load_verb_meta),
    matching what the palette pill falls back to."""
    verbs = []
    for info in list_verbs():
        path = VERBS_DIR / f"{info['name']}.sql"
        sql = path.read_text(encoding="utf-8") if path.is_file() else ""
        verbs.append({**info, "sql": sql})
    return jsonify({"verbs": verbs})


@app.post("/api/verbs/<name>")
def api_verb_save(name):
    """Saves a verb's Verb Editor state in one call: its underlying SQL
    (queries/verbs/<name>.sql -- the same file the "query" step's action
    dropdown and the Queries tab both read) plus its label/section
    palette overrides (the .meta.json sidecar)."""
    if not _QUERY_NAME_RE.match(name):
        return jsonify({"error": "invalid verb name"}), 400
    path = VERBS_DIR / f"{name}.sql"
    if not path.is_file():
        return jsonify({"error": f"No such verb: {name!r}"}), 404
    body = request.get_json(force=True)
    sql = body.get("sql")
    if sql is not None:
        path.write_text(sql, encoding="utf-8")
        # Keep the plain queries/<name>.sql file (if any) in sync too --
        # a verb is meant to still be usable as a generic "query" step,
        # so its SQL shouldn't silently fork between the two locations.
        plain_path = QUERIES_WRITE_DIR / f"{name}.sql"
        if plain_path.is_file():
            plain_path.write_text(sql, encoding="utf-8")
    save_verb_meta(name, (body.get("label") or "").strip(), (body.get("section") or "").strip())
    return jsonify({"saved": True, "name": name})


def _parse_query_params(sql_text: str) -> list[str]:
    """Pulls the ordered list of '?' placeholder names out of a
    queries/*.sql file, by scanning for its own DECLARE @Name ... = ?
    lines (the convention every query under queries/ already follows --
    each positional '?' is declared once, in call order, right before
    it's used). A DECLARE whose value comes from a subquery instead of a
    caller-supplied '?' (e.g. `= (SELECT ...)`) is correctly skipped,
    since '= ?' only matches the literal placeholder form."""
    return [m.group(1) for m in re.finditer(r"@(\w+)\b[^\n=]*=\s*\?", sql_text)]


def _prettify_param_name(name: str) -> str:
    """'ProductId' -> 'Product Id' -- splits on internal capital letters
    so a PascalCase SQL param name reads as a UI label."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", name)


@app.get("/api/queries/<name>/params")
def api_query_params(name):
    path = _resolve_query_path(name)
    if path is None:
        return jsonify({"params": []})
    names = _parse_query_params(path.read_text(encoding="utf-8"))
    return jsonify({"params": [{"name": n, "label": _prettify_param_name(n)} for n in names]})


@app.post("/api/parse-sql-params")
def api_parse_sql_params():
    """Same placeholder-name parsing as /api/queries/<name>/params, but for
    SQL typed directly into an inline query field (assert_sql's "Type SQL"
    mode, or a verb saved with inline SQL) rather than a queries/*.sql file."""
    sql_text = (request.get_json(silent=True) or {}).get("sql", "")
    names = _parse_query_params(sql_text)
    return jsonify({"params": [{"name": n, "label": _prettify_param_name(n)} for n in names]})


# ---------------------------------------------------------------------
# DB connections + Run (Queries tab) -- see engine/db.py and
# config/connections.json's own header comment for the plaintext
# storage tradeoff.
# ---------------------------------------------------------------------


def load_connections() -> list[dict]:
    import json

    if not CONNECTIONS_PATH.exists():
        return []
    try:
        data = json.loads(CONNECTIONS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def save_connections(connections: list[dict]) -> None:
    import json

    CONNECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONNECTIONS_PATH.write_text(json.dumps(connections, indent=2), encoding="utf-8")


def _find_connection(name: str) -> dict | None:
    return next((c for c in load_connections() if c.get("name") == name), None)


@app.get("/api/connections")
def api_connections_list():
    return jsonify({"connections": load_connections()})


@app.post("/api/connections")
def api_connections_save():
    """Adds a new connection, or replaces an existing one of the same
    name -- the request body IS the full connection {"name",
    "connection_string"}, not a partial patch."""
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    connection_string = body.get("connection_string") or ""
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not connection_string:
        return jsonify({"error": "connection_string is required"}), 400

    connections = [c for c in load_connections() if c.get("name") != name]
    connections.append({"name": name, "connection_string": connection_string})
    save_connections(connections)
    return jsonify({"saved": True, "name": name})


@app.delete("/api/connections/<name>")
def api_connections_delete(name):
    connections = load_connections()
    remaining = [c for c in connections if c.get("name") != name]
    if len(remaining) == len(connections):
        return jsonify({"error": f"No such connection: {name!r}"}), 404
    save_connections(remaining)
    return jsonify({"deleted": True, "name": name})


@app.post("/api/connections/<name>/test")
def api_connections_test(name):
    from engine.db import test_connection

    conn = _find_connection(name)
    if conn is None:
        return jsonify({"error": f"No such connection: {name!r}"}), 404
    try:
        test_connection(conn["connection_string"])
    except Exception as exc:  # pyodbc.Error and friends -- surfaced as-is
        return jsonify({"ok": False, "error": str(exc)})
    return jsonify({"ok": True})


def _jsonify_cell(value):
    """pyodbc rows can carry datetime/date/Decimal/bytes/etc -- none of
    which jsonify() can serialize as-is. Converts each to the closest
    JSON-safe representation; anything already JSON-safe (str/int/
    float/bool/None) passes through unchanged."""
    import datetime
    import decimal

    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return value


@app.post("/api/run-sql")
def api_run_sql():
    """Runs SQL typed directly in the request body against a named
    connection -- used by the Queries/Verbs tabs' Run button so it
    executes exactly what's currently in the editor, saved or not,
    rather than forcing a save first."""
    from engine.db import run_query

    body = request.get_json(force=True)
    sql = body.get("sql") or ""
    connection_name = body.get("connection") or ""
    params = body.get("params") or []

    if not sql.strip():
        return jsonify({"error": "No SQL to run"}), 400
    conn = _find_connection(connection_name)
    if conn is None:
        return jsonify({"error": f"No such connection: {connection_name!r}"}), 400

    try:
        result = run_query(conn["connection_string"], sql, params)
    except Exception as exc:  # pyodbc.Error and friends -- surfaced as-is
        return jsonify({"error": str(exc)}), 400

    result["rows"] = [[_jsonify_cell(v) for v in row] for row in result["rows"]]
    return jsonify(result)


if __name__ == "__main__":
    # debug=False and no reloader: the reloader re-execs the frozen exe
    # (or this script) as a subprocess to watch for file changes, which
    # is pointless for a shipped exe and would just open the app twice.
    if getattr(sys, "frozen", False):
        threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5000/")).start()
    app.run(debug=False, use_reloader=False, port=5000)
