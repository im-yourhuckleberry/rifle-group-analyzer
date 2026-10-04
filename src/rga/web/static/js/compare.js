/**
 * Page script for /compare?rifle_id=: reads the chosen loads and filters, fetches the comparison
 * through api.js and renders it with textContent only, so user text is never parsed as markup.
 */

import { apiGet } from "/static/js/api.js";

const TIER_LABELS = { tier10: "10-shot groups (10 to 19 shots)", tier30: "Groups of 30 or more shots" };
const STATUS_TEXT = {
  insufficient_data: "Insufficient data: not ranked",
  ok: "Enough data",
  high_confidence: "Higher confidence (20 or more rounds)",
};
const PAIR_TEXT = {
  insufficient_data: "insufficient data on one side, not ranked",
  no_real_difference: "no real difference (within normal group-to-group variation)",
};
const POSITION_TEXT = {
  bench: "Bench",
  prone_off_pack: "Prone off pack",
  kneeling_sitting: "Kneeling or sitting",
  standing_field: "Standing (field)",
};

function showError(error) {
  const flash = document.getElementById("flash");
  const message = error && error.detail ? error.detail : error && error.message ? error.message : "Something went wrong";
  if (!flash) return;
  flash.classList.remove("ok");
  flash.textContent = message;
  flash.hidden = false;
}

