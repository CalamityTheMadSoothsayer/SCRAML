// webui/static/js/flow_editor.js
//
// Scratch-style block editor for flows/*.yaml. Renders/collects blocks
// entirely off the STEP_TYPES table shipped from webui/flow_blocks.py
// (embedded into the page as window.STEP_TYPES) -- this file must never
// hand-duplicate a step type's field list; if a field is missing here,
// add it to STEP_TYPES in flow_blocks.py instead, not here.
//
// Block DOM node shape: <li class="block" data-type="...">
//   .block-header  (type label, required/if_visible/retries badges, delete/collapse)
//   .block-fields  (one row per STEP_TYPES field, plus the shared modifiers section)
//   .block-children (only for group/loop/base -- itself a canvas <ul>, i.e. a drop zone)
//
// Palette chips are the SAME markup as a block header, minus fields/children,
// with data-source="palette" so dragstart can tell "clone a new block" apart
// from "move an existing one" (data-source="canvas").

(function () {
  // let, not const -- reassigned by refreshStepTypes() whenever a verb is
  // promoted/removed on the Queries tab, so every function below (all of
  // which read this via closure) picks up the change without a reload.
  let STEP_TYPES = window.STEP_TYPES;
  const SECTIONS = [
    "Navigation / interaction",
    "Reading / entering",
    "Assertions",
    "Auditing",
    "Composition",
  ];

  let blockIdCounter = 0;
  function nextId() {
    return "b" + (blockIdCounter++);
  }

  // Used by the duplicate-block button -- gives a cloned block (and
  // every nested child, including inject slots) a fresh id so it isn't
  // mistaken for the original it was copied from.
  function regenerateBlockIds(block) {
    block.id = nextId();
    for (const child of block.children || []) regenerateBlockIds(child);
  }

  // Set by loadFlow()/newFlow() -- true only while editing a
  // flows/base/*.yaml file, since the __inject__ pseudo-block only means
  // something there.
  let editingBase = false;
  let currentRelPath = null; // e.g. "login.yaml" or "base/pallet_build_finish.yaml"

  // ------------------------------------------------------------------
  // Palette
  // ------------------------------------------------------------------

  // Collapsed/expanded state survives a buildPalette() rebuild (e.g. on
  // every loadFlow()) by section title, since users tend to keep the same
  // sections closed across flows -- keyed in module scope rather than
  // localStorage since it's only meaningful within this page session.
  //
  // Rather than hardcoding every known title as collapsed-by-default (a
  // list that has to be kept in sync with every new section AND every
  // new dynamically-nested subsection -- e.g. Base's per-file pills
  // group by whatever section: string their own .yaml declares, unknown
  // ahead of time), any title seen for the first time defaults to
  // collapsed automatically -- see seenPaletteSections below -- unless a
  // copied/reloaded URL already names it expanded.
  const collapsedPaletteSections = new Set();
  const seenPaletteSections = new Set();

  // A reload/copied link reopens whichever sections were expanded before
  // -- see window.FE_URL_STATE (index.html) and persistExpandedSections
  // below, which keeps the "expanded" URL param in sync on every toggle.
  const urlExpandedTitles = new Set(
    ((window.FE_URL_STATE && window.FE_URL_STATE.get("expanded")) || "")
      .split(",")
      .filter(Boolean)
      .map(decodeURIComponent)
  );
  // "Base file only" only exists transiently while a base file happens
  // to be loaded -- always starts collapsed on a fresh page load
  // regardless of what a stale/copied URL remembers, same as it'd start
  // for anyone who'd never opened it before.
  urlExpandedTitles.delete("Base file only");

  // Exposed for index.html's theme-toggle easter egg, which checks
  // whether specific sections are open before counting clicks.
  window.FE_PALETTE_STATE = {
    isExpanded: (title) => !collapsedPaletteSections.has(title),
  };

  function persistExpandedSections() {
    if (!window.FE_URL_STATE) return;
    const expanded = [...seenPaletteSections].filter((t) => !collapsedPaletteSections.has(t));
    window.FE_URL_STATE.set("expanded", expanded.map(encodeURIComponent).join(","));
  }

  function buildPaletteGroup(paletteEl, title, buildBody) {
    if (!seenPaletteSections.has(title)) {
      seenPaletteSections.add(title);
      if (!urlExpandedTitles.has(title)) collapsedPaletteSections.add(title);
    }

    const group = document.createElement("div");
    group.className = "fe-palette-group";

    const heading = document.createElement("button");
    heading.type = "button";
    heading.className = "fe-palette-section";
    heading.textContent = title;

    const body = document.createElement("div");
    body.className = "fe-palette-group-body";
    buildBody(body);

    function applyCollapsed() {
      const collapsed = collapsedPaletteSections.has(title);
      group.classList.toggle("collapsed", collapsed);
    }
    heading.addEventListener("click", () => {
      if (collapsedPaletteSections.has(title)) {
        collapsedPaletteSections.delete(title);
      } else {
        collapsedPaletteSections.add(title);
        // Collapsing a group hides any nested subsections inside it too
        // (e.g. Base's per-file subsections) -- fold those closed as
        // well, or they'd sit invisibly "expanded" and keep cluttering
        // the ?expanded= URL param even though nothing shows them.
        for (const el of body.querySelectorAll(".fe-palette-group > .fe-palette-section")) {
          collapsedPaletteSections.add(el.textContent);
          el.parentElement.classList.add("collapsed");
        }
      }
      applyCollapsed();
      persistExpandedSections();
    });
    applyCollapsed();

    group.appendChild(heading);
    group.appendChild(body);
    paletteEl.appendChild(group);
    return body;
  }

  // Groups the Base menu's per-file pills into nested subsections by
  // each file's own section: field (backslash-delimited, same
  // convention a flow's section: uses for the Available-tests tree --
  // see buildSectionTree in index.html) -- e.g. "Inventory
  // Mgmt\Pallet Build" nests one level under the other. A pill with no
  // section: set just sits as a flat chip alongside the subsections.
  function buildBasePaletteTree(body, entries) {
    const flat = entries.filter((e) => !e.path.length);
    const grouped = new Map();
    for (const e of entries) {
      if (!e.path.length) continue;
      const [head, ...rest] = e.path;
      if (!grouped.has(head)) grouped.set(head, []);
      grouped.get(head).push({ type: e.type, path: rest });
    }
    for (const e of flat) body.appendChild(makePaletteChip(e.type));
    for (const [head, items] of grouped) {
      buildPaletteGroup(body, head, (subBody) => buildBasePaletteTree(subBody, items));
    }
  }

  // The Queries tab dispatches "step-types-changed" (a separate script,
  // same page) after promoting/removing a verb -- refetches the merged
  // built-ins + verbs table and rebuilds the palette so a new/removed
  // verb pill shows up without a reload.
  async function refreshStepTypes() {
    try {
      const res = await fetch("/api/step-types");
      STEP_TYPES = await res.json();
    } catch (e) {
      return;
    }
    buildPalette();
  }

  function buildPalette() {
    const paletteEl = document.getElementById("fe-palette");
    paletteEl.innerHTML = "";

    const varsBody = buildPaletteGroup(paletteEl, "Variables", () => {});
    varsBody.id = "fe-var-list";
    refreshVariablesPalette();

    // SECTIONS plus any section STEP_TYPES has that it doesn't list
    // (e.g. "Custom Verbs", populated dynamically per queries/verbs/*.sql
    // -- see build_verb_step_types in flow_blocks.py) -- skipped
    // entirely when empty, so an empty custom category doesn't render a
    // permanent blank header.
    const allSections = [...SECTIONS];
    for (const spec of Object.values(STEP_TYPES)) {
      if (!allSections.includes(spec.section)) allSections.push(spec.section);
    }
    for (const section of allSections) {
      // The generic "base" step type (a fixed block + filename dropdown)
      // is superseded by "Base" -- one pill per flows/base/*.yaml file,
      // synthesized server-side (see build_base_step_types) -- so it's
      // excluded here rather than also showing up under Composition.
      const matching = Object.entries(STEP_TYPES).filter(([type, spec]) => spec.section === section && type !== "base");
      if (!matching.length) continue;
      buildPaletteGroup(paletteEl, section, (body) => {
        if (section === "Base") {
          buildBasePaletteTree(
            body,
            matching.map(([type, spec]) => ({
              type,
              path: (spec.subsection || "").split("\\").map((s) => s.trim()).filter(Boolean),
            }))
          );
        } else {
          for (const [type] of matching) body.appendChild(makePaletteChip(type));
        }
      });
    }

    if (editingBase) {
      buildPaletteGroup(paletteEl, "Base file only", (body) => {
        body.appendChild(makePaletteChip("__inject__"));
      });
    }

    applyPaletteFilter();
  }

  // Search box above the palette -- filters chips by name (block types
  // AND variable chips alike) and force-opens any group with a match
  // regardless of its collapsed state, without touching that stored
  // state (clearing the search reverts to exactly how it was).
  let paletteSearchQuery = "";

  function applyPaletteFilter() {
    const q = paletteSearchQuery.trim().toLowerCase();
    for (const group of document.querySelectorAll("#fe-palette .fe-palette-group")) {
      const chips = [...group.querySelectorAll(":scope > .fe-palette-group-body > .fe-chip")];
      let anyMatch = false;
      for (const chip of chips) {
        const match = !q || chip.textContent.toLowerCase().includes(q);
        chip.style.display = match ? "" : "none";
        if (match) anyMatch = true;
      }
      group.style.display = q && !anyMatch ? "none" : "";
      group.classList.toggle("force-open", !!q && anyMatch);
    }
  }

  // "select_modal" -> "Select Modal", "store_as" -> "Store As" -- shared
  // by block headers/palette chips (uppercased on top of this) and field
  // row labels, so a raw snake_case STEP_TYPES name always reads as
  // words instead of a YAML key, without hand-authoring a label for
  // every single field.
  function prettifyLabel(s) {
    return s
      .split(/[_\s]+/)
      .filter(Boolean)
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join(" ");
  }

  // ------------------------------------------------------------------
  // Field/modifier help tooltips -- a small (i) button next to a label
  // that pops a short explanation on click (also available as a native
  // hover tooltip via the same text, for anyone who prefers that).
  // ------------------------------------------------------------------

  let activeHelpTooltip = null;

  function closeHelpTooltip() {
    if (activeHelpTooltip) {
      activeHelpTooltip.remove();
      activeHelpTooltip = null;
    }
  }
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".help-icon") && !e.target.closest(".help-tooltip")) closeHelpTooltip();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeHelpTooltip();
  });
  // A tooltip is positioned relative to the viewport (fixed), so it'd
  // otherwise drift out from under its icon as the palette/canvas/etc.
  // scroll underneath it -- simplest correct fix is just to close it,
  // same as clicking away.
  document.addEventListener("scroll", () => closeHelpTooltip(), true);

  function makeHelpIcon(helpText) {
    const icon = document.createElement("button");
    icon.type = "button";
    icon.className = "help-icon";
    icon.textContent = "i";
    icon.title = helpText;
    icon.setAttribute("aria-label", "Help: " + helpText);
    icon.addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
      const reopening = activeHelpTooltip && activeHelpTooltip.dataset.owner === icon.dataset.helpId;
      closeHelpTooltip();
      if (reopening) return;

      const tip = document.createElement("div");
      tip.className = "help-tooltip";
      tip.textContent = helpText;
      tip.dataset.owner = icon.dataset.helpId;
      document.body.appendChild(tip);

      const iconRect = icon.getBoundingClientRect();
      const tipRect = tip.getBoundingClientRect();
      let left = iconRect.left;
      if (left + tipRect.width > window.innerWidth - 12) left = window.innerWidth - tipRect.width - 12;
      tip.style.left = Math.max(8, left) + "px";
      tip.style.top = iconRect.bottom + 6 + "px";
      activeHelpTooltip = tip;
    });
    icon.dataset.helpId = "h" + (blockIdCounter++);
    return icon;
  }

  // Wraps a plain text label with an optional (i) help icon -- used for
  // both STEP_TYPES field rows (fieldSpec.help) and the hardcoded
  // modifier rows (if_visible/retries/etc., see MODIFIER_HELP).
  function makeFieldLabel(text, helpText) {
    const wrap = document.createElement("span");
    wrap.className = "field-label-wrap";
    const label = document.createElement("label");
    label.textContent = text;
    wrap.appendChild(label);
    if (helpText) wrap.appendChild(makeHelpIcon(helpText));
    return wrap;
  }

  function labelForType(type) {
    if (type === "__inject__") return "INJECT";
    const spec = STEP_TYPES[type];
    if (spec && spec.label) return spec.label.toUpperCase();
    return prettifyLabel(type).toUpperCase();
  }

  // __inject__ is a pseudo-block (see INJECT_TYPE in flow_blocks.py) --
  // it has no STEP_TYPES entry of its own, so its help text is
  // hardcoded here rather than coming from spec.description.
  function descriptionForType(type) {
    if (type === "__inject__") {
      return "Marks where a base file's caller can splice in its own steps -- only meaningful inside a base file, and only usable if you drag it in yourself (see flows/base/*.yaml's own inject: point).";
    }
    const spec = STEP_TYPES[type];
    return spec && spec.description;
  }

  function makePaletteChip(type) {
    const chip = document.createElement("div");
    chip.className = "fe-chip";
    const spec = STEP_TYPES[type];
    const label = document.createElement("span");
    label.textContent = labelForType(type);
    chip.appendChild(label);
    const description = descriptionForType(type);
    if (description) {
      const icon = makeHelpIcon(description);
      icon.classList.add("fe-chip-help");
      chip.appendChild(icon);
    }
    chip.draggable = true;
    chip.dataset.type = type;
    chip.dataset.source = "palette";
    if (spec) chip.dataset.section = spec.section;
    let createdEl = null;
    chip.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", type);
      draggingType = type;
      // Build the real block up front and let it be the thing dragover
      // live-repositions (same mechanism an existing canvas block's
      // reorder already uses) -- so a brand-new block can be dropped
      // BETWEEN existing ones on its very first placement, not just
      // appended to the end and reordered afterward as a second step.
      createdEl = buildBlockNode(emptyBlock(type));
      draggingEl = createdEl;
      draggingEl.classList.add("dragging");
      chip.classList.add("dragging");
    });
    chip.addEventListener("dragend", () => {
      chip.classList.remove("dragging");
      if (createdEl) createdEl.classList.remove("dragging");
      createdEl = null;
      draggingEl = null;
      draggingType = null;
    });
    return chip;
  }

  // ------------------------------------------------------------------
  // Block construction
  // ------------------------------------------------------------------

  function emptyBlock(type) {
    const spec = STEP_TYPES[type];
    const fields = {};
    if (spec) {
      for (const f of spec.fields) {
        fields[f.name] = defaultFieldValue(f);
      }
    }
    // A base pill starts with one empty __inject_slot__ per inject point
    // its target file currently declares (see spec.inject_points, from
    // build_base_step_types), so the right number of drop targets shows
    // up immediately rather than only appearing after a save/reload.
    const children = (spec && spec.inject_points ? spec.inject_points : []).map((pointName) => ({
      id: nextId(),
      type: "__inject_slot__",
      fields: { name: pointName || "" },
      modifiers: { ...EMPTY_MODIFIERS },
      children: [],
    }));
    return {
      id: nextId(),
      type,
      fields,
      modifiers: {
        if_visible: null,
        if_visible_timeout: null,
        retries: null,
        retry_delay: null,
        required: false,
        comment: null,
      },
      children,
    };
  }

  function defaultFieldValue(fieldSpec) {
    // Distinct from fieldSpec.default -- that value is what a saved step
    // round-trips as when this key is left out (must match the runner's
    // own hardcoded fallback), while new_default is purely which choice
    // a freshly dragged-in block should start on.
    if (fieldSpec.new_default !== undefined) return fieldSpec.new_default;
    if (fieldSpec.display_scale && fieldSpec.default !== undefined) {
      return fieldSpec.default * fieldSpec.display_scale;
    }
    switch (fieldSpec.kind) {
      case "resolvable":
        return { kind: "literal", value: "" };
      case "criteria":
        return { field: "text", value: { kind: "literal", value: "" } };
      case "list_resolvable":
        return [];
      case "bool":
        return fieldSpec.default || false;
      case "select":
        return fieldSpec.default || (fieldSpec.choices && fieldSpec.choices[0]) || "";
      case "store_as":
        return "";
      case "tap_if_closed":
        return null;
      case "int":
      case "float":
        return fieldSpec.default !== undefined ? fieldSpec.default : "";
      default:
        return fieldSpec.default !== undefined ? fieldSpec.default : "";
    }
  }

  // buildBlockNode: JSON block -> live DOM node (mirror of collectBlock)
  function buildBlockNode(block) {
    const li = document.createElement("li");
    li.className = "block";
    li.dataset.id = block.id || nextId();
    li.dataset.type = block.type;
    li.dataset.source = "canvas";
    li.draggable = true;
    const spec = STEP_TYPES[block.type];
    if (spec) li.dataset.section = spec.section;

    li.appendChild(buildBlockHeader(li, block));

    if (block.type !== "__inject_slot__" && block.type !== "__unknown__") {
      li.appendChild(buildFieldsArea(block));
    }
    if (block.type !== "__inject__" && block.type !== "__inject_slot__" && block.type !== "__unknown__") {
      li.appendChild(buildModifiersArea(block));
    }

    const isBase = block.type === "base" || (spec && spec.base_of);
    if (isBase) {
      li.appendChild(buildInjectSlotsWrap(block));
    } else if (spec && spec.container) {
      const childrenWrap = document.createElement("div");
      childrenWrap.className = "block-children-wrap";
      const childLabel = document.createElement("div");
      childLabel.className = "block-children-label";
      childLabel.textContent = "Steps:";
      childrenWrap.appendChild(childLabel);

      const childCanvas = document.createElement("ul");
      childCanvas.className = "canvas block-children";
      for (const child of block.children || []) {
        childCanvas.appendChild(buildBlockNode(child));
      }
      childrenWrap.appendChild(childCanvas);
      li.appendChild(childrenWrap);
    }

    return li;
  }

  // A base block's children are one __inject_slot__ per inject point the
  // target base file currently declares (see _inject_slots_to_blocks in
  // flow_blocks.py) -- each gets its own labeled mini-canvas rather than
  // being just another draggable block in one shared list, since a slot
  // itself can't be reordered/deleted/dragged (its identity comes from
  // the base file, not from anything the caller controls). Each slot's
  // OWN canvas is a completely normal .canvas element underneath, so all
  // the existing drag/drop/auto-scroll machinery (keyed off ".canvas",
  // not step type) already works on it for free.
  function buildInjectSlotsWrap(block) {
    const wrap = document.createElement("div");
    wrap.className = "block-children-wrap";

    const slots = block.children || [];
    if (!slots.length) {
      const empty = document.createElement("div");
      empty.className = "block-children-label";
      empty.textContent = "This base file has no inject points -- nothing can be added here.";
      wrap.appendChild(empty);
      return wrap;
    }

    for (const slot of slots) {
      const slotWrap = document.createElement("div");
      slotWrap.className = "inject-slot";

      const label = document.createElement("div");
      label.className = "inject-slot-label";
      label.textContent = "Inject: " + (slot.fields?.name ? prettifyLabel(slot.fields.name) : "(unnamed)");
      slotWrap.appendChild(label);

      const canvas = document.createElement("ul");
      canvas.className = "canvas block-children";
      canvas.dataset.injectSlotName = slot.fields?.name || "";
      for (const child of slot.children || []) {
        canvas.appendChild(buildBlockNode(child));
      }
      slotWrap.appendChild(canvas);

      wrap.appendChild(slotWrap);
    }

    return wrap;
  }

  function buildBlockHeader(li, block) {
    const header = document.createElement("div");
    header.className = "block-header";

    const collapseBtn = document.createElement("button");
    collapseBtn.type = "button";
    collapseBtn.className = "block-collapse";
    collapseBtn.textContent = "−";
    collapseBtn.title = "Collapse/expand";
    collapseBtn.addEventListener("click", () => {
      li.classList.toggle("collapsed");
      collapseBtn.textContent = li.classList.contains("collapsed") ? "+" : "−";
    });
    header.appendChild(collapseBtn);

    const typeLabel = document.createElement("span");
    typeLabel.className = "block-type-label";
    typeLabel.textContent = labelForType(block.type);
    header.appendChild(typeLabel);

    const description = descriptionForType(block.type);
    if (description) {
      const icon = makeHelpIcon(description);
      icon.classList.add("block-header-help");
      header.appendChild(icon);
    }

    const spacer = document.createElement("span");
    spacer.style.flex = "1";
    header.appendChild(spacer);

    const dupBtn = document.createElement("button");
    dupBtn.type = "button";
    dupBtn.className = "block-duplicate";
    dupBtn.textContent = "⧉";
    dupBtn.title = "Duplicate block";
    dupBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const data = collectBlock(li);
      regenerateBlockIds(data);
      li.after(buildBlockNode(data));
    });
    header.appendChild(dupBtn);

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "block-delete";
    delBtn.textContent = "×";
    delBtn.title = "Delete block";
    delBtn.addEventListener("click", () => li.remove());
    header.appendChild(delBtn);

    li.addEventListener("dragstart", (e) => {
      e.stopPropagation();
      draggingEl = li;
      draggingType = null;
      li.classList.add("dragging");
    });
    li.addEventListener("dragend", (e) => {
      e.stopPropagation();
      li.classList.remove("dragging");
      draggingEl = null;
    });

    return header;
  }

  // ------------------------------------------------------------------
  // Field rendering (renderFields / collectFields are strict mirrors)
  // ------------------------------------------------------------------

  // __inject__ is a pseudo-block (see INJECT_TYPE in flow_blocks.py) --
  // it has no STEP_TYPES entry of its own, so its one field (an optional
  // label letting a calling flow target it specifically -- see
  // injects: in _step_base) is hardcoded here instead.
  const INJECT_FIELD_SPECS = [{
    name: "name", kind: "text", optional: true, label: "label",
    help: "Names this inject point so a calling flow can target it specifically via injects:. Leave blank if this base file only needs one inject point -- a caller's plain steps: list always fills the unnamed one.",
  }];

  function buildFieldsArea(block) {
    const area = document.createElement("div");
    area.className = "block-fields";
    const spec = block.type === "__inject__" ? { fields: INJECT_FIELD_SPECS } : STEP_TYPES[block.type];
    if (!spec) return area;

    for (const fieldSpec of spec.fields) {
      area.appendChild(buildFieldRow(fieldSpec, block.fields[fieldSpec.name], block.type));
    }
    wireShowWhen(area, spec);
    if (block.type === "query") wireQueryParamLabels(area);
    else if (block.type === "assert_sql") wireAssertSqlParamLabels(area);
    else if (INLINE_SQL_PARAM_TYPES.has(block.type)) wireInlineSqlParamLabels(area);
    else if (spec.verb_name) wireVerbParamLabels(area, spec.verb_name);
    return area;
  }

  // Any step with a plain inline "query" text field plus a "params" list
  // (read_sql always; enter_text/scan when their source toggle is set to
  // "sql") gets the same auto-populated param slots as assert_sql's
  // inline mode -- re-parsed (debounced) as the SQL text is edited.
  const INLINE_SQL_PARAM_TYPES = new Set(["read_sql", "enter_text", "scan"]);

  // Labels a "query" step's params rows with the names its selected
  // action's .sql file actually declares its '?' placeholders as (see
  // /api/queries/<name>/params, backed by _parse_query_params in
  // app.py), refetched (and cached) whenever the action dropdown changes.
  const queryParamLabelsCache = new Map();

  async function fetchQueryParamLabels(name) {
    if (queryParamLabelsCache.has(name)) return queryParamLabelsCache.get(name);
    let labels = [];
    try {
      const res = await fetch(`/api/queries/${encodeURIComponent(name)}/params`);
      const data = await res.json();
      labels = (data.params || []).map((p) => p.label);
    } catch (e) {
      labels = [];
    }
    queryParamLabelsCache.set(name, labels);
    return labels;
  }

  function wireQueryParamLabels(area) {
    const actionSelect = area.querySelector('.field-row[data-field="name"] select');
    const paramsBox = area.querySelector('.field-row[data-field="params"] .list-resolvable-box');
    if (!actionSelect || !paramsBox) return;

    async function refresh() {
      paramsBox.setParamLabels(await fetchQueryParamLabels(actionSelect.value));
    }
    actionSelect.addEventListener("change", refresh);
    refresh();
  }

  async function fetchInlineSqlParamLabels(sqlText) {
    try {
      const res = await fetch("/api/parse-sql-params", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sql: sqlText || "" }),
      });
      const data = await res.json();
      return (data.params || []).map((p) => p.label);
    } catch (e) {
      return [];
    }
  }

  // assert_sql can run either a typed-inline query or a saved queries/
  // file (toggled by the virtual "source" field) -- params autopopulate
  // from whichever one is currently active, re-parsed on every relevant
  // change (debounced for the inline text box since that fires per key).
  function wireAssertSqlParamLabels(area) {
    const sourceSelect = area.querySelector('.field-row[data-field="source"] select');
    const queryInput = area.querySelector('.field-row[data-field="query"] input');
    const nameSelect = area.querySelector('.field-row[data-field="name"] select');
    const paramsBox = area.querySelector('.field-row[data-field="params"] .list-resolvable-box');
    if (!paramsBox) return;

    let debounceHandle = null;
    async function refresh() {
      const mode = sourceSelect ? sourceSelect.value : (nameSelect && nameSelect.value ? "file" : "inline");
      const labels =
        mode === "file"
          ? await fetchQueryParamLabels(nameSelect ? nameSelect.value : "")
          : await fetchInlineSqlParamLabels(queryInput ? queryInput.value : "");
      paramsBox.setParamLabels(labels);
    }
    function refreshDebounced() {
      clearTimeout(debounceHandle);
      debounceHandle = setTimeout(refresh, 400);
    }
    if (sourceSelect) sourceSelect.addEventListener("change", refresh);
    if (nameSelect) nameSelect.addEventListener("change", refresh);
    if (queryInput) {
      queryInput.addEventListener("input", refreshDebounced);
      queryInput.addEventListener("change", refresh);
    }
    refresh();
  }

  function wireInlineSqlParamLabels(area) {
    const queryInput = area.querySelector('.field-row[data-field="query"] input');
    const paramsBox = area.querySelector('.field-row[data-field="params"] .list-resolvable-box');
    if (!queryInput || !paramsBox) return;

    let debounceHandle = null;
    async function refresh() {
      paramsBox.setParamLabels(await fetchInlineSqlParamLabels(queryInput.value));
    }
    function refreshDebounced() {
      clearTimeout(debounceHandle);
      debounceHandle = setTimeout(refresh, 400);
    }
    queryInput.addEventListener("input", refreshDebounced);
    queryInput.addEventListener("change", refresh);
    refresh();
  }

  // A verb block's underlying query name is fixed (see build_verb_step_
  // types in flow_blocks.py) -- no dropdown to watch, just fetch once.
  async function wireVerbParamLabels(area, verbName) {
    const paramsBox = area.querySelector('.field-row[data-field="params"] .list-resolvable-box');
    if (!paramsBox) return;
    paramsBox.setParamLabels(await fetchQueryParamLabels(verbName));
  }

  // Hides/shows a field's row based on another field's current value --
  // e.g. scan/enter_text's "value" field only makes sense when source is
  // "literal", "query"/"params" only when source is "sql". Driven by the
  // dependent field's `show_when: {field, equals}` spec (STEP_TYPES).
  function wireShowWhen(area, spec) {
    const dependents = spec.fields.filter((f) => f.show_when);
    if (!dependents.length) return;

    const controllerNames = new Set(dependents.map((f) => f.show_when.field));
    for (const controllerName of controllerNames) {
      const controllerRow = area.querySelector(`:scope > .field-row[data-field="${controllerName}"]`);
      if (!controllerRow) continue;
      const controllerInput = controllerRow.querySelector("select, input");
      if (!controllerInput) continue;

      const apply = () => {
        for (const f of dependents) {
          if (f.show_when.field !== controllerName) continue;
          const row = area.querySelector(`:scope > .field-row[data-field="${f.name}"]`);
          if (!row) continue;
          row.style.display = controllerInput.value === f.show_when.equals ? "" : "none";
        }
      };
      controllerInput.addEventListener("change", apply);
      apply();
    }
  }

  function buildFieldRow(fieldSpec, value, blockType) {
    const row = document.createElement("div");
    row.className = "field-row";
    row.dataset.field = fieldSpec.name;
    row.dataset.kind = fieldSpec.kind;

    // Criteria fields are near-always literally named "text" in
    // STEP_TYPES (it's the step's own on-screen-text target), but the
    // criteria control itself opens with a text/starts_with/contains
    // select that defaults to "text" too -- labeling the row "text" reads
    // as "text: text ▾ ..." Default to "match" there instead, unless a
    // field spec explicitly overrides it.
    const labelText = prettifyLabel(fieldSpec.label || (fieldSpec.kind === "criteria" ? "match" : fieldSpec.name));
    row.appendChild(makeFieldLabel(labelText, fieldSpec.help));

    row.appendChild(buildFieldInput(fieldSpec, value, blockType));
    return row;
  }

  function buildFieldInput(fieldSpec, value, blockType) {
    const wrap = document.createElement("span");
    wrap.className = "field-input";

    switch (fieldSpec.kind) {
      case "text": {
        const input = document.createElement("input");
        input.type = "text";
        input.value = value ?? "";
        wrap.appendChild(input);
        break;
      }
      case "int":
      case "float": {
        const input = document.createElement("input");
        input.type = "number";
        if (fieldSpec.kind === "float") input.step = "any";
        input.value = value ?? "";
        wrap.appendChild(input);
        break;
      }
      case "bool": {
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = !!value;
        wrap.appendChild(input);
        break;
      }
      case "select": {
        const select = document.createElement("select");
        const choices = fieldSpec.dynamic_choices
          ? (window.FE_DYNAMIC_CHOICES && window.FE_DYNAMIC_CHOICES[fieldSpec.dynamic_choices]) || []
          : fieldSpec.choices || [];
        const labels = fieldSpec.choice_labels || {};
        for (const choice of choices) {
          const opt = document.createElement("option");
          opt.value = choice;
          opt.textContent = labels[choice] || prettifyLabel(choice);
          if (choice === value) opt.selected = true;
          select.appendChild(opt);
        }
        wrap.appendChild(select);
        break;
      }
      case "combo": {
        const listId = "dl-" + fieldSpec.name + "-" + (blockIdCounter++);
        const input = document.createElement("input");
        input.type = "text";
        input.value = value ?? "";
        input.setAttribute("list", listId);
        const datalist = document.createElement("datalist");
        datalist.id = listId;
        for (const choice of fieldSpec.choices || []) {
          const opt = document.createElement("option");
          opt.value = choice;
          datalist.appendChild(opt);
        }
        wrap.appendChild(input);
        wrap.appendChild(datalist);
        break;
      }
      case "store_as": {
        if (fieldSpec.allow_multi) {
          wrap.appendChild(buildStoreAsTagsInput(value));
        } else {
          const input = document.createElement("input");
          input.type = "text";
          input.placeholder = "name";
          input.value = Array.isArray(value) ? value.join(",") : value ?? "";
          wrap.appendChild(input);
        }
        break;
      }
      case "resolvable": {
        wrap.appendChild(buildResolvableInput(value));
        break;
      }
      case "criteria": {
        wrap.appendChild(buildCriteriaInput(value));
        break;
      }
      case "list_resolvable": {
        wrap.appendChild(buildListResolvableInput(value, fieldSpec.name === "params"));
        break;
      }
      case "tap_if_closed": {
        wrap.appendChild(buildTapIfClosedInput(value));
        break;
      }
      default: {
        const input = document.createElement("input");
        input.type = "text";
        input.value = value ?? "";
        wrap.appendChild(input);
      }
    }
    return wrap;
  }

  function buildResolvableInput(value) {
    value = value || { kind: "literal", value: "" };
    const box = document.createElement("span");
    box.className = "resolvable-box";

    const kindSelect = document.createElement("select");
    kindSelect.className = "resolvable-kind";
    for (const k of ["literal", "from"]) {
      const opt = document.createElement("option");
      opt.value = k;
      opt.textContent = k === "literal" ? "From text" : "From Variable";
      if (k === value.kind) opt.selected = true;
      kindSelect.appendChild(opt);
    }
    box.appendChild(kindSelect);

    // The single source of truth collectResolvable() reads back -- kept
    // in sync by whichever visible control is currently rendered into
    // `slot` below (a plain text input in "literal" mode, a removable
    // variable card in "from" mode), so the shape collectResolvable
    // expects never has to change.
    const store = document.createElement("input");
    store.type = "hidden";
    store.className = "resolvable-value";
    store.value = value.kind === "from" ? value.key || "" : value.value ?? "";
    box.appendChild(store);

    const slot = document.createElement("span");
    slot.className = "resolvable-value-slot";
    box.appendChild(slot);

    function renderSlot() {
      slot.innerHTML = "";
      if (kindSelect.value !== "from") {
        const input = document.createElement("input");
        input.type = "text";
        input.className = "resolvable-literal-input";
        input.placeholder = "value";
        input.value = store.value;
        input.addEventListener("input", () => (store.value = input.value));
        slot.appendChild(input);
        return;
      }

      if (store.value) {
        const chip = document.createElement("span");
        chip.className = "fe-chip fe-var-chip resolvable-var-card";
        chip.draggable = true;
        chip.title = "Drag out to remove, or drag onto another variable field to move it there";
        chip.textContent = store.value;
        // This card sits nested inside the block <li>, which is itself
        // draggable (for reordering blocks on the canvas) -- with two
        // nested draggable elements, browsers inconsistently pick the
        // OUTER one as the drag source, so grabbing the card would drag
        // the whole block instead. Suspending the parent block's
        // draggable-ness for the duration of this gesture forces the
        // card itself to be the drag source.
        chip.addEventListener("mousedown", () => {
          const parentBlock = chip.closest("li.block");
          if (!parentBlock) return;
          parentBlock.draggable = false;
          document.addEventListener("mouseup", () => (parentBlock.draggable = true), { once: true });
        });
        chip.addEventListener("dragstart", (e) => {
          e.stopPropagation();
          e.dataTransfer.setData("application/x-fe-variable", store.value);
          e.dataTransfer.setData("text/plain", store.value);
          chip.classList.add("dragging");
        });
        // Dragging this card is always a move, never a copy -- whether
        // it lands on another field (which fills itself from the same
        // dataTransfer in its own drop handler) or nowhere at all, this
        // slot empties out once the drag ends.
        chip.addEventListener("dragend", (e) => {
          e.stopPropagation();
          store.value = "";
          renderSlot();
        });
        slot.appendChild(chip);
      } else {
        const empty = document.createElement("span");
        empty.className = "resolvable-var-empty";
        empty.textContent = "drop a variable here";
        slot.appendChild(empty);
      }
    }

    // Accepts a variable chip dropped from the palette's Variables
    // section (see refreshVariablesPalette) -- switches this box to
    // "From Variable" and drops the variable card into the slot.
    slot.addEventListener("drop", (e) => {
      const varName = e.dataTransfer.getData("application/x-fe-variable");
      if (!varName) return;
      e.preventDefault();
      kindSelect.value = "from";
      store.value = varName;
      renderSlot();
    });

    kindSelect.addEventListener("change", renderSlot);
    renderSlot();

    return box;
  }

  // Only letters/digits/underscore are valid store_as names (they end up
  // as Python dict keys read back via {from: name} elsewhere) -- anything
  // else typed is silently dropped rather than accepted then rejected
  // on save.
  const STORE_AS_NAME_RE = /^[A-Za-z0-9_]+$/;

  // A comma-delimited store_as (read_sql/query's allow_multi case) as a
  // tag input: typing a name then "," turns it into a pill; the pill list
  // plus whatever's still being typed is what collectFieldValue reads
  // back out. Backspacing on an empty, just-after-a-pill input "breaks"
  // that pill back into editable text instead of deleting it outright --
  // mirrors how comma committed it in the first place.
  function buildStoreAsTagsInput(value) {
    const box = document.createElement("span");
    box.className = "store-as-tags";

    const pillsWrap = document.createElement("span");
    pillsWrap.className = "store-as-pills";
    box.appendChild(pillsWrap);

    const input = document.createElement("input");
    input.type = "text";
    input.className = "store-as-input";
    box.appendChild(input);

    function updatePlaceholder() {
      input.placeholder = pillsWrap.children.length ? "" : "name  OR  a,b,c";
    }

    function addPill(name) {
      const pill = document.createElement("span");
      pill.className = "store-as-pill";
      pill.textContent = name;
      pill.dataset.name = name;
      pillsWrap.appendChild(pill);
    }

    function commitPill() {
      const name = input.value.trim();
      input.value = "";
      if (name) addPill(name);
      updatePlaceholder();
    }

    input.addEventListener("keydown", (e) => {
      if (e.key === "," || e.key === "Enter") {
        e.preventDefault();
        commitPill();
      } else if (e.key === "Backspace" && input.value === "" && pillsWrap.children.length) {
        e.preventDefault();
        const lastPill = pillsWrap.lastElementChild;
        input.value = lastPill.dataset.name;
        lastPill.remove();
        updatePlaceholder();
      } else if (e.key === " ") {
        e.preventDefault();
      }
    });

    input.addEventListener("input", () => {
      const cleaned = input.value.replace(/[^A-Za-z0-9_]/g, "");
      if (cleaned !== input.value) input.value = cleaned;
    });

    for (const raw of Array.isArray(value) ? value : (value || "").split(",")) {
      const name = raw.trim();
      if (name && STORE_AS_NAME_RE.test(name)) addPill(name);
    }
    updatePlaceholder();

    return box;
  }

  function collectStoreAsTags(box) {
    const names = [...box.querySelectorAll(".store-as-pill")].map((p) => p.dataset.name);
    const trailing = box.querySelector(".store-as-input").value.trim();
    if (trailing) names.push(trailing);
    return names;
  }

  function collectResolvable(box) {
    const kind = box.querySelector(".resolvable-kind").value;
    const raw = box.querySelector(".resolvable-value").value;
    if (kind === "from") return { kind: "from", key: raw };
    return { kind: "literal", value: raw };
  }

  function buildCriteriaInput(value) {
    value = value || { field: "text", value: { kind: "literal", value: "" } };
    const box = document.createElement("span");
    box.className = "criteria-box";

    const fieldSelect = document.createElement("select");
    fieldSelect.className = "criteria-field";
    for (const f of ["text", "starts_with", "contains"]) {
      const opt = document.createElement("option");
      opt.value = f;
      opt.textContent = prettifyLabel(f);
      if (f === value.field) opt.selected = true;
      fieldSelect.appendChild(opt);
    }
    box.appendChild(fieldSelect);
    box.appendChild(buildResolvableInput(value.value));
    return box;
  }

  function collectCriteria(box) {
    const field = box.querySelector(".criteria-field").value;
    const resolvableBox = box.querySelector(".resolvable-box");
    return { field, value: collectResolvable(resolvableBox) };
  }

  function buildListResolvableInput(values, autoSlotted) {
    const box = document.createElement("span");
    box.className = "list-resolvable-box";
    const rows = document.createElement("div");
    rows.className = "list-resolvable-rows";
    box.appendChild(rows);

    // Populated (only for query steps -- see wireQueryParamLabels) with
    // the '?' placeholder names the selected action's .sql file expects,
    // in order. Re-applied whenever rows are added/removed so a label
    // always lines up with its actual position.
    let paramLabels = [];

    function relabelRows() {
      [...rows.children].forEach((row, i) => {
        let labelEl = row.querySelector(".list-resolvable-label");
        const text = paramLabels[i];
        if (text) {
          if (!labelEl) {
            labelEl = document.createElement("span");
            labelEl.className = "list-resolvable-label";
            row.insertBefore(labelEl, row.firstChild);
          }
          labelEl.textContent = text;
        } else if (labelEl) {
          labelEl.remove();
        }
      });
    }

    function addRow(v) {
      const row = document.createElement("div");
      row.className = "list-resolvable-row";
      row.appendChild(buildResolvableInput(v));
      // A params field's row count is driven entirely by the query's own
      // declared placeholders (see setParamLabels below) -- no manual
      // add/remove, since one always exists per '?' whether the user
      // likes it or not.
      if (!autoSlotted) {
        const rm = document.createElement("button");
        rm.type = "button";
        rm.textContent = "×";
        rm.addEventListener("click", () => {
          row.remove();
          relabelRows();
        });
        row.appendChild(rm);
      }
      rows.appendChild(row);
      relabelRows();
    }

    for (const v of values || []) addRow(v);

    if (!autoSlotted) {
      const addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.textContent = "+ value";
      addBtn.addEventListener("click", () => addRow({ kind: "literal", value: "" }));
      box.appendChild(addBtn);
    }

    box.setParamLabels = (labels) => {
      paramLabels = labels || [];
      // Sync row count to the known param count -- a freshly dragged
      // block starts with zero rows, so without this the labels would
      // have nothing to attach to and no slots would ever appear.
      while (rows.children.length < paramLabels.length) addRow({ kind: "literal", value: "" });
      while (rows.children.length > paramLabels.length) rows.lastElementChild.remove();
      relabelRows();
    };

    return box;
  }

  function collectListResolvable(box) {
    return [...box.querySelectorAll(":scope > .list-resolvable-rows > .list-resolvable-row")].map((row) =>
      collectResolvable(row.querySelector(".resolvable-box"))
    );
  }

  function buildTapIfClosedInput(value) {
    const box = document.createElement("span");
    box.className = "tap-if-closed-box";

    const enableCb = document.createElement("input");
    enableCb.type = "checkbox";
    enableCb.className = "tif-enable";
    enableCb.checked = !!value;
    box.appendChild(enableCb);

    const belowInput = document.createElement("input");
    belowInput.type = "text";
    belowInput.className = "tif-below";
    belowInput.placeholder = "below label";
    belowInput.value = value ? value.below || "" : "";
    box.appendChild(belowInput);

    return box;
  }

  function collectTapIfClosed(box) {
    if (!box.querySelector(".tif-enable").checked) return null;
    const below = box.querySelector(".tif-below").value;
    if (!below) return null;
    return { below, control_class: "android.widget.Button", occurrence: 1 };
  }

  // ------------------------------------------------------------------
  // Modifiers (shared across every block type)
  // ------------------------------------------------------------------

  const MODIFIER_HELP = {
    if_visible:
      "Only run this step if the given text is visible on screen first. Useful for optional UI (e.g. a printer picker) that doesn't always appear.",
    if_visible_timeout:
      "How long (seconds) to wait for the If Visible text to appear before deciding it's absent and skipping this step. Only matters when If Visible is checked.",
    retries: "How many extra attempts to make if this step fails, before giving up.",
    retry_delay: "Seconds to wait between retry attempts.",
    required:
      "If this step still fails after all retries, abort the rest of this flow immediately instead of logging a failure and moving on to the next step.",
    comment: "A note attached to this step for readers of the saved YAML -- documentation only, never affects how the step runs.",
  };

  function buildModifiersArea(block) {
    const details = document.createElement("details");
    details.className = "block-modifiers";
    const summary = document.createElement("summary");
    summary.textContent = "Modifiers (If Visible, Retries, Required, Comment)";
    details.appendChild(summary);

    const m = block.modifiers || {};

    const grid = document.createElement("div");
    grid.className = "modifiers-grid";

    // if_visible
    const ivRow = document.createElement("div");
    ivRow.className = "field-row";
    ivRow.dataset.mod = "if_visible";
    ivRow.appendChild(makeFieldLabel(prettifyLabel("if_visible"), MODIFIER_HELP.if_visible));
    ivRow.appendChild(buildCriteriaInput(m.if_visible ? { field: m.if_visible.field, value: m.if_visible.value } : null));
    const ivEnableCb = document.createElement("input");
    ivEnableCb.type = "checkbox";
    ivEnableCb.className = "mod-if-visible-enable";
    ivEnableCb.checked = !!m.if_visible;
    ivRow.insertBefore(ivEnableCb, ivRow.firstChild);
    grid.appendChild(ivRow);

    const ivTimeoutRow = document.createElement("div");
    ivTimeoutRow.className = "field-row";
    ivTimeoutRow.dataset.mod = "if_visible_timeout";
    ivTimeoutRow.appendChild(makeFieldLabel(prettifyLabel("if_visible_timeout"), MODIFIER_HELP.if_visible_timeout));
    const ivtInput = document.createElement("input");
    ivtInput.type = "number";
    ivtInput.step = "any";
    ivtInput.value = m.if_visible_timeout ?? "";
    ivTimeoutRow.appendChild(ivtInput);
    grid.appendChild(ivTimeoutRow);

    function syncIvTimeoutVisibility() {
      ivTimeoutRow.style.display = ivEnableCb.checked ? "" : "none";
    }
    ivEnableCb.addEventListener("change", syncIvTimeoutVisibility);
    syncIvTimeoutVisibility();

    // retries / retry_delay
    const retriesRow = document.createElement("div");
    retriesRow.className = "field-row";
    retriesRow.dataset.mod = "retries";
    retriesRow.appendChild(makeFieldLabel(prettifyLabel("retries"), MODIFIER_HELP.retries));
    const rInput = document.createElement("input");
    rInput.type = "number";
    rInput.value = m.retries ?? "";
    retriesRow.appendChild(rInput);
    grid.appendChild(retriesRow);

    const retryDelayRow = document.createElement("div");
    retryDelayRow.className = "field-row";
    retryDelayRow.dataset.mod = "retry_delay";
    retryDelayRow.appendChild(makeFieldLabel(prettifyLabel("retry_delay"), MODIFIER_HELP.retry_delay));
    const rdInput = document.createElement("input");
    rdInput.type = "number";
    rdInput.step = "any";
    rdInput.value = m.retry_delay ?? "";
    retryDelayRow.appendChild(rdInput);
    grid.appendChild(retryDelayRow);

    // required
    const reqRow = document.createElement("div");
    reqRow.className = "field-row";
    reqRow.dataset.mod = "required";
    reqRow.appendChild(makeFieldLabel(prettifyLabel("required"), MODIFIER_HELP.required));
    const reqInput = document.createElement("input");
    reqInput.type = "checkbox";
    reqInput.checked = !!m.required;
    reqRow.appendChild(reqInput);
    grid.appendChild(reqRow);

    details.appendChild(grid);

    // comment (own row, full width -- literal text or repeatable parts)
    const commentRow = document.createElement("div");
    commentRow.className = "field-row comment-row";
    commentRow.dataset.mod = "comment";
    commentRow.appendChild(makeFieldLabel(prettifyLabel("comment"), MODIFIER_HELP.comment));
    commentRow.appendChild(buildCommentInput(m.comment));
    details.appendChild(commentRow);

    return details;
  }

  function buildCommentInput(comment) {
    const box = document.createElement("span");
    box.className = "comment-box";

    const modeSelect = document.createElement("select");
    modeSelect.className = "comment-mode";
    for (const m of ["none", "text", "parts"]) {
      const opt = document.createElement("option");
      opt.value = m;
      opt.textContent = prettifyLabel(m);
      modeSelect.appendChild(opt);
    }
    modeSelect.value = comment ? comment.kind : "none";
    box.appendChild(modeSelect);

    const textInput = document.createElement("input");
    textInput.type = "text";
    textInput.className = "comment-text";
    textInput.placeholder = "comment text";
    textInput.value = comment && comment.kind === "text" ? comment.value : "";
    box.appendChild(textInput);

    const partsBox = document.createElement("div");
    partsBox.className = "comment-parts";
    function addPart(part) {
      const row = document.createElement("div");
      row.className = "comment-part-row";
      const kindSelect = document.createElement("select");
      for (const k of ["text", "from"]) {
        const opt = document.createElement("option");
        opt.value = k;
        opt.textContent = k;
        kindSelect.appendChild(opt);
      }
      kindSelect.value = part ? part.kind : "text";
      row.appendChild(kindSelect);
      const valInput = document.createElement("input");
      valInput.type = "text";
      valInput.value = part ? (part.kind === "from" ? part.key : part.value) : "";
      row.appendChild(valInput);
      const rm = document.createElement("button");
      rm.type = "button";
      rm.textContent = "×";
      rm.addEventListener("click", () => row.remove());
      row.appendChild(rm);
      partsBox.appendChild(row);
    }
    if (comment && comment.kind === "parts") {
      for (const p of comment.parts) addPart(p);
    }
    const addPartBtn = document.createElement("button");
    addPartBtn.type = "button";
    addPartBtn.textContent = "+ part";
    addPartBtn.addEventListener("click", () => addPart(null));
    box.appendChild(partsBox);
    box.appendChild(addPartBtn);

    function syncVisibility() {
      textInput.style.display = modeSelect.value === "text" ? "" : "none";
      partsBox.style.display = modeSelect.value === "parts" ? "" : "none";
      addPartBtn.style.display = modeSelect.value === "parts" ? "" : "none";
    }
    modeSelect.addEventListener("change", syncVisibility);
    syncVisibility();

    return box;
  }

  function collectComment(box) {
    const mode = box.querySelector(".comment-mode").value;
    if (mode === "none") return null;
    if (mode === "text") {
      return { kind: "text", value: box.querySelector(".comment-text").value };
    }
    const parts = [...box.querySelectorAll(".comment-part-row")].map((row) => {
      const kind = row.querySelector("select").value;
      const val = row.querySelector("input").value;
      return kind === "from" ? { kind: "from", key: val } : { kind: "text", value: val };
    });
    return { kind: "parts", parts };
  }

  // ------------------------------------------------------------------
  // Collection: DOM -> JSON block tree (inverse of build*, symmetric)
  // ------------------------------------------------------------------

  function collectFieldValue(fieldSpec, row) {
    const inputWrap = row.querySelector(":scope > .field-input");
    switch (fieldSpec.kind) {
      case "text":
        return inputWrap.querySelector("input").value;
      case "int": {
        const v = inputWrap.querySelector("input").value;
        return v === "" ? null : parseInt(v, 10);
      }
      case "float": {
        const v = inputWrap.querySelector("input").value;
        return v === "" ? null : parseFloat(v);
      }
      case "bool":
        return inputWrap.querySelector("input").checked;
      case "select":
        return inputWrap.querySelector("select").value;
      case "store_as": {
        if (fieldSpec.allow_multi) {
          return collectStoreAsTags(inputWrap.querySelector(".store-as-tags"));
        }
        return inputWrap.querySelector("input").value;
      }
      case "resolvable":
        return collectResolvable(inputWrap.querySelector(".resolvable-box"));
      case "criteria":
        return collectCriteria(inputWrap.querySelector(".criteria-box"));
      case "list_resolvable":
        return collectListResolvable(inputWrap.querySelector(".list-resolvable-box"));
      case "tap_if_closed":
        return collectTapIfClosed(inputWrap.querySelector(".tap-if-closed-box"));
      default:
        return inputWrap.querySelector("input").value;
    }
  }

  function collectModifiers(li) {
    const details = li.querySelector(":scope > .block-modifiers");
    if (!details) {
      return { if_visible: null, if_visible_timeout: null, retries: null, retry_delay: null, required: false, comment: null };
    }
    const ivRow = details.querySelector('[data-mod="if_visible"]');
    const ivEnabled = ivRow.querySelector(".mod-if-visible-enable").checked;
    const ivCriteria = ivEnabled ? collectCriteria(ivRow.querySelector(".criteria-box")) : null;

    const ivTimeoutRaw = details.querySelector('[data-mod="if_visible_timeout"] input').value;
    const retriesRaw = details.querySelector('[data-mod="retries"] input').value;
    const retryDelayRaw = details.querySelector('[data-mod="retry_delay"] input').value;
    const required = details.querySelector('[data-mod="required"] input').checked;
    const comment = collectComment(details.querySelector('[data-mod="comment"] .comment-box'));

    return {
      if_visible: ivCriteria ? { field: ivCriteria.field, value: ivCriteria.value } : null,
      if_visible_timeout: ivTimeoutRaw === "" ? null : parseFloat(ivTimeoutRaw),
      retries: retriesRaw === "" ? null : parseInt(retriesRaw, 10),
      retry_delay: retryDelayRaw === "" ? null : parseFloat(retryDelayRaw),
      required,
      comment,
    };
  }

  const EMPTY_MODIFIERS = {
    if_visible: null, if_visible_timeout: null, retries: null, retry_delay: null, required: false, comment: null,
  };

  function collectBlock(li) {
    const type = li.dataset.type;
    if (type === "__inject__") {
      const fieldsArea = li.querySelector(":scope > .block-fields");
      const row = fieldsArea && fieldsArea.querySelector(':scope > [data-field="name"]');
      const name = row ? collectFieldValue(INJECT_FIELD_SPECS[0], row) : "";
      return { id: li.dataset.id, type, fields: { name }, modifiers: { ...EMPTY_MODIFIERS }, children: [] };
    }

    const fields = {};
    const spec = STEP_TYPES[type];
    if (spec) {
      const fieldsArea = li.querySelector(":scope > .block-fields");
      for (const fieldSpec of spec.fields) {
        const row = fieldsArea.querySelector(`:scope > [data-field="${fieldSpec.name}"]`);
        fields[fieldSpec.name] = collectFieldValue(fieldSpec, row);
      }
    }

    const isBase = type === "base" || (spec && spec.base_of);
    let children = [];
    if (isBase) {
      children = collectInjectSlots(li);
    } else if (spec && spec.container) {
      const childCanvas = li.querySelector(":scope > .block-children-wrap > .block-children");
      for (const childLi of childCanvas.children) {
        children.push(collectBlock(childLi));
      }
    }

    return {
      id: li.dataset.id,
      type,
      fields,
      modifiers: collectModifiers(li),
      children,
    };
  }

  // Inverse of buildInjectSlotsWrap -- each .inject-slot's own canvas
  // collects back into a __inject_slot__ pseudo-block carrying the same
  // slot name it was rendered with (read from the canvas itself, not
  // re-derived, since a slot has no header/fields UI of its own).
  function collectInjectSlots(li) {
    const slots = [];
    for (const canvas of li.querySelectorAll(":scope > .block-children-wrap > .inject-slot > .block-children")) {
      const children = [...canvas.children].map(collectBlock);
      slots.push({
        id: null,
        type: "__inject_slot__",
        fields: { name: canvas.dataset.injectSlotName || "" },
        modifiers: { ...EMPTY_MODIFIERS },
        children,
      });
    }
    return slots;
  }

  function canvasToBlocks(canvasEl) {
    return [...canvasEl.children].map(collectBlock);
  }

  // ------------------------------------------------------------------
  // Drag and drop (delegated on the editor panel so nested canvases
  // created/destroyed dynamically don't each need their own listener)
  // ------------------------------------------------------------------

  let draggingEl = null; // an existing canvas block being moved
  let draggingType = null; // a palette chip being dropped (creates new)

  // Auto-scrolls whichever scrollable drag-relevant container (the
  // canvas, or the palette) the cursor is currently within, when it's
  // close enough to that container's top/bottom edge -- both are tall,
  // often-overflowing lists, and without this a block/chip out of view
  // is simply unreachable mid-drag. Nested .block-children containers
  // never need their own entry here: they don't scroll independently
  // (max-height: none), they just grow #fe-canvas's total content height.
  const AUTOSCROLL_EDGE = 40;
  const AUTOSCROLL_MAX_SPEED = 18;

  function autoScrollOnDrag(e) {
    for (const el of [document.getElementById("fe-canvas"), document.getElementById("fe-palette")]) {
      if (!el) continue;
      const rect = el.getBoundingClientRect();
      if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) continue;

      if (e.clientY < rect.top + AUTOSCROLL_EDGE) {
        el.scrollTop -= AUTOSCROLL_MAX_SPEED * (1 - (e.clientY - rect.top) / AUTOSCROLL_EDGE);
      } else if (e.clientY > rect.bottom - AUTOSCROLL_EDGE) {
        el.scrollTop += AUTOSCROLL_MAX_SPEED * (1 - (rect.bottom - e.clientY) / AUTOSCROLL_EDGE);
      }
    }
  }

  function initDnd() {
    const panel = document.getElementById("tab-flow-editor");

    panel.addEventListener("dragover", (e) => {
      autoScrollOnDrag(e);

      // Dragging an existing canvas block over the palette (out to
      // delete it, or just back where it came from -- both read the
      // same to the user) previews as a delete, not a reorder.
      const paletteZone = e.target.closest("#fe-palette-col");
      if (paletteZone && draggingEl) {
        e.preventDefault();
        paletteZone.classList.add("delete-target");
        return;
      }

      const canvas = e.target.closest(".canvas");
      if (!canvas) return;
      e.preventDefault();
      canvas.classList.add("drag-over");

      if (draggingEl) {
        const movable = [...canvas.querySelectorAll(":scope > .block:not(.dragging)")];
        const after = movable.find(
          (el) => e.clientY <= el.getBoundingClientRect().top + el.getBoundingClientRect().height / 2
        );
        if (after) canvas.insertBefore(draggingEl, after);
        else canvas.appendChild(draggingEl);
      }
    });

    panel.addEventListener("dragleave", (e) => {
      const paletteZone = e.target.closest("#fe-palette-col");
      if (paletteZone) paletteZone.classList.remove("delete-target");
      const canvas = e.target.closest(".canvas");
      if (canvas) canvas.classList.remove("drag-over");
    });

    panel.addEventListener("drop", (e) => {
      const paletteZone = e.target.closest("#fe-palette-col");
      if (paletteZone && draggingEl) {
        e.preventDefault();
        paletteZone.classList.remove("delete-target");
        draggingEl.remove();
        draggingEl = null;
        return;
      }

      const canvas = e.target.closest(".canvas");
      if (!canvas) return;
      e.preventDefault();
      canvas.classList.remove("drag-over");
      // The dragover handler above already live-positioned draggingEl
      // (new-from-palette or an existing block being reordered alike)
      // exactly where it belongs -- nothing left to do here but clear
      // drag state.
      draggingEl = null;
      draggingType = null;
    });
  }

  // ------------------------------------------------------------------
  // Load / New / Save
  // ------------------------------------------------------------------

  async function refreshFlowPicker() {
    const res = await fetch("/api/flows/list");
    const data = await res.json();
    const select = document.getElementById("fe-flow-select");
    select.innerHTML = "";

    const flowGroup = document.createElement("optgroup");
    flowGroup.label = "Flows";
    for (const f of data.flows) {
      const opt = document.createElement("option");
      opt.value = f.path;
      opt.textContent = f.name || f.path;
      flowGroup.appendChild(opt);
    }
    select.appendChild(flowGroup);

    // Base files get their own optgroup per declared section (same
    // "Parent\Child" convention as a flow's own section: field, see
    // buildSectionTree in index.html) instead of one flat list -- lets
    // e.g. "Inventory Mgmt\Pallet Build" base files sit together,
    // distinct from any other menu area's shared steps. Uncategorized
    // ones (no section set) fall into a single "Base files" group last.
    const baseBySection = new Map();
    for (const f of data.base) {
      const section = (f.section || "").trim();
      if (!baseBySection.has(section)) baseBySection.set(section, []);
      baseBySection.get(section).push(f);
    }
    const sortedSections = [...baseBySection.keys()].sort((a, b) => {
      if (!a) return 1;
      if (!b) return -1;
      return a.localeCompare(b);
    });
    for (const section of sortedSections) {
      const baseGroup = document.createElement("optgroup");
      baseGroup.label = section ? `Base: ${section.replace(/\\/g, " / ")}` : "Base files";
      for (const f of baseBySection.get(section)) {
        const opt = document.createElement("option");
        opt.value = f.path;
        opt.textContent = f.name || f.path;
        baseGroup.appendChild(opt);
      }
      select.appendChild(baseGroup);
    }

    if (window.FE_DYNAMIC_CHOICES) {
      window.FE_DYNAMIC_CHOICES.base_files = data.base.map((f) => f.path.split("/").pop());
    }

    return data;
  }

  async function loadFlow(rel) {
    const res = await fetch(`/api/flows/${rel}`);
    if (!res.ok) {
      setFeStatus("Error loading flow: " + (await res.json()).error);
      return;
    }
    const envelope = await res.json();
    currentRelPath = rel;
    editingBase = rel.startsWith("base/");
    buildPalette();

    document.getElementById("fe-name").value = envelope.name || "";
    document.getElementById("fe-description").value = envelope.description || "";
    document.getElementById("fe-section").value = envelope.section || "";

    const canvas = document.getElementById("fe-canvas");
    canvas.innerHTML = "";
    for (const block of envelope.steps) {
      canvas.appendChild(buildBlockNode(block));
    }
    resetUndoHistory();
    setFeStatus(`Loaded ${rel}`);

    const select = document.getElementById("fe-flow-select");
    if (select) select.value = rel;
    if (window.FE_URL_STATE) window.FE_URL_STATE.set("flow", rel);

    refreshUsedByIndicator();
  }

  // Dry-run/lint pass over every flow AND base file on disk (see
  // /api/lint, app.py's lint_flow_steps) -- no Appium/device involved,
  // just statically checking every base/query/action reference is still
  // valid and every group/loop is non-empty. Catches "renamed a query,
  // forgot the 3 flows using it" before someone hits it mid-run.
  async function runLint() {
    const overlay = document.getElementById("fe-lint-overlay");
    const summary = document.getElementById("fe-lint-summary");
    const results = document.getElementById("fe-lint-results");
    overlay.hidden = false;
    summary.textContent = "Checking every flow and base file...";
    results.innerHTML = "";

    let data;
    try {
      const res = await fetch("/api/lint");
      data = await res.json();
    } catch (e) {
      summary.textContent = "Could not run the lint check.";
      return;
    }

    const problemCount = data.results.reduce((n, r) => n + r.problems.length, 0);
    if (!data.results.length) {
      summary.classList.add("unused");
      summary.textContent = `Checked ${data.checked} flow/base file(s) -- no problems found.`;
      return;
    }
    summary.classList.remove("unused");
    summary.textContent =
      `Checked ${data.checked} flow/base file(s) -- ${problemCount} problem(s) in ${data.results.length} file(s).`;

    for (const r of data.results) {
      const rel = r.path.replace(/^flows\//, "");
      const flowDiv = document.createElement("div");
      flowDiv.className = "fe-lint-flow";

      const nameDiv = document.createElement("div");
      nameDiv.className = "fe-lint-flow-name";
      const a = document.createElement("a");
      a.href = "#";
      a.textContent = r.path;
      a.dataset.loadRel = rel;
      nameDiv.appendChild(a);
      flowDiv.appendChild(nameDiv);

      const ul = document.createElement("ul");
      ul.className = "fe-lint-problems";
      for (const p of r.problems) {
        const li = document.createElement("li");
        li.textContent = p;
        ul.appendChild(li);
      }
      flowDiv.appendChild(ul);
      results.appendChild(flowDiv);
    }
  }

  // Shown only while editing a base file -- which flows (or other base
  // files) actually call it, so renaming/deleting one doesn't silently
  // break something else. See /api/base-files/<filename>/used-by
  // (app.py's references_base_file, walking every flow/base file's
  // steps recursively).
  async function refreshUsedByIndicator() {
    const box = document.getElementById("fe-used-by");
    if (!editingBase || !currentRelPath) {
      box.hidden = true;
      return;
    }
    const filename = currentRelPath.split("/").pop();
    box.hidden = false;
    box.classList.remove("unused");
    box.textContent = "Checking what uses this base file...";
    try {
      const res = await fetch(`/api/base-files/${encodeURIComponent(filename)}/used-by`);
      const data = await res.json();
      const usages = data.usages || [];
      if (!usages.length) {
        box.classList.add("unused");
        box.textContent = "Not used by any flow or base file yet.";
        return;
      }
      box.textContent = "Used by: ";
      usages.forEach((u, i) => {
        if (i > 0) box.appendChild(document.createTextNode(", "));
        const a = document.createElement("a");
        a.href = "#";
        a.textContent = u.name;
        a.dataset.loadRel = u.path.replace(/^flows\//, "");
        box.appendChild(a);
      });
    } catch (e) {
      box.textContent = "Could not check what uses this base file.";
    }
  }

  async function saveFlow() {
    if (!currentRelPath) {
      setFeStatus("Nothing loaded to save -- Load or create a flow first.");
      return;
    }
    const canvas = document.getElementById("fe-canvas");
    const envelope = {
      name: document.getElementById("fe-name").value,
      description: document.getElementById("fe-description").value,
      section: document.getElementById("fe-section").value,
      steps: canvasToBlocks(canvas),
    };
    setFeStatus("Saving...");
    const res = await fetch(`/api/flows/${currentRelPath}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(envelope),
    });
    const body = await res.json();
    if (!res.ok) {
      setFeStatus("Error saving: " + (body.problems ? body.problems.join("; ") : body.error));
      return;
    }
    setFeStatus(`Saved ${body.path}`);
    notifyFlowsChanged();
    // A base file's name/section: drive its own palette pill's
    // label/subsection (see build_base_step_types) -- refetch so an
    // edit to either shows up on the pill immediately, same as
    // promoting/removing a verb already does.
    if (currentRelPath.startsWith("base/")) await refreshStepTypes();
  }

  async function newFlow(kind) {
    const filename = prompt(`Filename for the new ${kind} (e.g. my_flow.yaml):`);
    if (!filename) return;
    const res = await fetch("/api/flows/new", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, filename }),
    });
    const body = await res.json();
    if (!res.ok) {
      setFeStatus("Error creating flow: " + body.error);
      return;
    }
    await refreshFlowPicker();
    const rel = kind === "base" ? `base/${filename}` : filename;
    document.getElementById("fe-flow-select").value = rel;
    await loadFlow(rel);
    notifyFlowsChanged();
    // A brand-new base file needs its own palette pill to exist before
    // it's usable from the palette -- without this it saved fine but
    // just never showed up anywhere, which reads as "didn't work".
    if (kind === "base") await refreshStepTypes();
  }

  async function renameFlow() {
    if (!currentRelPath) {
      setFeStatus("Nothing loaded to rename -- Load or create a flow first.");
      return;
    }
    const currentName = currentRelPath.split("/").pop();
    const newName = prompt("New filename (e.g. my_flow.yaml):", currentName);
    if (!newName || newName === currentName) return;

    if (editingBase) {
      try {
        const usedByRes = await fetch(`/api/base-files/${encodeURIComponent(currentName)}/used-by`);
        const usages = (await usedByRes.json()).usages || [];
        if (usages.length) {
          const names = usages.map((u) => u.name).join(", ");
          if (!confirm(`${currentName} is used by: ${names}. Renaming it will NOT update those references -- they'll break. Rename anyway?`)) {
            return;
          }
        }
      } catch (e) {
        // Best-effort check -- an unreachable lookup shouldn't block renaming.
      }
    }

    const res = await fetch(`/api/flows/${currentRelPath}/rename`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: newName }),
    });
    const body = await res.json();
    if (!res.ok) {
      setFeStatus("Error renaming: " + body.error);
      return;
    }
    setFeStatus(`Renamed to ${body.path}`);
    notifyFlowsChanged();
    await refreshFlowPicker();
    document.getElementById("fe-flow-select").value = body.path;
    await loadFlow(body.path);
    if (body.path.startsWith("base/")) await refreshStepTypes();
  }

  async function deleteFlow() {
    if (!currentRelPath) {
      setFeStatus("Nothing loaded to delete -- Load a flow first.");
      return;
    }

    let confirmMsg = `Delete ${currentRelPath}? This cannot be undone.`;
    if (editingBase) {
      const filename = currentRelPath.split("/").pop();
      try {
        const res = await fetch(`/api/base-files/${encodeURIComponent(filename)}/used-by`);
        const usages = (await res.json()).usages || [];
        if (usages.length) {
          confirmMsg =
            `${currentRelPath} is still used by: ${usages.map((u) => u.name).join(", ")}. ` +
            `Deleting it will break those. Delete anyway?`;
        }
      } catch (e) {
        // Used-by check is best-effort -- an unreachable check shouldn't
        // block deletion, just falls back to the plain confirm below.
      }
    }
    if (!confirm(confirmMsg)) return;

    const res = await fetch(`/api/flows/${currentRelPath}/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const body = await res.json();
    if (!res.ok) {
      setFeStatus("Error deleting: " + body.error);
      return;
    }
    setFeStatus(`Deleted ${body.path}`);
    notifyFlowsChanged();
    const deletedWasBase = currentRelPath.startsWith("base/");
    currentRelPath = null;
    editingBase = false;
    document.getElementById("fe-name").value = "";
    document.getElementById("fe-description").value = "";
    document.getElementById("fe-section").value = "";
    document.getElementById("fe-canvas").innerHTML = "";
    document.getElementById("fe-used-by").hidden = true;
    resetUndoHistory();
    await refreshFlowPicker();
    if (deletedWasBase) await refreshStepTypes();
  }

  function setFeStatus(msg) {
    document.getElementById("fe-status").textContent = msg;
  }

  // Tells the Run Tests tab's Available-tests tree (a separate script,
  // same page) to refetch /api/state -- any save/rename/delete/new here
  // can change what's selectable there or which section it's grouped
  // under, and both tabs stay on the same page without a reload.
  function notifyFlowsChanged() {
    document.dispatchEvent(new CustomEvent("flows-changed"));
  }

  // ------------------------------------------------------------------
  // Variables palette -- lists every store_as name currently on the
  // canvas as a draggable chip, so a later step's "From Variable"
  // resolvable field can be filled by dropping the chip onto it
  // instead of retyping the name (see the drop listener in
  // buildResolvableInput).
  // ------------------------------------------------------------------

  function refreshVariablesPalette() {
    const listEl = document.getElementById("fe-var-list");
    const canvas = document.getElementById("fe-canvas");
    if (!listEl || !canvas) return;

    const names = new Set();
    for (const row of canvas.querySelectorAll('.field-row[data-kind="store_as"]')) {
      const tagsBox = row.querySelector(".store-as-tags");
      if (tagsBox) {
        // allow_multi store_as (read_sql/query) -- already-committed names
        // are pills, not <input> text, so collectStoreAsTags is the only
        // thing that sees the full picture (pills + whatever's still
        // being typed).
        for (const name of collectStoreAsTags(tagsBox)) names.add(name);
        continue;
      }
      const input = row.querySelector("input");
      if (input && input.value.trim()) names.add(input.value.trim());
    }

    listEl.innerHTML = "";
    if (!names.size) {
      const empty = document.createElement("div");
      empty.className = "fe-var-empty";
      empty.textContent = "Nothing stored yet";
      listEl.appendChild(empty);
    } else {
      for (const name of [...names].sort()) {
        listEl.appendChild(makeVariableChip(name));
      }
    }
    applyPaletteFilter();
  }

  function initVariablesTracking() {
    const panel = document.getElementById("tab-flow-editor");
    panel.addEventListener("input", (e) => {
      if (e.target.closest('.field-row[data-kind="store_as"]')) refreshVariablesPalette();
    });

    const canvas = document.getElementById("fe-canvas");
    new MutationObserver(refreshVariablesPalette).observe(canvas, { childList: true, subtree: true });
  }

  // ------------------------------------------------------------------
  // Undo / redo -- whole-canvas JSON snapshots rather than granular
  // command objects, since the canvas already round-trips cleanly
  // through canvasToBlocks/buildBlockNode and a drag/drop editor has too
  // many distinct mutation shapes (reorder, nest, field edit, delete,
  // duplicate, drag-to-palette-delete...) to track individually. A
  // snapshot is pushed (debounced) after any field edit or structural
  // change; Ctrl+Z/Ctrl+Y step through that history.
  // ------------------------------------------------------------------
  let undoStack = [];
  let redoStack = [];
  let suppressSnapshot = false;
  let snapshotDebounce = null;

  function currentCanvasJson() {
    return JSON.stringify(canvasToBlocks(document.getElementById("fe-canvas")));
  }

  // Called once right after a flow finishes loading -- the freshly
  // loaded state becomes undo history's baseline, so the very first
  // Ctrl+Z doesn't have nothing-but-empty to fall back to.
  function resetUndoHistory() {
    undoStack = [currentCanvasJson()];
    redoStack = [];
  }

  function pushUndoSnapshot() {
    if (suppressSnapshot) return;
    const snapshot = currentCanvasJson();
    if (snapshot === undoStack[undoStack.length - 1]) return; // nothing actually changed
    undoStack.push(snapshot);
    if (undoStack.length > 100) undoStack.shift();
    redoStack = [];
  }

  function scheduleUndoSnapshot() {
    clearTimeout(snapshotDebounce);
    snapshotDebounce = setTimeout(pushUndoSnapshot, 400);
  }

  function applyCanvasSnapshot(json) {
    suppressSnapshot = true;
    const canvas = document.getElementById("fe-canvas");
    canvas.innerHTML = "";
    for (const block of JSON.parse(json)) {
      canvas.appendChild(buildBlockNode(block));
    }
    // Rendering above itself trips the MutationObserver that schedules a
    // snapshot -- release the suppression on the next tick, after that
    // debounce would have already been (harmlessly) re-armed, rather
    // than racing it.
    setTimeout(() => {
      suppressSnapshot = false;
    }, 0);
  }

  function undo() {
    clearTimeout(snapshotDebounce);
    pushUndoSnapshot(); // capture any pending edit first, so it isn't lost
    if (undoStack.length < 2) return;
    redoStack.push(undoStack.pop());
    applyCanvasSnapshot(undoStack[undoStack.length - 1]);
    setFeStatus("Undo");
  }

  function redo() {
    if (!redoStack.length) return;
    const snapshot = redoStack.pop();
    undoStack.push(snapshot);
    applyCanvasSnapshot(snapshot);
    setFeStatus("Redo");
  }

  function initUndoRedo() {
    const canvas = document.getElementById("fe-canvas");
    canvas.addEventListener("input", scheduleUndoSnapshot);
    canvas.addEventListener("change", scheduleUndoSnapshot);
    new MutationObserver(scheduleUndoSnapshot).observe(canvas, { childList: true, subtree: true });

    document.addEventListener("keydown", (e) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      const panel = document.getElementById("tab-flow-editor");
      if (!panel.classList.contains("active")) return;
      // A field mid-edit keeps its own native undo (e.g. a text input
      // the user just typed into) rather than being hijacked into
      // undoing the whole canvas structure instead.
      const active = document.activeElement;
      const isEditingText = active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA");
      if (isEditingText) return;

      const key = e.key.toLowerCase();
      if (key === "z" && !e.shiftKey) {
        e.preventDefault();
        undo();
      } else if (key === "y" || (key === "z" && e.shiftKey)) {
        e.preventDefault();
        redo();
      }
    });
  }

  function makeVariableChip(name) {
    const chip = document.createElement("div");
    chip.className = "fe-chip fe-var-chip";
    chip.textContent = name;
    chip.draggable = true;
    chip.title = "Drag onto a \"From Variable\" field";
    chip.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("application/x-fe-variable", name);
      e.dataTransfer.setData("text/plain", name);
      draggingEl = null;
      draggingType = null;
      chip.classList.add("dragging");
    });
    chip.addEventListener("dragend", () => chip.classList.remove("dragging"));
    return chip;
  }

  // Exposed ONLY for the Playwright harness under .scratch_test/ -- not
  // used by the real page (see init() below, which drives everything
  // through DOM events instead). Lets a test build/collect blocks
  // directly without simulating real drag-and-drop.
  window.__flowEditorTestHooks = { buildBlockNode, canvasToBlocks, emptyBlock };

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------

  async function init() {
    buildPalette();
    initDnd();
    initVariablesTracking();
    initUndoRedo();

    window.FE_DYNAMIC_CHOICES = {};
    try {
      const res = await fetch("/api/queries");
      const data = await res.json();
      // /api/queries returns [{name, updated}, ...] (see the Queries
      // tab's sidebar, which needs the mtime) -- this dropdown only
      // wants the bare filenames.
      window.FE_DYNAMIC_CHOICES.queries = (data.queries || []).map((q) => q.name);
    } catch (e) {
      window.FE_DYNAMIC_CHOICES.queries = [];
    }

    await refreshFlowPicker();

    // Reopens whatever flow was loaded before a refresh/reload, if the
    // URL still names one that exists (see window.FE_URL_STATE, set by
    // loadFlow itself on every successful load).
    const urlFlow = window.FE_URL_STATE && window.FE_URL_STATE.get("flow");
    if (urlFlow && document.querySelector(`#fe-flow-select option[value="${CSS.escape(urlFlow)}"]`)) {
      await loadFlow(urlFlow);
    }

    // Loads automatically as soon as a flow/base file is picked -- no
    // separate Load button (see the New split-button just below for the
    // same "fewer standalone buttons" treatment).
    document.getElementById("fe-flow-select").addEventListener("change", (e) => {
      if (e.target.value) loadFlow(e.target.value);
    });
    document.getElementById("fe-used-by").addEventListener("click", (e) => {
      const link = e.target.closest("a[data-load-rel]");
      if (!link) return;
      e.preventDefault();
      loadFlow(link.dataset.loadRel);
    });
    document.getElementById("fe-save-btn").addEventListener("click", saveFlow);

    // "New" split-button -- New Flow/New Base File tucked behind one
    // dropdown instead of two standalone buttons (mirrors the Queries
    // tab's "..." overflow menu pattern -- see .q-more-wrap/.q-more-menu).
    const feNewBtn = document.getElementById("fe-new-btn");
    const feNewMenu = document.getElementById("fe-new-menu");
    feNewBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      feNewMenu.hidden = !feNewMenu.hidden;
    });
    document.addEventListener("click", (e) => {
      if (!feNewMenu.hidden && !e.target.closest(".q-more-wrap")) feNewMenu.hidden = true;
    });
    document.getElementById("fe-new-flow-btn").addEventListener("click", () => {
      feNewMenu.hidden = true;
      newFlow("flow");
    });
    document.getElementById("fe-new-base-btn").addEventListener("click", () => {
      feNewMenu.hidden = true;
      newFlow("base");
    });
    document.getElementById("fe-rename-btn").addEventListener("click", renameFlow);
    document.getElementById("fe-delete-btn").addEventListener("click", deleteFlow);
    document.getElementById("fe-lint-btn").addEventListener("click", runLint);
    document.getElementById("fe-lint-close").addEventListener("click", () => {
      document.getElementById("fe-lint-overlay").hidden = true;
    });
    document.getElementById("fe-lint-results").addEventListener("click", (e) => {
      const link = e.target.closest("a[data-load-rel]");
      if (!link) return;
      e.preventDefault();
      document.getElementById("fe-lint-overlay").hidden = true;
      loadFlow(link.dataset.loadRel);
    });
    document.getElementById("fe-palette-search").addEventListener("input", (e) => {
      paletteSearchQuery = e.target.value;
      applyPaletteFilter();
    });
    document.addEventListener("step-types-changed", refreshStepTypes);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
