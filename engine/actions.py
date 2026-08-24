"""
actions.py
Registry of named actions a flow file can invoke via a step like:

    - type: action
      name: some_custom_thing

Actions are the escape hatch for genuine LOGIC that the generic verbs
(tap, enter_text, read_text, select_modal, press_key, assert_*) can't
express -- branching, computation, anything beyond "find this text and
do something with it."

To add one:
    def my_action(runner: "FlowRunner") -> object:
        ...use runner.driver / runner.screen (a BasePage) directly...
        return some_value

    ACTIONS["my_action"] = my_action

...then reference it from a flow file with `type: action, name: my_action`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from engine.flow_runner import FlowRunner

ACTIONS: dict[str, Callable[["FlowRunner"], object]] = {}


def dump_page_source(runner: "FlowRunner") -> str:
    """
    Diagnostic-only: writes the current screen's full page source to
    appium_helpers/dumps/<label>_<timestamp>.xml via the same helper
    android.py's __main__ block uses, and returns the path as a string.
    Meant to be added TEMPORARILY to a flow (type: action, name:
    dump_page_source) right before a step that can't find an element --
    the dump shows the real hierarchy around that field so a locator
    guess can be replaced with something confirmed against the actual
    DOM. Remove the step once the locator is fixed; this isn't meant to
    stay in a flow permanently.
    """
    from appium_helpers.android import dump_page_source as _dump

    path = _dump(runner.driver, label="dump")
    return str(path)


ACTIONS["dump_page_source"] = dump_page_source