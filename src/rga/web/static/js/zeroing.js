/**
 * Zeroing stepper page. Everything is read and written through api.js; the page shows only the
 * advice the server returns. A shoot-more or invalid advice never renders a correction, and all
 * text reaches the page through textContent.
 */
import { apiGet, apiSend, apiUpload, ApiError } from "/static/js/api.js";
import { wireSuppressorPicker } from "/static/js/suppressor_picker.js";

const root = document.getElementById("zeroing-root");
const zeroId = Number(root.dataset.zeroId);
const flash = document.getElementById("flash");
const NO_NUMBER = new Set(["shoot_more", "invalid"]);
const STANDARD_DOTS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0];
const PURPOSE_LABELS = {
  zero_step1: "Step 1 (on paper)",
  zero_step2: "Step 2 initial group",
  zero_confirm: "Step 2 confirm group",
  zero_check: "Zero check",
  general: "General",
};
const STEPS = [
  { key: "step1", label: "Step 1: get on paper", only: "new_setup" },
  { key: "step2_initial", label: "Step 2: initial group" },
  { key: "step2_confirm", label: "Step 2: confirm" },
  { key: "step3_dot", label: "Step 3: dot" },
  { key: "complete", label: "Zero check" },
];
const STEP_ORDER = STEPS.map((s) => s.key);
const OPEN_STATES = new Set(["step1", "step2_initial", "step2_confirm", "step3_dot"]);

let zero = null;
let dialedTouched = false;

function el(id) {
  return document.getElementById(id);
}

function show(message, kind) {
  flash.textContent = message;
  flash.classList.toggle("ok", kind === "ok");
  flash.hidden = false;
}

function describe(error) {
  if (error instanceof ApiError) {
    return error.code === "busy" ? `${error.detail} (try again)` : error.detail;
  }
  return "Something went wrong";
}

async function run(action) {
  flash.hidden = true;
  try {
    return await action();
  } catch (error) {
    show(describe(error));
    return undefined;
  }
}

function node(tag, text, className) {
  const created = document.createElement(tag);
  if (text !== undefined) created.textContent = text;
  if (className) created.className = className;
  return created;
}

function replaceChildren(parent, children) {
  parent.replaceChildren(...children);
}

function renderStepper() {
  const visible = STEPS.filter((s) => !s.only || s.only === zero.mode);
  const current = zero.state === "superseded" ? -1 : STEP_ORDER.indexOf(zero.state);
  replaceChildren(
    el("stepper"),
    visible.map((step) => {
      const index = STEP_ORDER.indexOf(step.key);
      const item = node("li", step.label);
      if (index < current) item.classList.add("done");
      if (index === current) item.classList.add("current");
      if (zero.state === "complete" && step.key === "complete") item.classList.add("done");
      return item;
    }),
  );
  const summary = zero.state === "superseded" ? "This zero was superseded by a newer one." : "";
  el("zero-summary").textContent =
    `Zero ${zero.id}, ${zero.mode === "new_setup" ? "new setup" : "existing setup"}, ` +
    `${zero.state.replaceAll("_", " ")}. ${summary}`.trim();
}

function statTile(label, value, sub) {
  const tile = node("div", undefined, "stat");
  tile.append(node("div", label, "label"), node("div", value, "value"));
  if (sub) tile.append(node("div", sub, "sub"));
  return tile;
}

function renderAdvice() {
  const card = el("advice-card");
  const advice = zero.advice;
  card.hidden = advice === null;
  if (advice === null) return;
  el("advice-message").textContent = advice.message;
  const showCorrection = !NO_NUMBER.has(advice.kind) && advice.correction !== null;
  const correction = el("advice-correction");
  correction.hidden = !showCorrection;
  correction.textContent = showCorrection ? advice.correction.text : "";

  const tiles = [];
  const stats = advice.stats;
  if (stats) {
    tiles.push(statTile("Shots", String(stats.n)));
    tiles.push(
      statTile("Extreme spread", `${stats.es_in.toFixed(2)} in`, `${stats.es_moa.toFixed(2)} MOA`),
    );
    if (stats.includes_estimated) tiles.push(statTile("Estimated shots", "included"));
  }
  replaceChildren(el("advice-stats"), tiles);
  el("advice-stats").hidden = tiles.length === 0;
  const poi = el("advice-poi");
  const poiText = !NO_NUMBER.has(advice.kind) && stats && stats.poi_text ? stats.poi_text : "";
  poi.hidden = poiText === "";
  poi.textContent = poiText ? `Mean point of impact: ${poiText}` : "";
}

