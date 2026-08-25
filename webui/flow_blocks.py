"""
webui/flow_blocks.py
Converts between a flow's on-disk YAML shape (a dict with a `steps:`
list, each step a dict with a `type:` key -- see engine/flow_runner.py)
and a JSON "block tree" shape the Flow Editor's frontend renders as
draggable blocks.

STEP_TYPES is the single source of truth for both directions: it is
consulted here for step_to_block/block_to_step, and it is shipped to the
frontend as-is (via `tojson` at page-render time) to drive block
rendering/field-collection there. Keeping one table instead of a
Python-side and a JS-side copy is what keeps the two conversions from
drifting apart as step types are added or changed here and in
engine/flow_runner.py.

Field `kind`s:
    text              -- plain string, never {from:}
    resolvable        -- string/number that may be a literal or {from: "key"}
    int / float       -- plain number, never {from:}
    bool              -- plain boolean, never {from:}
    select            -- one of a fixed/dynamic set of string choices
    combo             -- like select, but the value can also be freely
                          typed (a suggestions dropdown, not a hard
                          enum) -- used for control_class, since any
                          Android widget class is technically valid
    criteria          -- the shared text/starts_with/contains "which
                          element" trio (exactly one is set)
    list_resolvable   -- a list of resolvable values (params, equals,
                          not_equals)
    store_as          -- a plain string, or (only where allow_multi) a
                          list of plain strings -- never {from:}, since
                          store_as is always used as a dict KEY.

A field spec may also set `virtual: True` -- such a field is never read
from or written to the step dict; it exists purely so its value can
drive another field's `show_when` (e.g. wait's "mode" toggles whether
seconds or text/timeout is shown, derived from which key the loaded
step actually has -- see _field_value_for_block/_field_value_for_step).

Container step types (group, loop, base) additionally carry `children`:
a list of nested blocks. Every other step type has an empty children
list -- this key always exists on a block for a uniform shape, even
though only containers give it meaning.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# STEP_TYPES table
# ---------------------------------------------------------------------------

CONTROL_CLASS_CHOICES = [
    "android.widget.Button",
    "android.widget.EditText",
    "android.widget.TextView",
    "android.widget.CheckBox",
    "android.widget.ImageView",
    "android.widget.ImageButton",
]

# Friendlier display names for the dropdown -- the raw android.widget.*
# class still goes into the YAML unchanged (see kind: "select" on
# control_class below), this only changes what the editor shows for it.
CONTROL_CLASS_LABELS = {
    "android.widget.Button": "Button",
    "android.widget.EditText": "Text field",
    "android.widget.TextView": "Text label",
    "android.widget.CheckBox": "Checkbox",
    "android.widget.ImageView": "Image",
    "android.widget.ImageButton": "Image button",
}

# Android keycode names accepted by press_key. Letters/digits/nav/special
# keys covers the common cases; anything else can still be typed by hand
# since the "key" field is a combo (searchable, free-text-allowed) box.
PRESS_KEY_CHOICES = (
    [chr(c) for c in range(ord("A"), ord("Z") + 1)]
    + [str(d) for d in range(10)]
    + [
        "ENTER", "TAB", "SPACE", "ESCAPE", "DEL", "FORWARD_DEL", "BACKSPACE",
        "HOME", "BACK", "MENU", "SEARCH", "APP_SWITCH",
        "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_CENTER",
        "MOVE_HOME", "MOVE_END", "PAGE_UP", "PAGE_DOWN",
        "VOLUME_UP", "VOLUME_DOWN", "VOLUME_MUTE", "POWER", "CAMERA",
        "CALL", "ENDCALL",
        "PLUS", "MINUS", "EQUALS", "STAR", "POUND", "AT", "COMMA", "PERIOD",
        "SLASH", "BACKSLASH", "SEMICOLON", "APOSTROPHE", "GRAVE",
        "LEFT_BRACKET", "RIGHT_BRACKET",
    ]
)

STEP_TYPES: dict[str, dict[str, Any]] = {
    # --- Navigation / interaction ---------------------------------------
    "tap": {
        "section": "Navigation / interaction",
        "description": "Taps an on-screen element matched by its text.",
        "container": False,
        "fields": [
            {"name": "text", "kind": "criteria", "help": "Which on-screen text to find and tap. Choose exact, starts-with, or contains matching."},
            {"name": "below", "kind": "resolvable", "optional": True, "help": "Only match an element below this label, for when the same text appears in more than one place on screen."},
            {
                "name": "control_class", "label": "element type", "kind": "select", "optional": True,
                # "default" must match _step_tap's own hardcoded fallback
                # (see engine/flow_runner.py) -- it's what a brand-new
                # step round-trips as if this key is left out entirely.
                # "new_default" is UI-only: which choice a freshly dragged
                # tap block starts on, independent of that elision value.
                "default": "android.widget.Button", "new_default": "android.widget.EditText",
                "choices": CONTROL_CLASS_CHOICES, "choice_labels": CONTROL_CLASS_LABELS,
                "help": "The type of Android UI element to look for (Button, Text field, etc.) -- narrows the search when plain text matching alone would be ambiguous.",
            },
            {"name": "occurrence", "label": "match #", "kind": "int", "optional": True, "default": 1, "help": "If more than one matching element exists, which one to tap -- 1 is the first."},
        ],
    },
    "back": {
        "section": "Navigation / interaction",
        "description": "Presses the Android hardware/software Back button once.",
        "container": False, "fields": [],
    },
    "wait": {
        "section": "Navigation / interaction",
        "description": "Pauses the test, either for a fixed number of seconds or until some text shows up on screen.",
        "container": False,
        "fields": [
            {
                "name": "mode", "kind": "select", "choices": ["seconds", "text"], "default": "seconds",
                "choice_labels": {"seconds": "Fixed delay", "text": "Wait for text"}, "virtual": True,
                "help": "Pause for a fixed number of seconds, or wait until some text appears on screen.",
            },
            {"name": "seconds", "kind": "float", "optional": True, "show_when": {"field": "mode", "equals": "seconds"}, "help": "How long to pause, in seconds."},
            {"name": "text", "kind": "criteria", "optional": True, "show_when": {"field": "mode", "equals": "text"}, "help": "The text to wait for before continuing."},
            {"name": "timeout", "kind": "float", "optional": True, "show_when": {"field": "mode", "equals": "text"}, "help": "How long to wait for the text before giving up and failing this step."},
        ],
    },
    "scroll": {
        "section": "Navigation / interaction",
        "description": "Swipes the screen a fixed distance/direction, without looking for anything in particular.",
        "container": False,
        "fields": [
            {"name": "direction", "kind": "select", "choices": ["down", "up", "left", "right"], "default": "down", "help": "Which way to swipe the screen."},
            {
                "name": "percent", "label": "distance %", "kind": "float", "optional": True, "default": 0.75, "display_scale": 100,
                "help": "How far each swipe travels, as a percentage of the screen's height.",
            },
            {"name": "times", "kind": "int", "optional": True, "default": 1, "help": "How many swipes to perform."},
            {
                "name": "start",
                "kind": "select",
                "choices": ["top", "top_middle", "middle", "bottom_middle", "bottom"],
                "default": "middle",
                "help": "Which vertical band of the screen the swipe's touch points stay within -- starting too near the top can be misread as pulling down the notification shade instead of scrolling.",
            },
        ],
    },
    "scroll_to": {
        "section": "Navigation / interaction",
        "description": "Scrolls down repeatedly until specific text appears on screen, then stops.",
        "container": False,
        "fields": [{"name": "text", "kind": "resolvable", "help": "Scrolls down until this text appears on screen, then stops -- prefer this over a fixed Scroll when you know what you're looking for."}],
    },
    "hide_keyboard": {
        "section": "Navigation / interaction",
        "description": "Dismisses the on-screen keyboard, if it's currently showing.",
        "container": False, "fields": [],
    },
    "press_key": {
        "section": "Navigation / interaction",
        "description": "Sends a single Android keycode (a letter, digit, or special key like ENTER/BACK/HOME).",
        "container": False,
        "fields": [{
            "name": "key", "kind": "combo", "choices": PRESS_KEY_CHOICES,
            "help": "The Android keycode to send, e.g. ENTER, BACK, or a single character. Pick from the list or type your own.",
        }],
    },
    "close_app": {
        "section": "Navigation / interaction",
        "description": "Force-closes an app -- the one under test by default, or another package if named.",
        "container": False,
        "fields": [{"name": "package", "kind": "text", "optional": True, "help": "The Android package name to close. Leave blank to close the app currently under test."}],
    },
    "select_modal": {
        "section": "Navigation / interaction",
        "description": "Chooses an option from an already-open dropdown/modal by its text.",
        "container": False,
        "fields": [
            {"name": "select", "kind": "resolvable", "help": "The option text to choose from the open dropdown/modal."},
            {"name": "exact", "kind": "bool", "optional": True, "default": False, "help": "Require an exact text match instead of a partial one."},
            {"name": "tap_if_closed", "kind": "tap_if_closed", "optional": True, "help": "If the dropdown isn't already open, tap this element first to open it."},
            {"name": "store_as", "kind": "store_as", "optional": True, "help": "Save the selected value under this name so a later step can reference it with {from: name}."},
        ],
    },

    # --- Reading / entering text -----------------------------------------
    "enter_text": {
        "section": "Reading / entering",
        "description": "Types text into the input field found below a given label -- either a literal value or one pulled from a SQL query.",
        "container": False,
        "fields": [
            {"name": "below", "kind": "text", "help": "The field's label -- text is typed into whatever input sits below it."},
            {
                "name": "source", "kind": "select", "choices": ["literal", "sql"], "default": "literal",
                "choice_labels": {"literal": "From text", "sql": "From SQL query"},
                "help": "Type a literal value, or fetch one from a SQL query first.",
            },
            {"name": "value", "kind": "resolvable", "optional": True, "show_when": {"field": "source", "equals": "literal"}, "help": "The literal text to type."},
            {"name": "query", "kind": "text", "optional": True, "show_when": {"field": "source", "equals": "sql"}, "help": "A SQL query whose first column's value gets typed in."},
            {"name": "params", "kind": "list_resolvable", "optional": True, "show_when": {"field": "source", "equals": "sql"}, "help": "Values substituted for each '?' placeholder in the query, in order."},
            {"name": "submit", "kind": "bool", "optional": True, "default": False, "help": "Press Enter/submit immediately after typing."},
            {"name": "occurrence", "label": "match #", "kind": "int", "optional": True, "default": 1, "help": "If more than one matching field exists, which one to type into -- 1 is the first."},
            {"name": "store_as", "kind": "store_as", "optional": True, "show_when": {"field": "source", "equals": "sql"}, "help": "Save the value that was typed under this name so a later step can reference it."},
        ],
    },
    "read_text": {
        "section": "Reading / entering",
        "description": "Reads on-screen text (found directly or below a label) and saves it for later steps to use.",
        "container": False,
        "fields": [
            {"name": "store_as", "kind": "store_as", "help": "Save the text that gets read under this name."},
            {"name": "below", "kind": "text", "optional": True, "help": "Read the text found below this label."},
            {"name": "text", "kind": "criteria", "optional": True, "help": "Instead of Below, find the text to read by matching it directly."},
            {"name": "extract", "kind": "text", "optional": True, "help": "A regex pattern -- only the matched portion of the read text is stored, not the whole thing."},
        ],
    },
    "verify_alive": {
        "section": "Reading / entering",
        "description": "Confirms the app hasn't crashed and is showing SOME coherent screen -- doesn't check for a specific one (use Require Screen for that).",
        "container": False,
        "fields": [{"name": "label", "kind": "text", "optional": True, "help": "A note included in the report row, purely for readability."}],
    },
    "get_plant": {
        "section": "Reading / entering",
        "description": "Reads the current plant name shown in the app's banner and saves it.",
        "container": False,
        "fields": [
            {"name": "store_as", "kind": "store_as", "optional": True, "default": "plant", "help": "Save the current plant name under this name."},
            {"name": "contains", "kind": "text", "optional": True, "help": "Only match a plant banner containing this substring."},
        ],
    },
    "read_sql": {
        "section": "Reading / entering",
        "description": "Runs SQL typed directly here and saves column(s) from its first result row.",
        "container": False,
        "fields": [
            {"name": "query", "kind": "text", "help": "The SQL to run, written inline here. Use '?' for each positional parameter."},
            {"name": "store_as", "kind": "store_as", "allow_multi": True, "help": "Column(s) of the first returned row to save -- one name stores column 0; several names store that many columns in order."},
            {"name": "params", "kind": "list_resolvable", "optional": True, "help": "Values substituted for each '?' placeholder in the query, in order."},
        ],
    },
    "query": {
        "section": "Reading / entering",
        "description": "Runs a saved SQL file from queries/ and saves column(s) from its first result row.",
        "container": False,
        "fields": [
            {"name": "name", "label": "action", "kind": "select", "choices": [], "dynamic_choices": "queries", "help": "Which SQL file under queries/ to run."},
            {"name": "store_as", "kind": "store_as", "allow_multi": True, "help": "Column(s) of the first returned row to save -- one name stores column 0; several names store that many columns in order."},
            {"name": "params", "kind": "list_resolvable", "optional": True, "help": "Values substituted for each '?' placeholder in the query, in order."},
        ],
    },

    # --- Assertions -------------------------------------------------------
    "assert_visible": {
        "section": "Assertions",
        "description": "Fails the test unless matching text is currently visible on screen.",
        "container": False,
        "fields": [
            {"name": "text", "kind": "criteria", "help": "Fails this step if the text doesn't appear on screen."},
            {"name": "timeout", "kind": "float", "optional": True, "default": 10, "help": "How long to wait for the text to appear before failing."},
        ],
    },
    "assert_absent": {
        "section": "Assertions",
        "description": "Fails the test if matching text IS visible on screen -- the opposite of Assert Visible.",
        "container": False,
        "fields": [
            {"name": "text", "kind": "criteria", "help": "Fails this step if the text IS visible on screen -- the opposite of Assert Visible."},
            {"name": "timeout", "kind": "float", "optional": True, "default": 2, "help": "How long to wait/confirm the text stays absent before passing."},
        ],
    },
    "assert_sql": {
        "section": "Assertions",
        "description": "Runs SQL (typed here or from a saved query) and fails the test unless its results match the conditions given.",
        "container": False,
        "fields": [
            {
                "name": "source", "kind": "select", "choices": ["inline", "file"], "new_default": "file",
                "choice_labels": {"inline": "Type SQL", "file": "Saved query"}, "virtual": True,
                "help": "Write the SQL right here, or run one of the saved queries under queries/.",
            },
            {"name": "query", "kind": "text", "optional": True, "show_when": {"field": "source", "equals": "inline"}, "help": "Inline SQL to run for this check."},
            {"name": "name", "kind": "select", "choices": [], "dynamic_choices": "queries", "optional": True, "show_when": {"field": "source", "equals": "file"}, "help": "Which saved SQL file under queries/ to run."},
            {"name": "params", "kind": "list_resolvable", "optional": True, "help": "Values substituted for each '?' placeholder in the query, in order."},
            {"name": "min_rows", "kind": "int", "optional": True, "default": 1, "help": "Fails unless the query returns at least this many rows."},
            {"name": "equals", "kind": "list_resolvable", "optional": True, "help": "Fails unless the first row's value(s) match these exactly."},
            {"name": "not_equals", "kind": "list_resolvable", "optional": True, "help": "Fails if the first row's value(s) match these."},
            {"name": "timeout", "kind": "float", "optional": True, "default": 0, "help": "If nonzero, keeps re-running the query for up to this many seconds until the assertion passes, instead of checking just once."},
            {"name": "poll", "kind": "float", "optional": True, "default": 1.0, "help": "How often (seconds) to re-run the query while waiting, when Timeout is set."},
        ],
    },
    "assert_equals": {
        "section": "Assertions",
        "description": "Fails the test unless two values (literal or from an earlier step) match.",
        "container": False,
        "fields": [
            {"name": "a", "kind": "resolvable", "help": "First value to compare -- typed literally or pulled from an earlier step's stored result."},
            {"name": "b", "kind": "resolvable", "help": "Second value to compare -- typed literally or pulled from an earlier step's stored result."},
            {"name": "not", "kind": "bool", "optional": True, "default": False, "help": "Invert the comparison -- fail if they DO match instead of if they don't."},
        ],
    },
    "require_screen": {
        "section": "Assertions",
        "description": "Fails the test unless the app's current screen title exactly matches the one given.",
        "container": False,
        "fields": [{"name": "title", "kind": "text", "help": "Fails this step unless the app's current screen title matches this text."}],
    },

    # --- Auditing -----------------------------------------------------------
    "screenshot": {
        "section": "Auditing",
        "description": "Captures a screenshot of the current screen for the test report.",
        "container": False,
        "fields": [{"name": "label", "kind": "text", "optional": True, "default": "screenshot", "help": "Filename label for the saved screenshot."}],
    },
    "list_text": {
        "section": "Auditing",
        "description": "Dumps every piece of visible text on screen into the report, for debugging/auditing what's actually there.",
        "container": False,
        "fields": [
            {
                "name": "start",
                "kind": "select",
                "choices": ["top", "top_middle", "middle", "bottom_middle", "bottom"],
                "default": "middle",
                "help": "Which vertical band of the screen to start reading visible text from.",
            }
        ],
    },
    "back_until": {
        "section": "Auditing",
        "description": "Repeatedly presses Back until a target screen title is reached, or gives up after too many presses.",
        "container": False,
        "fields": [
            {"name": "title", "kind": "text", "help": "Keeps pressing Back until the screen title matches this text."},
            {"name": "max_presses", "kind": "int", "optional": True, "default": 5, "help": "Give up (and fail) after this many Back presses without reaching the title."},
            {"name": "settle", "kind": "float", "optional": True, "default": 0.5, "help": "Seconds to pause after each Back press, to let the screen finish transitioning."},
        ],
    },

    # --- Composition ----------------------------------------------------
    "group": {
        "section": "Composition",
        "description": "A plain container -- runs its nested steps in order, purely for organizing/grouping them visually.",
        "container": True, "fields": [],
    },
    "loop": {
        "section": "Composition",
        "description": "Runs its nested steps repeatedly, a fixed number of times.",
        "container": True,
        "fields": [
            {"name": "times", "kind": "int", "optional": True, "default": 1, "help": "How many times to repeat the nested steps."},
            {"name": "cycle_delay", "kind": "float", "optional": True, "default": 0, "help": "Seconds to pause between each repetition."},
        ],
    },
    "base": {
        "section": "Composition",
        "description": "Includes a shared flows/base/*.yaml file's steps here, as if they were pasted in directly -- lets multiple flows reuse the same sequence.",
        "container": True,
        "fields": [{
            "name": "name", "kind": "select", "choices": [], "dynamic_choices": "base_files",
            "help": "Which flows/base/*.yaml file to include -- its steps run here as if pasted in directly.",
        }],
    },
    "scan": {
        "section": "Composition",
        "description": "A composite step: types a value into a field, submits it, and takes a screenshot -- combines Enter Text + Press Key + Screenshot into one block.",
        "container": False,
        "fields": [
            {"name": "below", "kind": "text", "help": "The field's label -- text is typed into whatever input sits below it, then submitted."},
            {
                "name": "control_class", "label": "element type", "kind": "select", "optional": True,
                "default": "android.widget.EditText", "choices": CONTROL_CLASS_CHOICES,
                "choice_labels": CONTROL_CLASS_LABELS,
                "help": "The type of Android UI element to look for below the label.",
            },
            {
                "name": "source", "kind": "select", "choices": ["literal", "sql"], "default": "literal",
                "choice_labels": {"literal": "From text", "sql": "From SQL query"},
                "help": "Type a literal value, or fetch one from a SQL query first.",
            },
            {"name": "value", "kind": "resolvable", "optional": True, "show_when": {"field": "source", "equals": "literal"}, "help": "The literal text to type."},
            {"name": "query", "kind": "text", "optional": True, "show_when": {"field": "source", "equals": "sql"}, "help": "A SQL query whose first column's value gets typed in."},
            {"name": "params", "kind": "list_resolvable", "optional": True, "show_when": {"field": "source", "equals": "sql"}, "help": "Values substituted for each '?' placeholder in the query, in order."},
            {"name": "store_as", "kind": "store_as", "optional": True, "show_when": {"field": "source", "equals": "sql"}, "help": "Save the value that was typed under this name so a later step can reference it."},
            {"name": "label", "label": "screenshot label", "kind": "text", "optional": True, "help": "Filename label for the screenshot taken partway through this composite step."},
            {"name": "key", "kind": "combo", "choices": PRESS_KEY_CHOICES, "optional": True, "default": "ENTER", "help": "The key to press to submit/confirm the entered value."},
        ],
    },
}

# Not a real step type dispatched by FlowRunner -- only ever appears
# inside a flows/base/*.yaml file's own steps list, marking where a
# caller's injected steps get spliced in (see engine/flow_runner.py's
# _step_base). Kept out of STEP_TYPES (which mirrors real dispatchable
# step types 1:1) and handled as a special case in the conversion
# functions below.
INJECT_TYPE = "__inject__"


# ---------------------------------------------------------------------------
# step dict <-> block conversion
# ---------------------------------------------------------------------------


def _is_from_ref(value: Any) -> bool:
    return isinstance(value, dict) and "from" in value and len(value) == 1


def _value_to_field(value: Any) -> dict:
    """Wraps a scalar the way _resolve_value in flow_runner.py would
    interpret it: either a literal, or a {from: "key"} reference."""
    if _is_from_ref(value):
        return {"kind": "from", "key": value["from"]}
    return {"kind": "literal", "value": value}


def _field_to_value(field: dict) -> Any:
    if not isinstance(field, dict) or "kind" not in field:
        # Defensive: treat anything malformed as a literal passthrough
        # rather than raising, since block_to_step is also used for
        # brand-new blocks a frontend bug might send half-filled.
        return field
    if field["kind"] == "from":
        return {"from": field["key"]}
    return field.get("value")


def _list_to_field(values: list) -> list[dict]:
    return [_value_to_field(v) for v in (values or [])]


def _field_to_list(values: list) -> list:
    return [_field_to_value(v) for v in (values or [])]


def _comment_to_field(comment: Any) -> dict | None:
    if comment is None:
        return None
    if isinstance(comment, list):
        parts = []
        for part in comment:
            if _is_from_ref(part):
                parts.append({"kind": "from", "key": part["from"]})
            else:
                parts.append({"kind": "text", "value": part})
        return {"kind": "parts", "parts": parts}
    return {"kind": "text", "value": comment}


def _field_to_comment(field: dict | None) -> Any:
    if field is None:
        return None
    if field.get("kind") == "parts":
        parts = []
        for part in field.get("parts", []):
            if part.get("kind") == "from":
                parts.append({"from": part["key"]})
            else:
                parts.append(part.get("value", ""))
        return parts
    return field.get("value")


def _extract_modifiers(step: dict) -> dict:
    """Pulls the universal if_visible/retries/required/comment keys off a
    step dict -- see engine/flow_runner.py's _execute() for exactly how
    each is read and defaulted."""
    if_visible = step.get("if_visible")
    if_visible_block = None
    if if_visible:
        field = next((k for k in ("text", "starts_with", "contains") if k in if_visible), None)
        if field:
            if_visible_block = {"field": field, "value": _value_to_field(if_visible[field])}

    return {
        "if_visible": if_visible_block,
        "if_visible_timeout": step.get("if_visible_timeout") if if_visible_block else None,
        "retries": step.get("retries") or None,
        "retry_delay": step.get("retry_delay") if step.get("retries") else None,
        "required": bool(step.get("required", False)),
        "comment": _comment_to_field(step.get("comment")),
    }


def _apply_modifiers(step: dict, modifiers: dict) -> None:
    """Inverse of _extract_modifiers -- writes universal modifier keys
    back onto a step dict, omitting any at their handler default so
    saved YAML doesn't carry clutter keys (matches how hand-written
    flows look today, e.g. a step with no retries just has no
    retries: key at all)."""
    if_visible = modifiers.get("if_visible")
    if if_visible:
        step["if_visible"] = {if_visible["field"]: _field_to_value(if_visible["value"])}
        timeout = modifiers.get("if_visible_timeout")
        if timeout is not None:
            step["if_visible_timeout"] = timeout

    retries = modifiers.get("retries")
    if retries:
        step["retries"] = retries
        retry_delay = modifiers.get("retry_delay")
        if retry_delay is not None:
            step["retry_delay"] = retry_delay

    if modifiers.get("required"):
        step["required"] = True

    comment = _field_to_comment(modifiers.get("comment"))
    if comment is not None:
        step["comment"] = comment


def _field_value_for_block(spec: dict, step: dict) -> Any:
    """Reads one STEP_TYPES field spec's value off a raw step dict into
    its block-JSON shape, based on the field's kind."""
    name = spec["name"]
    kind = spec["kind"]

    if spec.get("virtual"):
        # A UI-only toggle, never itself read from/written to the step
        # dict -- e.g. wait's "mode" derives which of seconds/text this
        # step actually uses purely to drive those fields' show_when,
        # not because 'mode' is a real key FlowRunner looks at.
        if name == "mode":
            return "seconds" if "seconds" in step else "text"
        if name == "source":
            # assert_sql accepts either inline 'query' SQL or a saved
            # 'name' -- whichever key the loaded step actually has wins,
            # so an existing flow reopens on the right side of the toggle.
            return "file" if step.get("name") else "inline"
        return spec.get("default")

    if spec.get("display_scale"):
        # e.g. scroll's percent -- Appium's scrollGesture wants a 0-1
        # fraction (see pages/base_page.py's scroll_screen), but a human
        # reads that as a percentage, so the editor shows raw * scale
        # (0.75 -> 75) and _field_value_for_step divides back out on save.
        raw = step.get(name, spec.get("default"))
        return raw * spec["display_scale"] if raw is not None else None

    if kind == "criteria":
        for key in ("text", "starts_with", "contains"):
            if key in step:
                return {"field": key, "value": _value_to_field(step[key])}
        return None
    if kind == "resolvable":
        if name == "value" and name not in step and "text" in step:
            # scan/enter_text historically accepted either 'value' or
            # 'text' as the literal-source key (see _step_enter_text) --
            # the editor only ever writes 'value' back out, so a flow
            # hand-written (or previously saved) with 'text' still loads
            # into the same single field here.
            return _value_to_field(step["text"])
        if name not in step:
            return None
        return _value_to_field(step[name])
    if kind == "list_resolvable":
        if name not in step:
            return None
        raw = step[name]
        # equals/not_equals accept either a bare scalar or a list --
        # normalize to a list of one for the editor, block_to_step
        # collapses a single-element list back to a bare scalar to
        # match what assert_sql actually expects there.
        if not isinstance(raw, list):
            raw = [raw]
        return _list_to_field(raw)
    if kind == "store_as":
        return step.get(name)
    if kind == "tap_if_closed":
        fallback = step.get("tap_if_closed")
        if not fallback:
            return None
        return {
            "below": fallback.get("below"),
            "control_class": fallback.get("control_class", "android.widget.Button"),
            "occurrence": fallback.get("occurrence", 1),
        }
    if kind in ("text", "int", "float", "bool", "select"):
        return step.get(name, spec.get("default"))
    return step.get(name)


