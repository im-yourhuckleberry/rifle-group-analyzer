/**
 * Page script for /groups/{id} (notes) and /analysis (group picker, aggregate overlay, notes
 * search). Every request goes through api.js; user text is written with textContent only.
 */

import { apiGet, apiSend } from "/static/js/api.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const PX_PER_IN = 100;
const GROUP_COLORS = 6;

function flashEl() {
  return document.getElementById("flash");
}

function showError(error) {
  const flash = flashEl();
  if (!flash) return;
  flash.classList.remove("ok");
  flash.textContent =
    error && error.detail ? error.detail : error && error.message ? error.message : "Something went wrong";
  flash.hidden = false;
}

function clearError() {
  const flash = flashEl();
  if (flash) {
    flash.textContent = "";
    flash.hidden = true;
  }
}

function element(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined && text !== null) el.textContent = String(text);
  if (className) el.className = className;
  return el;
}

function svgElement(tag, attrs, className) {
  const el = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs).forEach(([name, value]) => el.setAttribute(name, String(value)));
  if (className) el.setAttribute("class", className);
  return el;
}

function onSubmit(form, handler) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const buttons = form.querySelectorAll("button");
    buttons.forEach((b) => {
      b.disabled = true;
    });
    clearError();
    try {
      await handler();
    } catch (error) {
      showError(error);
    } finally {
      buttons.forEach((b) => {
        b.disabled = false;
      });
    }
  });
}

// ---------------------------------------------------------------- group page

function wireGroupPage(root) {
  const groupId = Number(root.dataset.groupId);
  const form = root.querySelector('[data-form="group-notes"]');
  onSubmit(form, async () => {
    await apiSend("PATCH", `/api/groups/${groupId}`, { notes: form.elements.namedItem("notes").value });
    window.location.reload();
  });
}

// ---------------------------------------------------------------- analysis page

function directionWords(dxMil, dyMil) {
  const parts = [];
  const dy = Math.round(dyMil * 10) / 10;
  const dx = Math.round(dxMil * 10) / 10;
  if (dy !== 0) parts.push(`${Math.abs(dy).toFixed(1)} mil ${dy > 0 ? "high" : "low"}`);
  if (dx !== 0) parts.push(`${Math.abs(dx).toFixed(1)} mil ${dx > 0 ? "right" : "left"}`);
  return parts.length ? parts.join(", ") : "on the aim point";
}

async function loadContext() {
  const rifles = await apiGet("/api/rifles");
  const loads = new Map();
  for (const rifle of rifles) {
    const rows = await apiGet(`/api/rifles/${rifle.id}/loads`);
    rows.forEach((load) => loads.set(load.id, load.label));
  }
  return {
    rifles: new Map(rifles.map((r) => [r.id, r.name])),
    loads,
  };
}

function statTile(label, value, sub) {
  const tile = element("div", null, "stat");
  tile.appendChild(element("div", label, "label"));
  tile.appendChild(element("div", value, "value"));
  if (sub) tile.appendChild(element("div", sub, "sub"));
  return tile;
}

function drawOverlay(svg, result) {
  svg.replaceChildren();
  const reach = Math.max(
    1.5,
    ...result.shots.map((s) => Math.max(Math.abs(s.dx_in), Math.abs(s.dy_in)) + 0.4),
    Math.abs(result.center_dx_in) + 0.4,
    Math.abs(result.center_dy_in) + 0.4,
  );
  const half = reach * PX_PER_IN;
  svg.setAttribute("viewBox", `${-half} ${-half} ${2 * half} ${2 * half}`);
  const aim = svgElement("g", {}, "aim-mark");
  aim.appendChild(svgElement("line", { x1: -24, y1: 0, x2: 24, y2: 0 }));
  aim.appendChild(svgElement("line", { x1: 0, y1: -24, x2: 0, y2: 24 }));
  svg.appendChild(aim);
  const groupIds = [...new Set(result.shots.map((s) => s.group_id))];
  result.shots.forEach((shot) => {
    const kind = shot.origin === "estimated" ? "estimated" : "detected";
    svg.appendChild(
      svgElement(
        "circle",
        { cx: shot.dx_in * PX_PER_IN, cy: -shot.dy_in * PX_PER_IN, r: 7 },
        `shot ${kind} ${groupColorClass(groupIds.indexOf(shot.group_id))}`,
      ),
    );
  });
  const cx = result.center_dx_in * PX_PER_IN;
  const cy = -result.center_dy_in * PX_PER_IN;
  const arm = 11;
  svg.appendChild(svgElement("line", { x1: cx - arm, y1: cy - arm, x2: cx + arm, y2: cy + arm }, "poi-mark"));
  svg.appendChild(svgElement("line", { x1: cx - arm, y1: cy + arm, x2: cx + arm, y2: cy - arm }, "poi-mark"));
}

