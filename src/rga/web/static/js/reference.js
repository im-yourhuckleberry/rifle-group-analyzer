/**
 * Reference page: propose, pick or mark the printed reference, confirm it, then go to review.
 * The app never confirms for the user: only the two "Use ..." buttons call the confirm endpoint.
 * All writes and reads go through api.js; every dynamic string is set with textContent.
 */
import { apiSend } from "/static/js/api.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const CIRCLE_MIN_MARKS = 3;
const CIRCLE_MAX_MARKS = 50;
const GRID_MARKS = 4;
const GRID_CORNER_NAMES = ["top-left", "top-right", "bottom-right", "bottom-left"];

const app = document.getElementById("reference-app");
const photoId = Number(app.dataset.photoId);
const imageWidth = Number(app.dataset.width);
const imageHeight = Number(app.dataset.height);
const reviewUrl = app.dataset.reviewUrl;
const returnTo = app.dataset.returnTo || "/";

const el = (id) => document.getElementById(id);
const overlay = el("overlay");
const kindSelect = el("kind");
const state = { candidates: [], selected: null, manual: false, marks: [], busy: false };

function flash(message) {
  const box = el("flash");
  box.textContent = message;
  box.hidden = false;
}

function clearFlash() {
  const box = el("flash");
  box.textContent = "";
  box.hidden = true;
}

function isGrid() {
  return kindSelect.value === "grid_1in";
}

function notice(kind, text) {
  const div = document.createElement("div");
  div.className = `notice ${kind}`;
  div.textContent = text;
  return div;
}

function svg(name, attrs) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  node.setAttribute("vector-effect", "non-scaling-stroke");
  return node;
}

function markRadius() {
  return Math.max(imageWidth, imageHeight) / 150;
}

function drawCandidate(candidate, selected) {
  const cls = selected ? "shot detected" : "shot estimated";
  if (candidate.circle) {
    const c = candidate.circle;
    const node = svg("ellipse", {
      cx: c.center_x_px,
      cy: c.center_y_px,
      rx: c.axis_major_px / 2,
      ry: c.axis_minor_px / 2,
      transform: `rotate(${c.angle_deg} ${c.center_x_px} ${c.center_y_px})`,
      class: cls,
    });
    overlay.appendChild(node);
  } else if (candidate.grid_points_px) {
    const points = candidate.grid_points_px.map(([x, y]) => `${x},${y}`).join(" ");
    overlay.appendChild(svg("polygon", { points, class: cls }));
    for (const [x, y] of candidate.grid_points_px) {
      overlay.appendChild(svg("circle", { cx: x, cy: y, r: markRadius(), class: cls }));
    }
  }
}

function drawMarks() {
  state.marks.forEach(([x, y], index) => {
    overlay.appendChild(svg("circle", { cx: x, cy: y, r: markRadius(), class: "shot manual selected" }));
    const label = svg("text", {
      x: x + markRadius() * 1.4,
      y: y - markRadius() * 1.4,
      class: "shot-label",
    });
    label.textContent = String(index + 1);
    overlay.appendChild(label);
  });
}

function redraw() {
  overlay.replaceChildren();
  state.candidates.forEach((candidate, index) => {
    if (index !== state.selected) drawCandidate(candidate, false);
  });
  if (state.selected !== null) drawCandidate(state.candidates[state.selected], true);
  drawMarks();
}

function candidateLabel(candidate) {
  const parts = [`Proposal ${candidate.index + 1}`];
  if (candidate.axis_ratio !== null) parts.push(`axis ratio ${candidate.axis_ratio.toFixed(2)}`);
  return parts.join(", ");
}

function renderWarnings(target, warnings) {
  target.replaceChildren();
  for (const warning of warnings) target.appendChild(notice("danger", warning));
}

function renderCandidates() {
  const list = el("candidates");
  list.replaceChildren();
  el("candidates-panel").hidden = false;
  el("candidates-empty").hidden = state.candidates.length > 0;
  for (const candidate of state.candidates) {
    const row = document.createElement("div");
    row.className = "card";
    const label = document.createElement("label");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "candidate";
    radio.value = String(candidate.index);
    radio.addEventListener("change", () => selectCandidate(candidate.index));
    const text = document.createElement("span");
    text.textContent = candidateLabel(candidate);
    label.append(radio, text);
    row.appendChild(label);
    for (const warning of candidate.warnings) row.appendChild(notice("danger", warning));
    list.appendChild(row);
  }
  el("confirm-candidate").disabled = true;
}

function selectCandidate(index) {
  state.selected = index;
  el("confirm-candidate").disabled = false;
  redraw();
}