def _show_when_active(field_spec: dict, fields: dict) -> bool:
    """True unless field_spec has a show_when whose controller field
    (e.g. assert_sql's "source": inline/file, wait's "mode": seconds/
    text) currently points somewhere else -- see _field_value_for_step's
    caller. The Flow Editor's own show_when only ever hides a field's row
    (row.style.display = "none"); it never clears that row's underlying
    input value when the user switches away from it. Left unguarded here,
    an assert_sql block edited from "Type SQL" to "Saved query" (or vice
    versa) would silently carry the OTHER branch's stale value straight
    into the saved YAML -- e.g. both `query:` and `name:` ending up set
    on the same step, with `name:` winning at runtime while the `query:`
    the user actually meant to run is silently ignored. Treating an
    inactive branch's fields as absent here, at the single point every
    save path converges on, closes that off at the source instead of
    trying to keep every input's DOM state in sync with every possible
    toggle."""
    show_when = field_spec.get("show_when")
    if not show_when:
        return True
    return fields.get(show_when["field"]) == show_when["equals"]


def _field_value_for_step(spec: dict, value: Any, step: dict) -> None:
    """Inverse of _field_value_for_block -- writes one field's
    block-JSON value back onto a raw step dict, skipping keys left at
    their default/empty so the emitted YAML stays uncluttered."""
    name = spec["name"]
    kind = spec["kind"]

    if spec.get("virtual"):
        return

    if value is None:
        return

    if spec.get("display_scale"):
        if value == "":
            return
        raw = value / spec["display_scale"]
        default = spec.get("default")
        if default is not None and abs(raw - default) < 1e-9:
            return
        step[name] = raw
        return

    if kind == "criteria":
        if isinstance(value, dict) and value.get("field"):
            step[value["field"]] = _field_to_value(value["value"])
        return
    if kind == "resolvable":
        resolved = _field_to_value(value)
        if resolved is not None and resolved != "":
            step[name] = resolved
        return
    if kind == "list_resolvable":
        resolved = _field_to_list(value)
        if resolved:
            # equals/not_equals: a single-element list is written back
            # as a bare scalar, matching how these are actually
            # hand-written in flows/*.yaml today (params always stays
            # a list, even with one element, since read_sql/assert_sql
            # always call cursor.execute with a list of params).
            if name in ("equals", "not_equals") and len(resolved) == 1:
                step[name] = resolved[0]
            else:
                step[name] = resolved
        return
    if kind == "store_as":
        if value not in (None, "", []):
            step[name] = value
        return
    if kind == "tap_if_closed":
        if isinstance(value, dict) and value.get("below"):
            fallback = {"below": value["below"]}
            if value.get("control_class") and value["control_class"] != "android.widget.Button":
                fallback["control_class"] = value["control_class"]
            if value.get("occurrence") and value["occurrence"] != 1:
                fallback["occurrence"] = value["occurrence"]
            step[name] = fallback
        return
    if kind == "bool":
        if value:
            step[name] = True
        return
    default = spec.get("default")
    if value != default and value != "":
        step[name] = value


