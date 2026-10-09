/**
 * Reference page: choose one way, find it automatically (pick a numbered outline) or mark it by
 * hand, then Next confirms that choice and goes on to the aim step. The app never confirms for the
 * user: only Next calls the confirm endpoint.
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
// The page layout sizes the photo to the window from its shape (app.css, `--photo-aspect`).
function setPhotoAspect(width, height) {
  if (width > 0 && height > 0) app.closest(".flow-page")?.style.setProperty("--photo-aspect", String(width / height));
}
setPhotoAspect(imageWidth, imageHeight);
const reviewUrl = app.dataset.reviewUrl;
const returnTo = app.dataset.returnTo || "/";
// Another group's saved work fixes this photo's scale: there is nothing to choose, Next goes on.
const locked = app.dataset.locked === "1";
// This group already has an aim or shots against the old scale: they are cleared before the new one.
const ownWork = app.dataset.ownWork === "1";

const el = (id) => document.getElementById(id);
const overlay = el("overlay");
const viewer = el("viewer");
const stage = el("stage");
const kindSelect = el("kind");
// `mode` is the chosen way to set the reference: "auto" (pick a found outline), "manual" (mark the
// edge by hand) or null before either is chosen; one Next button confirms whichever is chosen.
const state = {
  candidates: [],
  selected: null,
  hovered: null,
  mode: null,
  manual: false,
  searched: false,
  marks: [],
  busy: false,
};
const TILT_RATIO = 0.98;

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

function candidateNumberAt(candidate) {
  // Where the option's number is written on the photo: just above the outline's top.
  if (candidate.circle) {
    const c = candidate.circle;
    return [c.center_x_px, c.center_y_px - Math.max(c.axis_major_px, c.axis_minor_px) / 2];
  }
  const ys = candidate.grid_points_px.map(([, y]) => y);
  const top = candidate.grid_points_px[ys.indexOf(Math.min(...ys))];
  return top;
}

function drawCandidateNumber(candidate, emphasised) {
  const [x, y] = candidateNumberAt(candidate);
  const scale = screenToImage();
  const label = svg("text", {
    transform: `translate(${x} ${y - 6 * scale}) scale(${scale * (emphasised ? 1.6 : 1.3)})`,
    class: emphasised ? "shot-label candidate-number is-active" : "shot-label candidate-number",
    "text-anchor": "middle",
  });
  label.textContent = String(candidate.index + 1);
  overlay.appendChild(label);
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
  if (state.mode === "auto") {
    const focus = state.hovered !== null ? state.hovered : state.selected;
    state.candidates.forEach((candidate, index) => {
      if (index !== focus) drawCandidate(candidate, false);
    });
    if (state.candidates[focus]) drawCandidate(state.candidates[focus], true);
    if (state.candidates.length > 1) {
      state.candidates.forEach((candidate, index) => drawCandidateNumber(candidate, index === focus));
    }
  }
  drawMarks();
}

function circleSize(candidate) {
  return (candidate.circle.axis_major_px + candidate.circle.axis_minor_px) / 2;
}

function candidateLabel(candidate) {
  // Plain words: which outline it is, by size among the circles found.
  const number = `Option ${candidate.index + 1}`;
  if (!candidate.circle) return `${number}: the grid of squares`;
  const circles = state.candidates.filter((c) => c.circle).sort((a, b) => circleSize(b) - circleSize(a));
  if (circles.length < 2) return `${number}: the circle outlined on the photo`;
  const rank = circles.indexOf(candidate);
  const size =
    rank === 0 ? "the biggest circle" : rank === circles.length - 1 ? "the smallest circle" : "a middle-sized circle";
  return `${number}: ${size}`;
}

function candidateHint(candidate) {
  if (candidate.axis_ratio !== null && candidate.axis_ratio < TILT_RATIO) {
    return "The photo looks taken at an angle, so sizes from it may be slightly off.";
  }
  return "";
}

function renderCandidates() {
  const list = el("candidates");
  list.replaceChildren();
  // The rows go, so does any hover on them (their pointerleave never fires).
  state.hovered = null;
  el("candidates-panel").hidden = state.mode !== "auto";
  el("candidates-title").textContent = isGrid()
    ? "Which outline is your printed grid?"
    : "Which outline is your printed circle?";
  el("candidates-empty").hidden = state.candidates.length > 0;
  el("candidates-help").textContent =
    "Each option is outlined and numbered on the photo. Point at an option to highlight it, then pick the one that sits exactly on the printed edge.";
  if (state.candidates.length === 1) {
    // One outline found: it is the choice (Next still confirms it), so no radio and no number.
    const only = state.candidates[0];
    state.selected = only.index;
    el("candidates-title").textContent = isGrid() ? "Is this your printed grid?" : "Is this your printed circle?";
    el("candidates-help").textContent =
      "Check that the outline sits exactly on the printed edge, then press Next. If it does not, mark it by hand.";
    const row = document.createElement("div");
    row.className = "card";
    row.id = "single-candidate";
    const text = document.createElement("p");
    text.textContent = only.circle ? "Circle outlined on the photo." : "Grid outlined on the photo.";
    row.appendChild(text);
    const hint = candidateHint(only);
    if (hint) row.appendChild(notice("danger", hint));
    list.appendChild(row);
    updateNext();
    return;
  }
  // Several outlines: the biggest circle starts selected (a pick the user keeps across re-renders).
  if (state.candidates.length > 0 && !state.candidates.some((c) => c.index === state.selected)) {
    state.selected = defaultCandidate().index;
  }
  for (const candidate of state.candidates) {
    const row = document.createElement("div");
    row.className = "card";
    const label = document.createElement("label");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "candidate";
    radio.value = String(candidate.index);
    radio.checked = candidate.index === state.selected;
    radio.addEventListener("change", () => selectCandidate(candidate.index));
    const text = document.createElement("span");
    text.textContent = candidateLabel(candidate);
    label.append(radio, text);
    row.appendChild(label);
    const hint = candidateHint(candidate);
    if (hint) row.appendChild(notice("danger", hint));
    row.addEventListener("pointerenter", () => {
      state.hovered = candidate.index;
      redraw();
    });
    row.addEventListener("pointerleave", () => {
      state.hovered = null;
      redraw();
    });
    list.appendChild(row);
  }
  updateNext();
}

function defaultCandidate() {
  const circles = state.candidates.filter((c) => c.circle);
  if (circles.length === 0) return state.candidates[0];
  return circles.reduce((big, c) => (circleSize(c) > circleSize(big) ? c : big));
}

function selectCandidate(index) {
  state.selected = index;
  updateNext();
  redraw();
}

function ready() {
  if (locked) return true;
  if (state.mode === "auto") return state.selected !== null;
  if (state.mode === "manual") {
    return isGrid() ? state.marks.length === GRID_MARKS : state.marks.length >= CIRCLE_MIN_MARKS;
  }
  return false;
}

function updateNext() {
  el("reference-next").disabled = state.busy || !ready();
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
      : "All four corners are placed. Enter the squares across and down, then press Next.";
  } else {
    el("manual-instructions").textContent =
      `Click at least ${CIRCLE_MIN_MARKS} points on the edge of the printed circle, spread around it; more points give a better fit.`;
  }
  updateNext();
}

function setMode(mode) {
  // A two-way toggle: choosing one way opens its section and closes the other.
  state.mode = mode;
  state.manual = mode === "manual";
  el("find").setAttribute("aria-pressed", String(mode === "auto"));
  el("manual-toggle").setAttribute("aria-pressed", String(mode === "manual"));
  el("candidates-panel").hidden = mode !== "auto" || !state.searched;
  el("manual-panel").hidden = mode !== "manual";
  viewer.classList.toggle("is-marking", state.manual);
  el("viewer-hint").textContent = state.manual
    ? "Click the photo to place marks; drag a mark to adjust it. Scroll to zoom in for precision and drag empty space to pan."
    : "The outlines found are numbered on the photo. Choose one in the list. Scroll to zoom and drag to pan.";
  updateManualUi();
  redraw();
}

function chooseAuto() {
  setMode("auto");
  if (!state.searched) guarded(findReference);
}

function resetChoices() {
  state.candidates = [];
  state.selected = null;
  state.searched = false;
  state.marks = [];
  el("candidates-panel").hidden = true;
  el("candidates").replaceChildren();
  el("warnings").replaceChildren();
  redraw();
  updateManualUi();
  // A different kind means a different search; it runs again while that way is chosen.
  if (state.mode === "auto") guarded(findReference);
}

async function guarded(action) {
  if (state.busy) return;
  state.busy = true;
  clearFlash();
  for (const id of ["find", "manual-toggle", "reference-next"]) el(id).disabled = true;
  try {
    await action();
  } catch (error) {
    flash(error.detail || error.message || "Something went wrong");
  } finally {
    state.busy = false;
    el("find").disabled = false;
    el("manual-toggle").disabled = false;
    updateManualUi();
  }
}

async function findReference() {
  const result = await apiSend("POST", `/api/photos/${photoId}/reference/propose`, {
    kind: kindSelect.value,
  });
  state.candidates = result.candidates;
  state.selected = null;
  state.searched = true;
  renderCandidates();
  redraw();
  if (state.candidates.length === 0) {
    setMode("manual");
    el("warnings").replaceChildren(notice("info", "Nothing was found automatically. Mark the edge by hand."));
  }
}

function finish() {
  // Confirmed: straight on to the next step (a tilted photo was already flagged on its option).
  window.location.assign(reviewUrl || returnTo);
}

async function next() {
  if (!ready()) return;
  if (locked) {
    finish();
    return;
  }
  if (ownWork) await apiSend("POST", `/api/photos/${photoId}/reference/reset`);
  if (state.mode === "auto") await confirmCandidate();
  else await confirmManual();
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
  if (finished && finished.pan) finished.pan.end();
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
  if (gesture && gesture.pan) gesture.pan.end();
  gesture = null;
  viewer.classList.remove("is-dragging");
});

el("zoom-in-button").addEventListener("click", () => zoom.zoomBy(BUTTON_ZOOM_STEP));
el("zoom-out-button").addEventListener("click", () => zoom.zoomBy(1 / BUTTON_ZOOM_STEP));
el("zoom-fit-button").addEventListener("click", () => zoom.fit({ animate: true }));
document.addEventListener("keydown", (event) => {
  const target = event.target;
  if (target instanceof HTMLInputElement || target instanceof HTMLSelectElement || target instanceof HTMLTextAreaElement) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === "+" || event.key === "=") zoom.zoomBy(BUTTON_ZOOM_STEP);
  else if (event.key === "-") zoom.zoomBy(1 / BUTTON_ZOOM_STEP);
  else if (event.key === "0") zoom.fit({ animate: true });
});

el("find").addEventListener("click", chooseAuto);
el("reference-next").addEventListener("click", () => guarded(next));
el("manual-toggle").addEventListener("click", () => setMode("manual"));
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