function groupActions(group) {
  const cell = node("td");
  const back = encodeURIComponent(`/zeroing/${zero.id}`);
  const review = node("a", "Review");
  review.href = `/groups/${group.group_id}/review?return_to=${back}`;
  cell.append(review);
  if (OPEN_STATES.has(zero.state) && group.purpose !== "zero_check") {
    const detach = node("button", "Detach", "secondary");
    detach.type = "button";
    detach.title = "Remove an abandoned group from this zero; it stays a general group";
    detach.addEventListener("click", () => detachGroup(group.group_id));
    cell.append(" ", detach);
  }
  return cell;
}

function renderGroups() {
  const rows = zero.groups.map((group) => {
    const row = document.createElement("tr");
    row.append(
      node("td", `#${group.group_id}`),
      node("td", PURPOSE_LABELS[group.purpose] || group.purpose),
      node("td", group.status),
      node("td", String(group.shots), "num"),
      groupActions(group),
    );
    return row;
  });
  replaceChildren(el("groups-body"), rows);
  el("groups-table").hidden = rows.length === 0;
  el("groups-empty").hidden = rows.length !== 0;
}

function nextGroupPurpose() {
  return { step1: "zero_step1", step2_initial: "zero_step2" }[zero.state] || "zero_confirm";
}

async function lotCode() {
  if (zero.lot_id === null) return "";
  const lots = await run(() => apiGet(`/api/loads/${zero.load_id}/lots`));
  const lot = (lots || []).find((l) => l.id === zero.lot_id);
  return lot ? lot.lot_code : "";
}

async function renderAddGroup() {
  const card = el("add-group-card");
  const checking = zero.state === "complete";
  card.hidden = !OPEN_STATES.has(zero.state) && !checking;
  if (card.hidden) return;
  const titles = {
    zero_step1: "Add a Step 1 group (1 to 3 shots)",
    zero_step2: "Add the Step 2 initial group (10 or more shots)",
    zero_confirm: "Add a confirm group (10 or more shots)",
  };
  const purpose = checking ? "zero_check" : nextGroupPurpose();
  el("add-group-title").textContent = checking ? "Create a zero-check group (1 or 2 shots)" : titles[purpose];
  el("add-group-hint").textContent = checking
    ? "Create the group, add its photo and review the shots, finalize it, then run the check below."
    : "The group is created and linked to this zero. With a photo it opens for review; otherwise add the photo from the rifle page.";
  el("ag-create").textContent = checking ? "Create zero-check group" : "Create group";
  el("ag-shots").max = checking ? "2" : "100";
  el("ag-shots").value = purpose === "zero_step1" ? "3" : checking ? "2" : "10";
  if (el("ag-date").value === "") el("ag-date").value = localToday();
  if (el("ag-lot").value === "") el("ag-lot").value = await lotCode();
}

let photo = null;
let suppressors = null;

function localToday() {
  return new Date().toLocaleDateString("sv-SE");
}

async function chosePhoto() {
  const file = el("ag-photo").files[0];
  photo = null;
  el("ag-date-source").textContent = "entered";
  if (!file) return;
  const uploaded = await run(() => apiUpload("/api/photos", file));
  if (!uploaded) return;
  photo = uploaded;
  if (!uploaded.taken_at) {
    el("ag-date-source").textContent = "entered (the photo has no date)";
    return;
  }
  const [day, time] = uploaded.taken_at.split("T");
  el("ag-date").value = day;
  el("ag-time").value = time.slice(0, 5);
  el("ag-date-source").textContent = "from photo";
}

