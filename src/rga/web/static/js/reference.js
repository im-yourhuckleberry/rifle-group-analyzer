/**
 * Reference page: propose, pick or mark the printed reference, confirm it, then go to review.
 * The app never confirms for the user: only the two "Use ..." buttons call the confirm endpoint.
 * All writes and reads go through api.js; every dynamic string is set with textContent.
 * The photo and overlay zoom together (scroll, the toolbar or +/-/0) as one CSS-transformed stage;
 * pointer positions map to image pixels through getScreenCTM, so marks are exact at any zoom.
 * In manual mode a click places a mark and a placed mark can be dragged to adjust it; dragging
 * anywhere else pans. Marks and labels keep a constant on-screen size.
 */
import { apiSend } from "/static/js/api.js";
import { BUTTON_ZOOM_STEP, createZoom } from "/static/js/svg_zoom.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const CIRCLE_MIN_MARKS = 3;
const CIRCLE_MAX_MARKS = 50;
const GRID_MARKS = 4;
const GRID_CORNER_NAMES = ["top-left", "top-right", "bottom-right", "bottom-left"];
const MARK_SCREEN_PX = 5;
const MARK_REACH_SCREEN_PX = 10;
const DRAG_THRESHOLD_PX = 4;

const app = document.getElementById("reference-app");
const photoId = Number(app.dataset.photoId);
const imageWidth = Number(app.dataset.width);
const imageHeight = Number(app.dataset.height);
const reviewUrl = app.dataset.reviewUrl;
const returnTo = app.dataset.returnTo || "/";

const el = (id) => document.getElementById(id);
const overlay = el("overlay");
const viewer = el("viewer");
const stage = el("stage");
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

const zoom = createZoom(overlay, {
  bounds: { x: 0, y: 0, w: imageWidth, h: imageHeight },
  frame: viewer,
  apply(view) {
    // Percent translations are of the stage's own (1x) size, so the view survives a resize.
    const scale = imageWidth / view.w;
    const tx = (-view.x / imageWidth) * scale * 100;
    const ty = (-view.y / imageHeight) * scale * 100;
    stage.style.transform = `translate(${tx}%, ${ty}%) scale(${scale})`;
  },
  onView(level) {
    el("zoom-level").textContent = `${level.toFixed(1)}\u00d7`;
    redraw();
  },
});

function screenToImage() {
  return 1 / zoom.pixelsPerUnit();
}

function markRadius() {
  return MARK_SCREEN_PX * screenToImage();
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
      transform: `translate(${x + markRadius() * 1.4} ${y - markRadius() * 1.4}) scale(${screenToImage()})`,
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
  viewer.classList.toggle("is-marking", on);
  el("viewer-hint").textContent = on
    ? "Click the photo to place marks; drag a mark to adjust it. Scroll to zoom in for precision and drag empty space to pan."
    : "Candidates are drawn on the photo. Choose one in the list, or switch to manual marking. Scroll to zoom and drag to pan.";
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

function imagePoint(event) {
  const matrix = overlay.getScreenCTM();
  if (!matrix) return null;
  const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
  return { x: point.x, y: point.y };
}

function rounded(x, y) {
  const clampTo = (value, high) => Math.min(Math.max(value, 0), high);
  return [Math.round(clampTo(x, imageWidth) * 10) / 10, Math.round(clampTo(y, imageHeight) * 10) / 10];
}

function markAt(point) {
  const reach = MARK_REACH_SCREEN_PX * screenToImage();
  let best = null;
  let bestDistance = reach;
  state.marks.forEach(([x, y], index) => {
    const distance = Math.hypot(x - point.x, y - point.y);
    if (distance <= bestDistance) {
      best = index;
      bestDistance = distance;
    }
  });
  return best;
}

// One pointer gesture: drag a mark (keeping the grab offset), pan, or a click that places a mark.
let gesture = null;

overlay.addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return;
  const point = imagePoint(event);
  if (!point) return;
  overlay.setPointerCapture(event.pointerId);
  const index = state.manual && !state.busy ? markAt(point) : null;
  const start = { x: event.clientX, y: event.clientY };
  if (index !== null) {
    const [mx, my] = state.marks[index];
    gesture = { kind: "mark", index, start, offset: { x: mx - point.x, y: my - point.y }, moved: false };
  } else {
    gesture = { kind: "empty", start, pan: zoom.startPan(event.clientX, event.clientY), moved: false };
  }
});

overlay.addEventListener("pointermove", (event) => {
  if (!gesture) return;
  if (!gesture.moved) {
    if (Math.hypot(event.clientX - gesture.start.x, event.clientY - gesture.start.y) < DRAG_THRESHOLD_PX) return;
    gesture.moved = true;
    viewer.classList.add("is-dragging");
  }
  if (gesture.kind === "mark") {
    const point = imagePoint(event);
    if (!point) return;
    state.marks[gesture.index] = rounded(point.x + gesture.offset.x, point.y + gesture.offset.y);
    redraw();
  } else {
    gesture.pan.move(event.clientX, event.clientY);
  }
});

overlay.addEventListener("pointerup", (event) => {
  const finished = gesture;
  gesture = null;
  viewer.classList.remove("is-dragging");
  if (!finished || finished.moved || finished.kind !== "empty" || !state.manual) return;
  const limit = isGrid() ? GRID_MARKS : CIRCLE_MAX_MARKS;
  if (state.marks.length >= limit) return;
  const point = imagePoint(event);
  if (!point || point.x < 0 || point.y < 0 || point.x > imageWidth || point.y > imageHeight) return;
  state.marks.push(rounded(point.x, point.y));
  redraw();
  updateManualUi();
});

overlay.addEventListener("pointercancel", () => {
  gesture = null;
  viewer.classList.remove("is-dragging");
});

el("zoom-in-button").addEventListener("click", () => zoom.zoomBy(BUTTON_ZOOM_STEP));
el("zoom-out-button").addEventListener("click", () => zoom.zoomBy(1 / BUTTON_ZOOM_STEP));
el("zoom-fit-button").addEventListener("click", () => zoom.fit());
document.addEventListener("keydown", (event) => {
  const target = event.target;
  if (target instanceof HTMLInputElement || target instanceof HTMLSelectElement || target instanceof HTMLTextAreaElement) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === "+" || event.key === "=") zoom.zoomBy(BUTTON_ZOOM_STEP);
  else if (event.key === "-") zoom.zoomBy(1 / BUTTON_ZOOM_STEP);
  else if (event.key === "0") zoom.fit();
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

zoom.fit();
updateManualUi();