def _find_verb_key(step_types: dict, step: dict) -> str | None:
    """A verb (see build_verb_step_types) is sugar over a real step type
    (currently always 'query') with a fixed 'name' -- if this raw step
    matches one, returns that verb's STEP_TYPES key so it renders as its
    own labeled/colored pill instead of the generic step + a dropdown."""
    step_type = step.get("type")
    for key, spec in step_types.items():
        if spec.get("verb_of") == step_type and spec.get("verb_name") == step.get("name"):
            return key
    return None


def _find_base_key(step_types: dict, step: dict) -> str | None:
    """Same trick as _find_verb_key, for base files (see
    build_base_step_types) -- a plain {type: base, name: X} step whose
    name matches a known flows/base/*.yaml file renders as that file's
    own pill instead of the generic "base" block plus a name dropdown."""
    if step.get("type") != "base":
        return None
    for key, spec in step_types.items():
        if spec.get("base_of") == "base" and spec.get("base_name") == step.get("name"):
            return key
    return None


INJECT_SLOT_TYPE = "__inject_slot__"


def _inject_slots_to_blocks(inject_points: list, step: dict, step_types: dict) -> list[dict]:
    """Builds one __inject_slot__ pseudo-block per inject point the
    target base file actually declares (inject_points, from
    build_base_step_types) -- each holds whatever steps the caller
    targeted at that point (step["injects"][name], with step["steps"]
    as sugar for the unnamed point -- see _step_base in
    engine/flow_runner.py). Re-derived fresh from the base file's
    CURRENT content every time rather than stored in the flow itself, so
    adding/renaming/removing an inject point there is picked up on next
    load without touching every caller."""
    injects = dict(step.get("injects") or {})
    plain_steps = step.get("steps")
    if plain_steps:
        injects.setdefault(None, plain_steps)

    slots = []
    for point_name in inject_points:
        slot_steps = injects.get(point_name) or []
        slots.append({
            "id": None,
            "type": INJECT_SLOT_TYPE,
            "fields": {"name": point_name or ""},
            "modifiers": _extract_modifiers({}),
            "children": [step_to_block(s, step_types) for s in slot_steps],
        })
    return slots