function photoDateUnchanged() {
  if (!photo || !photo.taken_at) return false;
  const [day, time] = photo.taken_at.split("T");
  return el("ag-date").value === day && el("ag-time").value === time.slice(0, 5);
}

function shotTime() {
  if (photoDateUnchanged()) return photo.taken_at.split("T")[1];
  const time = el("ag-time").value.trim();
  return time === "" ? null : time;
}

async function createGroup(checking) {
  const code = el("ag-lot").value.trim();
  if (el("ag-date").value === "" || code === "") {
    show("Enter the date and the lot code.");
    return undefined;
  }
  if (suppressors.needsSaving()) {
    show("Save the new suppressor first, or choose one from the list.");
    return undefined;
  }
  const shots = Number(el("ag-shots").value);
  const notes = el("ag-notes").value;
  const photoId = photo ? photo.id : null;
  return run(async () => {
    if (checking) {
      const group = await apiSend("POST", `/api/zeros/${zero.id}/check-groups`, {
        lot_code: code,
        shots_expected: shots,
        notes,
        shot_date: el("ag-date").value,
        shot_time: shotTime(),
        position: el("ag-position").value,
        suppressor: suppressors.suppressed(),
        suppressor_id: suppressors.suppressorId(),
        photo_id: photoId,
      });
      return { group };
    }
    const group = await apiSend("POST", "/api/groups", {
      load_id: zero.load_id,
      lot_code: code,
      shots_expected: shots,
      distance_yd: 100,
      purpose: "general",
      notes,
      shot_date: el("ag-date").value,
      shot_time: shotTime(),
      utc_offset: photoDateUnchanged() ? photo.taken_at_offset : null,
      position: el("ag-position").value,
      suppressor: suppressors.suppressed(),
      suppressor_id: suppressors.suppressorId(),
      photo_id: photoId,
    });
    zero = await apiSend("POST", `/api/zeros/${zero.id}/groups`, { group_id: group.id });
    return { group };
  });
}

async function onCreateGroup() {
  const checking = zero.state === "complete";
  const created = await createGroup(checking);
  if (!created) return;
  const back = encodeURIComponent(`/zeroing/${zero.id}`);
  if (photo) {
    window.location.href = photo.reference_confirmed
      ? `/groups/${created.group.id}/review?return_to=${back}`
      : `/photos/${photo.id}/reference?group_id=${created.group.id}&return_to=${back}`;
    return;
  }
  const link = node("a", "Open the rifle page to import the photo");
  link.href = `/rifles/${zero.rifle_id}`;
  const next = el("ag-next");
  next.hidden = false;
  next.replaceChildren(
    checking
      ? `Group #${created.group.id} was created. Import and review its photo from the rifle page, finalize it, then come back and run the check. `
      : `Group #${created.group.id} is linked to this zero. `,
    link,
    ".",
  );
  await render();
}

async function detachGroup(groupId) {
  const updated = await run(() => apiSend("DELETE", `/api/zeros/${zero.id}/groups/${groupId}`));
  if (updated) {
    zero = updated;
    await render();
    show(`Group #${groupId} was detached and is a general group again.`, "ok");
  }
}

function renderDialed() {
  const card = el("dialed-card");
  card.hidden = !OPEN_STATES.has(zero.state) || zero.groups.length === 0;
  const advice = zero.advice;
  const suggested = advice && !NO_NUMBER.has(advice.kind) ? advice.correction : null;
  if (suggested && !dialedTouched) {
    el("dl-up").value = String(suggested.up_mil);
    el("dl-right").value = String(suggested.right_mil);
  }
}

async function saveDialed() {
  const body = { up_mil: Number(el("dl-up").value), right_mil: Number(el("dl-right").value) };
  const updated = await run(() => apiSend("POST", `/api/zeros/${zero.id}/dialed`, body));
  if (updated) {
    zero = updated;
    await render();
    show("Dialed correction recorded.", "ok");
  }
}

