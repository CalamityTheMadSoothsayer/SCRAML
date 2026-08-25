# SCRAML <img width="480" alt="image" src="https://github.com/user-attachments/assets/20c9d021-606a-49f8-ad03-cd409893915c" />


<img width="2534" height="1214" alt="image" src="https://github.com/user-attachments/assets/3daca63c-91d6-4ff3-a775-54b6b76e60ad" />


A Scratch-style visual block editor for building arbitrary YAML documents.

Drag blocks from a palette onto a canvas to build a `steps:` list, save it
straight to a `.yaml` file, and reload it back into blocks later. Nothing
about the palette is hardcoded -- every block type, including the built-ins,
is a plain data record you can edit or add to from inside the app itself.
The palette is assembled at runtime from:

- **`webui/flow_blocks.py`'s `STEP_TYPES`** -- the built-in block
  definitions (tap, wait, assert_sql, group, etc.), editable from the
  **Blocks** tab. An edit persists to `config/step_types.json` (created on
  first save); until then the app just uses the built-in table as-is.
- **`queries/*.sql`** -- SQL files, each usable from a generic `query`
  block, or "promoted" to its own labeled palette pill (a "verb") via
  `queries/verbs/`, editable from the **Verbs** tab.
- **`flows/base/*.yaml`** -- shared step sequences other flows can include
  wholesale via a `base` block, optionally with named injection points.

The built-in step types themselves came from SCRAML's origin as the Flow
Editor inside an Android UI test-automation tool; since they're just data
now, point the same block-canvas/YAML machinery at a different domain
entirely by editing them in the Blocks tab, no code changes needed.

## Tabs

- **Flow Editor** -- the drag-and-drop canvas: build a flow's `steps:`
  list out of blocks, save/rename/delete flows and base files, lint every
  flow on disk for dangling base/query/action references.
- **Queries** -- a sidebar of `queries/*.sql` files with a syntax-
  highlighted SQL editor (line numbers, param placeholders). If a named
  ODBC connection is configured, a query can also be run directly against
  a real database and its results/params inspected -- see "DB
  connections" below; this is the one place SCRAML actually executes
  anything, and only for previewing a query, not for running flows.
- **Verbs** -- edit a promoted verb's SQL plus its display label/palette
  section (stored in a `.meta.json` sidecar next to the `.sql` file).
- **Blocks** -- the full field editor behind `STEP_TYPES`: add, edit, or
  delete any block type (built-in or custom), including its fields'
  kinds, defaults, choices, and container-ness.

## Running

```bash
pip install -r requirements.txt
python webui/app.py
```

Then open http://127.0.0.1:5000/.

## DB connections (optional)

The Queries tab's Run button executes against a named connection stored
in `config/connections.json` (see `config/connections.example.json` for
the shape -- a plain list of `{name, connection_string}`, where the
connection string is whatever your ODBC driver expects). This file is
gitignored and stored in **plaintext**, deliberately, for a local
single-user dev tool -- never commit real credentials. Running a query
against a live connection also requires an ODBC driver installed on the
machine (`pyodbc`'s own dependency, not something `pip install` alone
provides).

## Layout

- `webui/app.py` -- Flask app and JSON API (load/save/rename/delete a
  flow, lint every flow on disk, manage `queries/*.sql`/verbs/block types,
  DB connections + running SQL).
- `webui/flow_blocks.py` -- converts between a flow's on-disk YAML shape
  and the JSON block tree the frontend renders; also the built-in
  `STEP_TYPES` table (see `config/step_types.json` for how an edited copy
  shadows it).
- `webui/templates/index.html` + `webui/static/js/flow_editor.js` -- the
  whole frontend: all four tabs, the drag-and-drop canvas, and the SQL
  editor.
- `engine/db.py` -- thin `pyodbc` wrapper backing the Queries tab's Run
  button.
- `engine/actions.py` -- the custom-action registry.
- `flows/` -- example flow and base files.
- `queries/` -- example SQL files (`queries/select_item_from_table.sql`
  is a placeholder -- point `SCRAML_QUERIES_DIR` at your own directory to
  use a different set).
- `config/` -- `ui_prefs.json` (theme), `step_types.json` (edited block
  types, if any), `connections.json` (DB connections, gitignored).

## Building the EXE

```powershell
py -3 -m PyInstaller --clean --noconfirm SCRAML.spec
```

Output: `dist/SCRAML/SCRAML.exe`.