def _inject_slots_to_step(children: list, step: dict, step_types: dict) -> None:
    """Inverse of _inject_slots_to_blocks -- writes each slot's steps
    back onto the step dict as 'steps' (the unnamed point, for backward
    compatibility with base files that only ever had one) and/or
    'injects' (any named points), skipping empty slots entirely so an
    unused inject point doesn't clutter the saved YAML."""
    injects = {}
    for slot in children:
        point_name = (slot.get("fields") or {}).get("name") or None
        slot_children = slot.get("children") or []
        if slot_children:
            injects[point_name] = [block_to_step(c, step_types) for c in slot_children]

    if None in injects:
        step["steps"] = injects.pop(None)
    if injects:
        step["injects"] = injects


def step_to_block(step: dict, step_types: dict | None = None) -> dict:
    """Converts one step dict (as loaded from flows/*.yaml) into a JSON
    block object the Flow Editor frontend renders. Recurses into
    children for container types (group/loop/base).

    step_types defaults to the built-in STEP_TYPES table, but callers
    that also have SQL-backed verbs (see build_verb_step_types) pass the
    merged dict through here instead, so a plain `type: query, name: X`
    step whose name matches a registered verb renders as that verb's own
    block type rather than generic "query"."""
    step_types = step_types if step_types is not None else STEP_TYPES
    step_type = step.get("type")

    if step_type == "inject":
        return {
            "id": None,
            "type": INJECT_TYPE,
            "fields": {"name": step.get("name") or ""},
            "modifiers": _extract_modifiers({}),
            "children": [],
        }

    verb_key = _find_verb_key(step_types, step)
    if verb_key is not None:
        verb_spec = step_types[verb_key]
        fields = {f["name"]: _field_value_for_block(f, step) for f in verb_spec["fields"]}
        return {
            "id": None,
            "type": verb_key,
            "fields": fields,
            "modifiers": _extract_modifiers(step),
            "children": [],
        }

    base_key = _find_base_key(step_types, step)
    if base_key is not None:
        return {
            "id": None,
            "type": base_key,
            "fields": {},
            "modifiers": _extract_modifiers(step),
            "children": _inject_slots_to_blocks(step_types[base_key].get("inject_points") or [], step, step_types),
        }

    spec = step_types.get(step_type)
    if spec is None:
        # Unknown step type (e.g. a hand-written flow using a type this
        # editor doesn't model yet) -- represented as an opaque block so
        # loading never hard-fails; round-trips back out unchanged via
        # the "raw" field, but won't be editable in the UI.
        return {
            "id": None,
            "type": "__unknown__",
            "fields": {"raw_type": step_type, "raw": step},
            "modifiers": {"if_visible": None, "if_visible_timeout": None, "retries": None,
                          "retry_delay": None, "required": False, "comment": None},
            "children": [],
        }

    fields = {}
    for field_spec in spec["fields"]:
        fields[field_spec["name"]] = _field_value_for_block(field_spec, step)

    children = []
    if spec["container"]:
        nested = step.get("steps") or []
        children = [step_to_block(s, step_types) for s in nested]

    return {
        "id": None,
        "type": step_type,
        "fields": fields,
        "modifiers": _extract_modifiers(step),
        "children": children,
    }