function renderDot() {
  const card = el("dot-card");
  const visible = zero.state === "step3_dot" || zero.state === "complete";
  card.hidden = !visible;
  if (!visible) return;
  const suggested = zero.suggested_dot_in;
  el("dot-suggestion").textContent =
    suggested === null
      ? "Judge a ready confirm group first."
      : `Suggested dot: ${suggested} in, from the ${zero.zero_shot_count}-shot overlay spread of ` +
        `${zero.zero_es_in.toFixed(2)} in minus the bullet diameter.`;
  el("dot-standard").textContent = `Standard sizes: ${STANDARD_DOTS.join(", ")} in. Any size is allowed.`;
  const size = el("dot-size");
  if (zero.dot_size_in !== null) size.value = String(zero.dot_size_in);
  else if (suggested !== null && size.value === "") size.value = String(suggested);
  const complete = zero.state === "complete";
  size.disabled = complete;
  el("dot-save").hidden = complete;
}

async function saveDot() {
  const updated = await run(() =>
    apiSend("POST", `/api/zeros/${zero.id}/dot`, { dot_size_in: Number(el("dot-size").value) }),
  );
  if (updated) {
    zero = updated;
    await render();
    show("The zero is complete.", "ok");
  }
}

async function renderCheck() {
  const card = el("check-card");
  card.hidden = zero.state !== "complete";
  if (zero.state !== "complete") return;
  const groups = await run(() =>
    apiGet(`/api/groups?rifle_id=${zero.rifle_id}&load_id=${zero.load_id}&status=final`),
  );
  const checked = new Set(zero.checks.map((c) => c.group_id));
  const candidates = (groups || []).filter(
    (g) => g.shots_expected <= 2 && !checked.has(g.id) && (g.zero_id === null || g.zero_id === zero.id),
  );
  replaceChildren(
    el("ck-group"),
    candidates.map((g) => {
      const option = node("option", `Group #${g.id}, ${g.shots_expected} shot(s)`);
      option.value = String(g.id);
      return option;
    }),
  );
  el("ck-hint").textContent =
    candidates.length === 0 ? "No finalized 1 or 2 shot group is waiting for a check." : "";
  el("ck-run").disabled = candidates.length === 0;
  replaceChildren(
    el("ck-history"),
    zero.checks.map((c) => node("li", `Group #${c.group_id}: ${c.result.replaceAll("_", " ")}. ${c.message}`)),
  );
}

function showCheck(check) {
  const box = el("ck-result");
  box.hidden = false;
  box.className = `notice ${check.result === "pass" ? "info" : "danger"}`;
  const lines = [node("p", check.message)];
  if (check.route.length > 0) {
    const list = document.createElement("ol");
    list.append(...check.route.map((step) => node("li", step)));
    lines.push(list);
  }
  box.replaceChildren(...lines);
}

async function runCheck() {
  const groupId = Number(el("ck-group").value);
  if (!groupId) return;
  const check = await run(() => apiSend("POST", `/api/zeros/${zero.id}/checks`, { group_id: groupId }));
  if (check) {
    showCheck(check);
    zero = await apiGet(`/api/zeros/${zero.id}`);
    await render();
  }
}

async function render() {
  renderStepper();
  renderAdvice();
  renderGroups();
  renderDialed();
  renderDot();
  await Promise.all([renderAddGroup(), renderCheck()]);
}

async function start() {
  const loaded = await run(() => apiGet(`/api/zeros/${zeroId}`));
  if (!loaded) return;
  zero = loaded;
  suppressors = wireSuppressorPicker(el("add-group-card").querySelector("[data-suppressor-picker]"), show);
  el("ag-photo").addEventListener("change", chosePhoto);
  el("ag-create").addEventListener("click", onCreateGroup);
  el("dl-save").addEventListener("click", saveDialed);
  for (const id of ["dl-up", "dl-right"]) {
    el(id).addEventListener("input", () => {
      dialedTouched = true;
    });
  }
  el("dot-save").addEventListener("click", saveDot);
  el("ck-run").addEventListener("click", runCheck);
  await render();
}

start();