function clearError() {
  const flash = document.getElementById("flash");
  if (flash) {
    flash.textContent = "";
    flash.hidden = true;
  }
}

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function capitalized(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

function moa(value) {
  return value === null || value === undefined ? "none" : `${value.toFixed(2)} MOA`;
}

function stat(label, value, sub) {
  const box = el("div", null, "stat");
  box.append(el("div", label, "label"), el("div", value, "value"));
  if (sub) box.append(el("div", sub, "sub"));
  return box;
}

function tierBlock(key, tier) {
  const block = el("div", null, "stack");
  block.append(el("h3", TIER_LABELS[key]));
  if (tier.groups === 0) {
    block.append(el("p", "No groups in this tier.", "muted"));
    return block;
  }
  const grid = el("div", null, "stat-grid");
  grid.append(
    stat("Mean ES", moa(tier.mean_es_moa)),
    stat("Worst group", moa(tier.worst_es_moa)),
    stat("Rounds", String(tier.rounds)),
    stat("Groups", String(tier.groups)),
  );
  block.append(grid);
  return block;
}

function groupsTable(groups) {
  const table = el("table");
  const head = el("tr");
  ["Date", "Distance", "Position", "Suppressor", "Shots", "ES (MOA)", "Tier"].forEach((name, index) => {
    head.append(el("th", name, index >= 4 && index <= 5 ? "num" : undefined));
  });
  const thead = el("thead");
  thead.append(head);
  const tbody = el("tbody");
  groups.forEach((group) => {
    const row = el("tr");
    const shots = group.includes_estimated ? `${group.shots} (includes estimated shots)` : String(group.shots);
    const tier = group.tier === null ? "no tier" : group.tier === "tier10" ? "10-shot" : "30+";
    row.append(
      el("td", group.shot_time ? `${group.shot_date} ${group.shot_time.slice(0, 5)}` : group.shot_date),
      el("td", `${group.distance_yd} yd`),
      el("td", POSITION_TEXT[group.position] || group.position),
      el("td", group.suppressor ? "on" : "off"),
      el("td", shots, "num"),
      el("td", group.es_moa.toFixed(2), "num"),
      el("td", tier),
    );
    tbody.append(row);
  });
  table.append(thead, tbody);
  return table;
}

function aggregatesList(aggregates) {
  const list = el("ul");
  aggregates.forEach((aggregate) => {
    const item = el("li");
    item.append(el("strong", aggregate.name));
    item.append(
      ` ${aggregate.group_ids.length} groups, ${aggregate.shots} shots at ${aggregate.distance_yd} yd, ` +
        `${aggregate.es_moa.toFixed(2)} MOA: `,
    );
    item.append(
      aggregate.counted
        ? el("span", "counted in the 30-shot comparison", "badge")
        : el("span", `not counted: ${aggregate.note}`, "badge warn"),
    );
    list.append(item);
  });
  return list;
}

function loadCard(load) {
  const card = el("section", null, "card stack");
  const title = el("h2", load.label);
  if (load.is_zero_load) title.append(" ", el("span", "zero load", "badge"));
  const status = el("span", STATUS_TEXT[load.status] || load.status, load.status === "insufficient_data" ? "badge warn" : "badge");
  card.append(title, status);
  card.append(tierBlock("tier10", load.tier10), tierBlock("tier30", load.tier30));
  const verdict = el("p");
  verdict.append(el("strong", "Precision (10-shot average): "));
  verdict.append(load.precision_verdict || "needs at least one 10-shot group");
  card.append(verdict);
  const shift = el("p");
  shift.append(el("strong", "POI shift from the zero load: "));
  shift.append(load.poi_shift ? load.poi_shift.text : capitalized(load.poi_shift_note || "not available"));
  card.append(shift);
  if (load.groups.length) {
    card.append(el("h3", "Groups included"), groupsTable(load.groups));
  } else {
    card.append(el("p", "No finalized groups of 10 or more shots match these filters.", "muted"));
  }
  if (load.aggregates.length) {
    card.append(el("h3", "Saved combinations"), aggregatesList(load.aggregates));
  }
  return card;
}

function pairsBlock(data, labels) {
  const card = el("section", null, "card stack");
  card.append(el("h2", "Head to head"));
  const list = el("ul");
  data.pairs.forEach((pair) => {
    const a = labels.get(pair.a_load_id) || `Load ${pair.a_load_id}`;
    const b = labels.get(pair.b_load_id) || `Load ${pair.b_load_id}`;
    ["tier10", "tier30"].forEach((tier) => {
      const verdict = pair[tier];
      let text = PAIR_TEXT[verdict];
      if (verdict === "a_smaller") text = `${a} is smaller`;
      if (verdict === "b_smaller") text = `${b} is smaller`;
      list.append(el("li", `${a} vs ${b}, ${TIER_LABELS[tier].toLowerCase()}: ${text}`));
    });
  });
  card.append(list);
  return card;
}

function render(container, data) {
  container.replaceChildren();
  const labels = new Map(data.loads.map((load) => [load.load_id, load.label]));
  if (data.conditions_warning) container.append(el("p", data.conditions_warning, "notice"));
  const grid = el("div", null, "grid-2");
  data.loads.forEach((load) => grid.append(loadCard(load)));
  container.append(grid);
  if (data.pairs.length) container.append(pairsBlock(data, labels));
  container.append(el("p", data.zero_to_hunting_load_advice, "notice info"));
}

function selectedLoadIds(root) {
  return Array.from(root.querySelectorAll("input[data-load-id]:checked")).map((box) => box.value);
}

function comparisonPath(rifleId, ids, position, suppressor) {
  const params = [`load_ids=${ids.join(",")}`];
  if (position) params.push(`position=${encodeURIComponent(position)}`);
  if (suppressor) params.push(`suppressor=${encodeURIComponent(suppressor)}`);
  return `/api/rifles/${rifleId}/comparison?${params.join("&")}`;
}

function main() {
  const root = document.getElementById("compare-root");
  const button = document.getElementById("run-compare");
  const results = document.getElementById("comparison-results");
  if (!root || !button || !results) return;
  const rifleId = root.dataset.rifleId;
  const run = async () => {
    clearError();
    const ids = selectedLoadIds(root);
    if (ids.length === 0) {
      showError(new Error("Choose at least one load"));
      return;
    }
    button.disabled = true;
    try {
      const position = document.getElementById("filter-position").value;
      const suppressor = document.getElementById("filter-suppressor").value;
      const data = await apiGet(comparisonPath(rifleId, ids, position, suppressor));
      render(results, data);
    } catch (error) {
      showError(error);
    } finally {
      button.disabled = false;
    }
  };
  button.addEventListener("click", run);
  root.addEventListener("change", (event) => {
    if (event.target.matches("input[data-load-id], select")) results.replaceChildren();
  });
  run();
}

main();