def block_to_step(block: dict, step_types: dict | None = None) -> dict:
    """Inverse of step_to_block -- converts one JSON block object back
    into a step dict suitable for yaml.safe_dump. Recurses into
    children for container types. See step_to_block for step_types."""
    step_types = step_types if step_types is not None else STEP_TYPES
    block_type = block.get("type")

    if block_type == INJECT_TYPE:
        name = (block.get("fields") or {}).get("name")
        return {"type": "inject", "name": name} if name else {"type": "inject"}

    if block_type == "__unknown__":
        raw = dict(block.get("fields", {}).get("raw") or {})
        return raw

    spec = step_types.get(block_type)
    if spec is None:
        raise ValueError(f"Unknown block type: {block_type!r}")

    if spec.get("verb_of"):
        # Sugar over a real step type with a fixed name -- e.g. a verb
        # over "query" compiles to plain {type: query, name: <verb>},
        # exactly what hand-writing it that way would produce.
        step = {"type": spec["verb_of"], "name": spec["verb_name"]}
        fields = block.get("fields") or {}
        for field_spec in spec["fields"]:
            if not _show_when_active(field_spec, fields):
                continue
            _field_value_for_step(field_spec, fields.get(field_spec["name"]), step)
        _apply_modifiers(step, block.get("modifiers") or {})
        return step

    if spec.get("base_of"):
        # Same sugar as verb_of, but base is a container -- compiles to
        # plain {type: base, name: <file>}, carrying over its injected
        # children (one __inject_slot__ per inject point the target base
        # file declares -- see _inject_slots_to_blocks/_to_step) the same
        # way the generic "base" block does.
        step = {"type": spec["base_of"], "name": spec["base_name"]}
        _inject_slots_to_step(block.get("children") or [], step, step_types)
        _apply_modifiers(step, block.get("modifiers") or {})
        return step

    step: dict = {"type": block_type}
    fields = block.get("fields") or {}
    for field_spec in spec["fields"]:
        if not _show_when_active(field_spec, fields):
            continue
        _field_value_for_step(field_spec, fields.get(field_spec["name"]), step)

    if spec["container"]:
        children = block.get("children") or []
        # base's injected-steps payload is genuinely optional -- a base
        # call with no injected steps (e.g. pallet_build_finish.yaml's
        # plain suffix include) should round-trip with no `steps:` key
        # at all, not an empty list, matching real hand-written flows
        # and _step_base's own "steps given but no inject point" check
        # (an empty list there is indistinguishable from "not given").
        # group/loop always require a non-empty steps list at run time
        # (see FlowError checks in _step_group/_step_loop), so always
        # emitting the key there is correct either way.
        if block_type == "base":
            if children:
                step["steps"] = [block_to_step(c, step_types) for c in children]
        else:
            step["steps"] = [block_to_step(c, step_types) for c in children]

    _apply_modifiers(step, block.get("modifiers") or {})

    return step


