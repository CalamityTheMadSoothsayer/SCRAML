# SCRAML

A Scratch-style visual block editor for building arbitrary YAML documents.

Drag blocks from a palette onto a canvas to build a `steps:` list, save it
straight to a `.yaml` file, and reload it back into blocks later. The
palette isn't hardcoded to one schema -- it's assembled at runtime from:

- **`engine/actions.py`** -- a registry of named Python actions (`ACTIONS`
  dict). Each one shows up in the editor's "Escape hatch" section as an
  `action` block referencing it by name.
- **`queries/*.sql`** -- SQL files, each usable from a generic `query`
  block, or "promoted" to its own labeled palette pill (a "verb") via
  `queries/verbs/`.
- **`flows/base/*.yaml`** -- shared step sequences other flows can include
  wholesale via a `base` block, optionally with named injection points.

The built-in step types themselves (tap, wait, assert_sql, etc. -- see
`webui/flow_blocks.py`'s `STEP_TYPES` table) came from SCRAML's origin as
the Flow Editor inside an Android UI test-automation tool; swap or extend
that table to point the same block-canvas/YAML machinery at a different
domain entirely.

## Running

```bash
pip install -r requirements.txt
python webui/app.py
```

Then open http://127.0.0.1:5000/.

## Layout

- `webui/app.py` -- Flask app and JSON API (load/save/rename/delete a
  flow, lint every flow on disk, manage `queries/*.sql` and verbs).
- `webui/flow_blocks.py` -- converts between a flow's on-disk YAML shape
  and the JSON block tree the frontend renders.
- `webui/static/js/flow_editor.js` + `webui/templates/index.html` -- the
  drag-and-drop canvas and Queries tab.
- `flows/` -- example flow and base files.
- `queries/` -- example SQL files (`queries/select_item_from_table.sql`
  is a placeholder -- point `SCRAML_QUERIES_DIR` at your own directory to
  use a different set).
- `engine/actions.py` -- the custom-action registry.

## Building the EXE

```powershell
py -3 -m PyInstaller --clean --noconfirm SCRAML.spec
```

Output: `dist/SCRAML/SCRAML.exe`.