function updateManualUi() {
  const grid = isGrid();
  el("grid-fields").hidden = !grid;
  const count = state.marks.length;
  el("mark-count").textContent = String(count);
  if (grid) {
    const next = count < GRID_MARKS ? GRID_CORNER_NAMES[count] : null;
    el("manual-instructions").textContent = next
      ? `Click the ${next} grid-line crossing of a block of squares (corners in order: top-left, top-right, bottom-right, bottom-left), then enter how many squares it spans.`
      : "All four corners are placed. Enter the squares across and down, then confirm.";
    el("confirm-manual").disabled = count !== GRID_MARKS;
  } else {
    el("manual-instructions").textContent =
      `Click at least ${CIRCLE_MIN_MARKS} points on the edge of the printed circle, spread around it; more points give a better fit.`;
    el("confirm-manual").disabled = count < CIRCLE_MIN_MARKS;
  }
}

function setManual(on) {
  state.manual = on;
  el("manual-panel").hidden = !on;
  el("manual-toggle").setAttribute("aria-pressed", String(on));
  el("viewer-hint").textContent = on
    ? "Click the photo to place marks."
    : "Candidates are drawn on the photo. Choose one in the list, or switch to manual marking.";
  updateManualUi();
}

function resetChoices() {
  state.candidates = [];
  state.selected = null;
  state.marks = [];
  el("candidates-panel").hidden = true;
  el("candidates").replaceChildren();
  el("warnings").replaceChildren();
  el("result").hidden = true;
  redraw();
  updateManualUi();
}

async function guarded(action) {
  if (state.busy) return;
  state.busy = true;
  clearFlash();
  for (const id of ["find", "confirm-candidate", "confirm-manual"]) el(id).disabled = true;
  try {
    await action();
  } catch (error) {
    flash(error.detail || error.message || "Something went wrong");
  } finally {
    state.busy = false;
    el("find").disabled = false;
    el("confirm-candidate").disabled = state.selected === null;
    updateManualUi();
  }
}

async function findReference() {
  const result = await apiSend("POST", `/api/photos/${photoId}/reference/propose`, {
    kind: kindSelect.value,
  });
  state.candidates = result.candidates;
  state.selected = null;
  renderCandidates();
  redraw();
  if (state.candidates.length === 0) setManual(true);
}

function finish(result) {
  el("candidates-panel").hidden = true;
  el("manual-panel").hidden = true;
  const warnings = result.warnings || [];
  const link = el("continue");
  link.href = reviewUrl || returnTo;
  link.textContent = reviewUrl ? "Continue to review" : "Continue";
  renderWarnings(el("result-warnings"), warnings);
  el("result").hidden = false;
  if (warnings.length === 0 && reviewUrl) {
    window.location.assign(reviewUrl);
  }
}

async function confirmCandidate() {
  if (state.selected === null) return;
  const result = await apiSend("POST", `/api/photos/${photoId}/reference/confirm`, {
    kind: kindSelect.value,
    candidate_index: state.selected,
  });
  finish(result);
}

function manualBody() {
  const points = state.marks.map(([x, y]) => ({ x_px: x, y_px: y }));
  if (isGrid()) {
    const squaresX = Number(el("squares-x").value);
    const squaresY = Number(el("squares-y").value);
    if (!Number.isInteger(squaresX) || !Number.isInteger(squaresY) || squaresX < 1 || squaresY < 1) {
      throw new Error("Enter whole numbers of squares across and down (1 or more).");
    }
    return {
      kind: kindSelect.value,
      manual: { grid: { corners_px: points, squares_x: squaresX, squares_y: squaresY } },
    };
  }
  return { kind: kindSelect.value, manual: { edge_points_px: points } };
}

async function confirmManual() {
  const result = await apiSend("POST", `/api/photos/${photoId}/reference/confirm`, manualBody());
  finish(result);
}

function clickToImagePoint(event) {
  const matrix = overlay.getScreenCTM();
  if (!matrix) return null;
  const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
  if (point.x < 0 || point.y < 0 || point.x > imageWidth || point.y > imageHeight) return null;
  return [Math.round(point.x * 10) / 10, Math.round(point.y * 10) / 10];
}

overlay.addEventListener("click", (event) => {
  if (!state.manual) return;
  const limit = isGrid() ? GRID_MARKS : CIRCLE_MAX_MARKS;
  if (state.marks.length >= limit) return;
  const point = clickToImagePoint(event);
  if (!point) return;
  state.marks.push(point);
  redraw();
  updateManualUi();
});

el("find").addEventListener("click", () => guarded(findReference));
el("confirm-candidate").addEventListener("click", () => guarded(confirmCandidate));
el("confirm-manual").addEventListener("click", () => guarded(confirmManual));
el("manual-toggle").addEventListener("click", () => setManual(!state.manual));
el("undo-mark").addEventListener("click", () => {
  state.marks.pop();
  redraw();
  updateManualUi();
});
el("clear-marks").addEventListener("click", () => {
  state.marks = [];
  redraw();
  updateManualUi();
});
kindSelect.addEventListener("change", resetChoices);

updateManualUi();