def flow_to_blocks(data: dict, step_types: dict | None = None) -> dict:
    """Converts a whole flow file's parsed YAML (dict with name/
    description/steps) into the JSON envelope the Flow Editor loads."""
    data = data or {}
    steps = data.get("steps") or []
    return {
        "name": data.get("name", ""),
        "description": data.get("description", ""),
        "section": data.get("section", ""),
        "steps": [step_to_block(s, step_types) for s in steps],
    }


def blocks_to_flow(doc: dict, step_types: dict | None = None) -> dict:
    """Inverse of flow_to_blocks -- converts the JSON envelope the Flow
    Editor saves back into a plain dict ready for yaml.safe_dump.

    section is a user-defined, backslash-delimited path (e.g.
    "Inventory\\PalletBuild") the Run Tests tab groups the Available
    tests list by -- purely a display grouping, FlowRunner never reads
    it. Omitted entirely (not even as `section: ""`) when blank, same as
    every other optional key here, so an ungrouped flow's YAML doesn't
    carry clutter."""
    doc = doc or {}
    blocks = doc.get("steps") or []
    result = {"name": doc.get("name", ""), "description": doc.get("description", "")}
    section = (doc.get("section") or "").strip()
    if section:
        result["section"] = section
    result["steps"] = [block_to_step(b, step_types) for b in blocks]
    return result