function groupColorClass(slot) {
  return `group-color-${slot % GROUP_COLORS}`;
}

function legendItem(keyClass, keyShapes, text) {
  const item = document.createElement("li");
  const key = element("span", null, `key ${keyClass}`);
  const icon = svgElement("svg", { viewBox: "-12 -12 24 24", "aria-hidden": "true" });
  keyShapes.forEach(([tag, attrs]) => icon.appendChild(svgElement(tag, attrs)));
  key.appendChild(icon);
  item.appendChild(key);
  item.appendChild(element("span", text));
  return item;
}

function drawLegend(list, groupIds, labelFor) {
  list.replaceChildren(
    ...groupIds.map((id, slot) =>
      legendItem(`key-group ${groupColorClass(slot)}`, [["circle", { cx: 0, cy: 0, r: 8 }]], labelFor(id)),
    ),
    legendItem(
      "key-aim",
      [
        ["line", { x1: -10, y1: 0, x2: 10, y2: 0 }],
        ["line", { x1: 0, y1: -10, x2: 0, y2: 10 }],
      ],
      "Aim point",
    ),
    legendItem(
      "key-poi",
      [
        ["line", { x1: -8, y1: -8, x2: 8, y2: 8 }],
        ["line", { x1: -8, y1: 8, x2: 8, y2: -8 }],
      ],
      "Combined center",
    ),
  );
}

function showAggregate(result, ids, labelFor) {
  const box = document.getElementById("aggregate-result");
  const stats = document.getElementById("aggregate-stats");
  stats.replaceChildren(
    statTile("Groups", result.group_count),
    statTile("Shots", result.n),
    statTile(
      "Extreme spread",
      `${result.es_in.toFixed(2)} in`,
      `${result.es_moa.toFixed(2)} MOA`,
    ),
    statTile(
      "Combined center",
      directionWords(result.center_dx_mil, result.center_dy_mil),
      `${result.center_dx_in.toFixed(2)} in right, ${result.center_dy_in.toFixed(2)} in up`,
    ),
  );
  const badges = document.getElementById("aggregate-badges");
  badges.replaceChildren();
  if (result.includes_estimated) badges.appendChild(element("span", "includes estimated shots", "badge warn"));
  const warnings = document.getElementById("aggregate-warnings");
  warnings.replaceChildren(...result.warnings.map((text) => element("p", text, "notice")));
  document.getElementById("aggregate-note").textContent =
    `Extreme spread is the smallest target every shot hit (center-to-center), over ${ids.length} group(s) laid on their aim points.`;
  drawOverlay(document.getElementById("aggregate-plot"), result);
  drawLegend(
    document.getElementById("aggregate-legend"),
    [...new Set(result.shots.map((shot) => shot.group_id))],
    labelFor,
  );
  box.hidden = false;
}

function savedItem(saved, onDelete) {
  const item = document.createElement("li");
  item.appendChild(element("strong", saved.name));
  item.appendChild(
    document.createTextNode(
      ` ${saved.group_count} groups, ${saved.n} shots, ${saved.es_moa.toFixed(2)} MOA at ${saved.distance_yd} yd `,
    ),
  );
  if (!saved.valid) item.appendChild(element("span", `no longer valid: ${saved.invalid_reason}`, "badge warn"));
  else if (saved.tier === "tier30") item.appendChild(element("span", "30+ shots: eligible for the 30-shot comparison (Compare shows whether it's counted)", "badge"));
  const remove = element("button", "Delete", "secondary");
  remove.type = "button";
  remove.addEventListener("click", () => onDelete(saved, remove));
  item.appendChild(document.createTextNode(" "));
  item.appendChild(remove);
  return item;
}

async function refreshSaved() {
  const list = document.getElementById("saved-list");
  const empty = document.getElementById("saved-empty");
  const saved = await apiGet("/api/aggregates");
  list.replaceChildren(
    ...saved.map((entry) =>
      savedItem(entry, async (target, button) => {
        clearError();
        button.disabled = true;
        try {
          await apiSend("DELETE", `/api/aggregates/${target.id}`);
          await refreshSaved();
        } catch (error) {
          button.disabled = false;
          showError(error);
        }
      }),
    ),
  );
  empty.hidden = saved.length !== 0;
}

function wireSave(getIds) {
  const form = document.querySelector('[data-form="save-aggregate"]');
  onSubmit(form, async () => {
    const name = form.elements.namedItem("name").value.trim();
    if (name === "") throw new Error("Give this combination a name");
    await apiSend("POST", "/api/aggregates", { name, group_ids: getIds() });
    form.elements.namedItem("name").value = "";
    await refreshSaved();
  });
}