def has_inject(data: dict) -> bool:
    """Scans a base file's top-level steps list for an {type: inject}
    placeholder -- used to tell the editor whether a base file accepts
    injected steps at all."""
    steps = (data or {}).get("steps") or []
    return any(isinstance(s, dict) and s.get("type") == "inject" for s in steps)


def validate_blocks(blocks: list, step_types: dict | None = None) -> list[str]:
    """Returns a list of human-readable problems with a block tree
    before it's converted/saved -- e.g. an empty group/loop, which
    FlowRunner itself rejects with a FlowError at run time (see
    _step_group/_step_loop's "requires a non-empty 'steps' list").
    Catching this at save time gives a clear error immediately instead
    of only surfacing it the next time someone actually runs the flow.
    """
    step_types = step_types if step_types is not None else STEP_TYPES
    problems = []

    def walk(block_list, path):
        for i, block in enumerate(block_list):
            block_type = block.get("type")
            here = f"{path}[{i}]"
            if block_type not in step_types and block_type not in (INJECT_TYPE, INJECT_SLOT_TYPE, "__unknown__"):
                problems.append(f"{here}: unknown step type {block_type!r}")
                continue
            if block_type in ("group", "loop"):
                children = block.get("children") or []
                if not children:
                    problems.append(f"{here} ({block_type}): requires at least one nested step")
                walk(children, f"{here}.children")
            elif (
                block_type == "base"
                or step_types.get(block_type, {}).get("base_of") == "base"
                or block_type == INJECT_SLOT_TYPE
            ):
                walk(block.get("children") or [], f"{here}.children")

    walk(blocks, "steps")
    return problems