function wirePicker(root) {
  const body = document.getElementById("picker-body");
  const empty = document.getElementById("picker-empty");
  const button = document.getElementById("aggregate-button");
  const rifleFilter = document.getElementById("filter-rifle");
  const distanceFilter = document.getElementById("filter-distance");
  let context = null;
  let groups = [];

  const selectedIds = () =>
    [...body.querySelectorAll("input[type=checkbox]:checked")].map((box) => Number(box.value));
  const refreshButton = () => {
    button.disabled = selectedIds().length === 0;
  };

  function render() {
    body.replaceChildren();
    const distance = distanceFilter.value;
    let shown = 0;
    groups.forEach((group) => {
      if (distance && String(group.distance_yd) !== distance) return;
      shown += 1;
      const row = document.createElement("tr");
      const pick = document.createElement("td");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = String(group.id);
      box.disabled = group.masked;
      box.addEventListener("change", refreshButton);
      pick.appendChild(box);
      row.appendChild(pick);
      row.appendChild(element("td", group.shot_date));
      row.appendChild(element("td", context.rifles.get(group.rifle_id) || ""));
      row.appendChild(element("td", context.loads.get(group.load_id) || ""));
      row.appendChild(element("td", group.lot_code));
      row.appendChild(element("td", `${group.distance_yd} yd`, "num"));
      row.appendChild(element("td", group.shots_expected, "num"));
      const purpose = document.createElement("td");
      const link = element("a", group.purpose.replace(/_/g, " "));
      link.href = `/groups/${group.id}`;
      purpose.appendChild(link);
      if (group.masked) purpose.appendChild(element("span", " (cannot be combined)", "muted"));
      row.appendChild(purpose);
      body.appendChild(row);
    });
    empty.hidden = shown !== 0;
    refreshButton();
  }

  async function loadGroups() {
    const query = rifleFilter.value ? `&rifle_id=${rifleFilter.value}` : "";
    groups = await apiGet(`/api/groups?status=final${query}`);
    const distances = [
      ...new Set(groups.map((g) => g.distance_yd)),
    ].sort((a, b) => a - b);
    const previous = distanceFilter.value;
    distanceFilter.replaceChildren(new Option("All distances", ""));
    distances.forEach((d) => distanceFilter.appendChild(new Option(`${d} yd`, String(d))));
    distanceFilter.value = distances.map(String).includes(previous) ? previous : "";
    render();
  }

  const labelFor = (id) => {
    const group = groups.find((g) => g.id === id);
    if (!group) return `Group #${id}`;
    return `Group #${id}: ${group.shot_date}, lot ${group.lot_code}, ${context.loads.get(group.load_id) || ""}`;
  };
  let aggregatedIds = [];
  wireSave(() => aggregatedIds);
  refreshSaved().catch(showError);

  button.addEventListener("click", async () => {
    clearError();
    button.disabled = true;
    const ids = selectedIds();
    try {
      showAggregate(await apiSend("POST", "/api/aggregate", { group_ids: ids }), ids, labelFor);
      aggregatedIds = ids;
    } catch (error) {
      showError(error);
    } finally {
      refreshButton();
    }
  });
  rifleFilter.addEventListener("change", () => loadGroups().catch(showError));
  distanceFilter.addEventListener("change", render);

  loadContext()
    .then(async (ctx) => {
      context = ctx;
      ctx.rifles.forEach((name, id) => rifleFilter.appendChild(new Option(name, String(id))));
      await loadGroups();
    })
    .catch(showError);
  return root;
}

function wireSearch() {
  const form = document.querySelector('[data-form="search"]');
  const list = document.getElementById("search-results");
  const empty = document.getElementById("search-empty");
  onSubmit(form, async () => {
    const q = form.elements.namedItem("q").value.trim();
    list.replaceChildren();
    empty.hidden = true;
    if (q === "") throw new Error("Enter some text to search for");
    const hits = await apiGet(`/api/search?q=${encodeURIComponent(q)}`);
    empty.hidden = hits.length !== 0;
    hits.forEach((hit) => {
      const item = document.createElement("li");
      item.appendChild(element("span", hit.kind, "badge"));
      item.appendChild(document.createTextNode(" "));
      const link = element("a", `#${hit.id}`);
      link.href = `/groups/${hit.id}`;
      item.appendChild(link);
      item.appendChild(document.createTextNode(` ${hit.snippet}`));
      list.appendChild(item);
    });
  });
}

function init() {
  const root = document.querySelector("[data-page]");
  if (!root) return;
  if (root.dataset.page === "group") wireGroupPage(root);
  if (root.dataset.page === "analysis") {
    wirePicker(root);
    wireSearch();
  }
}

init();