def _verb_label(name: str) -> str:
    """'RemoveCaseMovePermission' -> 'Remove Case Move Permission' for
    the palette pill/block header -- same PascalCase splitter used for
    query param names (see app.py's _prettify_param_name)."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", name)


def build_verb_step_types(verb_infos: list[dict]) -> dict[str, dict]:
    """Synthesizes one STEP_TYPES entry per SQL-backed 'verb' (a file
    under queries/verbs/*.sql -- see app.py's VERBS_DIR) -- each is
    sugar over the "query" step with a fixed name, so e.g.
    RemoveCaseMovePermission.sql shows up as its own labeled/colored
    "REMOVE CASE MOVE PERMISSION" pill instead of generic "query" plus
    an action dropdown. It saves out as plain `type: query, name:
    RemoveCaseMovePermission` -- FlowRunner needs no changes at all, and
    a flow written by hand with that shape loads back as the verb pill
    too (see _find_verb_key).

    verb_infos is a list of {"name", "label", "section"} dicts (see
    app.py's list_verbs) -- label/section default to the auto-derived
    PascalCase-split name and "Custom Verbs" respectively when not
    overridden via the Verb Editor's saved metadata.

    Callers merge this into STEP_TYPES (e.g. `{**STEP_TYPES,
    **build_verb_step_types(infos)}`) and pass the result as the
    step_types argument to step_to_block/block_to_step/flow_to_blocks/
    blocks_to_flow/validate_blocks -- the module-level STEP_TYPES itself
    stays built-ins-only, since verb availability is only known at
    request time (whatever's currently in queries/verbs/)."""
    query_fields = [f for f in STEP_TYPES["query"]["fields"] if f["name"] != "name"]
    result = {}
    for info in verb_infos:
        name = info["name"]
        section = info.get("section") or "Custom Verbs"
        result[f"verb__{name}"] = {
            "section": section,
            "description": f"Runs the saved '{name}' SQL query (queries/verbs/{name}.sql).",
            "container": False,
            "verb_of": "query",
            "verb_name": name,
            "label": info.get("label") or _verb_label(name),
            "fields": query_fields,
        }
    return result


def _base_key(filename: str) -> str:
    return "base__" + re.sub(r"[^A-Za-z0-9]+", "_", filename)


def build_base_step_types(base_files: list[dict]) -> dict[str, dict]:
    """Synthesizes one STEP_TYPES entry per shared flows/base/*.yaml file
    -- each is sugar over the "base" step with a fixed 'name', the same
    trick build_verb_step_types uses for SQL queries, so every base file
    shows up as its own labeled pill (grouped into the Base palette menu
    by its own section: field -- see "subsection" below) instead of one
    generic "base" block plus a filename dropdown. Compiles to plain
    `{type: base, name: <file>}` -- FlowRunner needs no changes, and a
    flow written by hand that way loads back as the matching pill too
    (see _find_base_key).

    base_files is a list of {"filename", "label", "section",
    "inject_points"} dicts (see app.py's list_base_files) --
    inject_points is the ordered list of each `- type: inject` in that
    file's own steps (None for an unnamed one), used by step_to_block to
    build one __inject_slot__ per point (see _inject_slots_to_blocks) so
    the editor always reflects that file's CURRENT inject points, not
    whatever existed when a calling flow was last saved. Callers merge
    this into STEP_TYPES the same way as build_verb_step_types."""
    return {
        _base_key(bf["filename"]): {
            "section": "Base",
            "subsection": bf.get("section") or "",
            "description": f"Includes the shared \"{bf['label']}\" base file (flows/base/{bf['filename']}).",
            "container": True,
            "base_of": "base",
            "base_name": bf["filename"],
            "inject_points": bf.get("inject_points") or [],
            "label": bf["label"],
            "fields": [],
        }
        for bf in base_files
    }


def parse_inject_points(base_steps: list) -> list:
    """Ordered list of each top-level `- type: inject` step's own 'name'
    (None for an unnamed one) in a base file's steps -- doesn't recurse
    into nested containers, matching _step_base's own flat scan in
    engine/flow_runner.py."""
    return [s.get("name") for s in (base_steps or []) if isinstance(s, dict) and s.get("type") == "inject"]


def references_base_file(steps: list, filename: str) -> bool:
    """True if this step list contains a `{type: base, name: <filename>}`
    step anywhere -- recurses into every container's nested steps
    (group/loop/base's own 'steps', and a base call's 'injects' lists),
    so a base file used deep inside a group or another base file's inject
    still counts. Used by app.py's "used by" lookup (see
    find_base_file_usages) so renaming/deleting a base file can warn
    about every flow/base file that actually calls it."""
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        if step.get("type") == "base":
            if step.get("name") == filename:
                return True
            for inject_steps in (step.get("injects") or {}).values():
                if references_base_file(inject_steps, filename):
                    return True
        if references_base_file(step.get("steps") or [], filename):
            return True
    return False
