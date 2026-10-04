/**
 * Review editor page: shows the proposed or stored shots over the working image, lets the user
 * add, drag and remove them, mark the aim point, set the expected count and a region, then saves
 * and finalizes. All requests go through api.js; user-visible text is set with textContent only.
 * The page shows size (extreme spread) and counts, never a point of impact.
 */
import { ApiError, apiSend } from "/static/js/api.js";
import { BUTTON_ZOOM_STEP, createEditor } from "/static/js/svg_editor.js";

const MOA_IN_PER_100YD = 1.0471975512;
const MAX_EXPECTED = 200;
const NO_PAIR_COST = 1e6;
const RING_CLICK_REACH = 1.3;
const AIM_RING_IN = 0.22;
const AIM_KIND_LABELS = {
  paster_center: "paster center",
  dot_center: "dot center",
  diamond_center: "diamond center",
  cross_center: "cross center",
  grid_intersection: "grid crossing",
};
const EDIT_HINT =
  "Scroll to zoom and drag empty space to pan. Click an empty spot to add a shot, drag a shot to move it, select one and use Not a bullet hole to remove it.";

const root = document.getElementById("review-root");
const server = JSON.parse(root.dataset.state);
const groupId = Number(root.dataset.groupId);
const isFinal = server.status === "final";
const hasPhoto = server.photo_id !== null;
const hasFrame = server.frame !== null && server.image_url !== null;
const referenceChange = server.reference_change;

const els = {
  flash: document.getElementById("flash"),
  notices: document.getElementById("review-notices"),
  context: document.getElementById("review-context"),
  svg: document.getElementById("editor-svg"),
  hint: document.getElementById("editor-hint"),
  changeBox: document.getElementById("reference-change"),
  changeButton: document.getElementById("reference-change-button"),
  changeReason: document.getElementById("reference-change-reason"),
  changeConfirm: document.getElementById("reference-change-confirm"),
  changeText: document.getElementById("reference-change-text"),
  changeContinue: document.getElementById("reference-change-continue"),
  changeCancel: document.getElementById("reference-change-cancel"),
  counter: document.getElementById("shot-counter"),
  spread: document.getElementById("spread-line"),
  estimated: document.getElementById("estimated-badge"),
  expected: document.getElementById("expected-input"),
  detect: document.getElementById("detect-button"),
  region: document.getElementById("region-button"),
  fill: document.getElementById("fill-button"),
  aim: document.getElementById("aim-button"),
  remove: document.getElementById("delete-button"),
  aimStatus: document.getElementById("aim-status"),
  suggestion: document.getElementById("aim-suggestion"),
  suggestionText: document.getElementById("aim-suggestion-text"),
  useAim: document.getElementById("aim-use-button"),
  nextAim: document.getElementById("aim-next-button"),
  manualAim: document.getElementById("aim-manual-button"),
  save: document.getElementById("save-button"),
  finalize: document.getElementById("finalize-button"),
  reason: document.getElementById("finalize-reason"),
  zoomIn: document.getElementById("zoom-in-button"),
  zoomOut: document.getElementById("zoom-out-button"),
  zoomFit: document.getElementById("zoom-fit-button"),
  zoomRegion: document.getElementById("zoom-region-button"),
  zoomLevel: document.getElementById("zoom-level"),
};

let storedExpected = server.shots_expected;
const model = {
  shots: server.shots.map(fromServerShot),
  rejections: server.rejections.map((r) => ({ x_in: r.x_in, y_in: r.y_in, reason: r.reason })),
  aim: server.aim,
  expected: server.shots_expected,
  region: server.roi_px ? [server.roi_px.x0, server.roi_px.y0, server.roi_px.x1, server.roi_px.y1] : null,
  selected: null,
  mode: "edit",
  busy: false,
  suggestions: [],
  suggestionIndex: -1,
};

function fromServerShot(shot) {
  return {
    x_in: shot.x_in,
    y_in: shot.y_in,
    origin: shot.origin,
    moved_by_user: shot.moved_by_user,
    confidence: shot.confidence,
    saved: true,
  };
}

function fromProposal(shot) {
  return {
    x_in: shot.x_in,
    y_in: shot.y_in,
    origin: shot.origin,
    moved_by_user: false,
    confidence: shot.confidence,
    saved: false,
  };
}

function flash(message, ok = false) {
  els.flash.textContent = message;
  els.flash.classList.toggle("ok", ok);
  els.flash.hidden = false;
}

function clearFlash() {
  els.flash.hidden = true;
  els.flash.textContent = "";
}

function showNotices(messages, kind = "") {
  els.notices.replaceChildren();
  for (const message of messages) {
    const notice = document.createElement("div");
    notice.className = `notice ${kind}`.trim();
    notice.textContent = message;
    els.notices.append(notice);
  }
}

function failure(error) {
  if (error instanceof ApiError) {
    flash(error.code === "busy" ? "The database is busy; try again in a moment." : error.detail);
  } else {
    flash("Something went wrong in the page; reload and try again.");
    throw error;
  }
}

function extremeSpreadIn() {
  let best = 0;
  for (let i = 0; i < model.shots.length; i += 1) {
    for (let j = i + 1; j < model.shots.length; j += 1) {
      best = Math.max(
        best,
        Math.hypot(model.shots[i].x_in - model.shots[j].x_in, model.shots[i].y_in - model.shots[j].y_in),
      );
    }
  }
  return best;
}

function finalizeBlocker() {
  if (isFinal) return "This group is already final.";
  if (hasPhoto && !server.reference_confirmed) return "Confirm the reference object on the photo first.";
  if (!model.aim) return "Mark the aim point first.";
  const count = model.shots.length;
  if (count > model.expected) {
    return `${count} of ${model.expected} shots: remove ${count - model.expected} with Not a bullet hole, or raise the expected count.`;
  }
  if (count < model.expected) {
    return `${count} of ${model.expected} shots: add ${model.expected - count} missing shot(s), or lower the expected count.`;
  }
  return "";
}

function refresh() {
  const count = model.shots.length;
  els.counter.textContent = `${count} of ${model.expected} shots`;
  els.counter.classList.toggle("mismatch", count !== model.expected);
  if (count >= 2) {
    const es = extremeSpreadIn();
    const moa = es / ((MOA_IN_PER_100YD * server.distance_yd) / 100);
    els.spread.textContent = `Extreme spread ${es.toFixed(2)} in (${moa.toFixed(2)} MOA), smallest target every shot hit (center-to-center)`;
  } else {
    els.spread.textContent = "";
  }
  els.estimated.hidden = !model.shots.some((s) => s.origin === "estimated");
  els.expected.value = String(model.expected);
  const suggesting = !isFinal && !model.aim && model.suggestions.length > 0;
  els.suggestion.hidden = !suggesting;
  if (suggesting) {
    const current = model.suggestions[model.suggestionIndex];
    els.suggestionText.textContent = current
      ? `Suggested aim ${model.suggestionIndex + 1} of ${model.suggestions.length}: ${current.label}. It is not set until you use it.`
      : `${model.suggestions.length} possible aim mark(s) found, none selected. Click a ring or use Next to pick one, then use it, or place the aim yourself.`;
  }
  els.useAim.disabled = !suggesting || model.suggestionIndex < 0 || model.busy;
  els.nextAim.disabled =
    !suggesting || (model.suggestionIndex >= 0 && model.suggestions.length < 2) || model.busy;
  els.manualAim.disabled = !suggesting || model.busy;
  els.remove.disabled = isFinal || model.selected === null || model.busy;
  els.aimStatus.textContent = model.aim ? "Aim point is set." : "Aim point is not set.";
  const blocker = finalizeBlocker();
  els.finalize.disabled = blocker !== "" || model.busy;
  els.reason.textContent = blocker;
  els.aim.textContent = model.mode === "aim" ? "Click the aim mark" : "Mark aim";
  els.region.textContent = model.mode === "region" ? "Drag a region" : model.region ? "Change region" : "Set region";
  for (const button of [els.detect, els.fill, els.save, els.aim, els.region]) {
    button.disabled = isFinal || !hasFrame || model.busy;
  }
  for (const button of [els.zoomIn, els.zoomOut, els.zoomFit]) button.disabled = !hasFrame;
  els.zoomRegion.disabled = !hasFrame || model.region === null;
  els.expected.disabled = isFinal || model.busy;
  if (referenceChange) {
    els.changeButton.disabled = referenceChange.mode === "blocked" || model.busy;
    els.changeContinue.disabled = model.busy;
    els.changeCancel.disabled = model.busy;
  }
  if (editor) {
    editor.render({
      shots: model.shots,
      selected: model.selected,
      aim: model.aim,
      region: model.region,
      aimCandidates: suggesting ? model.suggestions : [],
      activeCandidate: model.suggestionIndex,
    });
  }
}

function setMode(mode) {
  model.mode = model.mode === mode ? "edit" : mode;
  if (editor) editor.setMode(model.mode);
  els.hint.textContent =
    model.mode === "aim"
      ? "Click on the aim mark; it snaps to the centre of the mark."
      : model.mode === "region"
        ? "Drag a rectangle around this group's holes, then use Detect holes."
        : EDIT_HINT;
  refresh();
}

function removeSelected() {
  if (isFinal || model.selected === null) return;
  const [shot] = model.shots.splice(model.selected, 1);
  if (!isPlaceholder(shot) && !(shot.origin === "manual" && !shot.saved)) {
    model.rejections.push({ x_in: shot.x_in, y_in: shot.y_in, reason: "not_a_bullet_hole" });
  }
  model.selected = null;
  refresh();
}

function isPlaceholder(shot) {
  return shot.origin === "estimated" && !shot.moved_by_user;
}

function isUnsavedProposal(shot) {
  return !shot.saved && !shot.moved_by_user && shot.origin !== "manual";
}

function mergeProposal(proposal, replaceUnsavedProposals = false) {
  // Unmoved estimated shots are placeholders the proposal replaces. A Detect inside a region also
  // replaces the earlier unsaved, unmoved proposals (the whole-photo guess); saved, moved and
  // manual shots always stay.
  const replaceable = (s) => isPlaceholder(s) || (replaceUnsavedProposals && isUnsavedProposal(s));
  const kept = model.shots.filter((s) => !replaceable(s));
  const fresh = unmatchedProposals(kept, proposal.shots.map(fromProposal), proposal.bullet_diameter_in);
  model.shots = [...kept, ...fresh];
  model.selected = null;
  showNotices(proposal.messages, "info");
}

function unmatchedProposals(kept, candidates, reach) {
  // Each kept shot stands for at most one proposed shot within reach. The pairing is the optimal
  // one-to-one assignment (most pairs first, then least total distance), so a nudged shot never
  // hides its neighbour's re-proposal, however the siblings were moved.
  const near = kept.map((k) => candidates.map((c) => Math.hypot(k.x_in - c.x_in, k.y_in - c.y_in)));
  const rows = kept.map((_, i) => i).filter((i) => near[i].some((d) => d < reach));
  const cols = candidates.map((_, j) => j).filter((j) => near.some((row) => row[j] < reach));
  const used = new Set();
  const cost = rows.map((i) => cols.map((j) => (near[i][j] < reach ? near[i][j] : NO_PAIR_COST)));
  for (const [r, c] of assignMinimum(cost)) {
    if (cost[r][c] < NO_PAIR_COST) used.add(cols[c]);
  }
  return candidates.filter((_, j) => !used.has(j));
}

function assignMinimum(cost) {
  // Hungarian algorithm for a rectangular cost matrix; returns [row, column] pairs.
  if (cost.length === 0 || cost[0].length === 0) return [];
  const transposed = cost.length > cost[0].length;
  const c = transposed ? cost[0].map((_, j) => cost.map((row) => row[j])) : cost;
  const n = c.length;
  const m = c[0].length;
  const u = new Array(n + 1).fill(0);
  const v = new Array(m + 1).fill(0);
  const way = new Array(m + 1).fill(0);
  const match = new Array(m + 1).fill(0);
  for (let i = 1; i <= n; i += 1) {
    match[0] = i;
    let j0 = 0;
    const minv = new Array(m + 1).fill(Infinity);
    const seen = new Array(m + 1).fill(false);
    do {
      seen[j0] = true;
      const i0 = match[j0];
      let delta = Infinity;
      let j1 = 0;
      for (let j = 1; j <= m; j += 1) {
        if (seen[j]) continue;
        const current = c[i0 - 1][j - 1] - u[i0] - v[j];
        if (current < minv[j]) {
          minv[j] = current;
          way[j] = j0;
        }
        if (minv[j] < delta) {
          delta = minv[j];
          j1 = j;
        }
      }
      for (let j = 0; j <= m; j += 1) {
        if (seen[j]) {
          u[match[j]] += delta;
          v[j] -= delta;
        } else {
          minv[j] -= delta;
        }
      }
      j0 = j1;
    } while (match[j0] !== 0);
    do {
      const j1 = way[j0];
      match[j0] = match[j1];
      j0 = j1;
    } while (j0 !== 0);
  }
  const pairs = [];
  for (let j = 1; j <= m; j += 1) {
    if (match[j] !== 0) pairs.push(transposed ? [j - 1, match[j] - 1] : [match[j] - 1, j - 1]);
  }
  return pairs;
}

function suggestAims(candidates) {
  model.suggestions = model.aim
    ? []
    : candidates.map((c) => ({
        x_in: c.x_in,
        y_in: c.y_in,
        label: AIM_KIND_LABELS[c.kind] || "mark",
      }));
  model.suggestionIndex = -1;
}

function useSuggestedAim() {
  const chosen = model.suggestions[model.suggestionIndex];
  if (!chosen || model.aim) return;
  model.aim = { x_in: chosen.x_in, y_in: chosen.y_in };
  model.suggestions = [];
  els.aimStatus.textContent = "Aim set from the suggested mark.";
  refresh();
}

function nextSuggestedAim() {
  if (model.suggestions.length === 0 || (model.suggestionIndex >= 0 && model.suggestions.length < 2)) return;
  model.suggestionIndex = (model.suggestionIndex + 1) % model.suggestions.length;
  refresh();
}

function selectRingAt(event) {
  // A click on a suggestion ring selects it; anywhere else the editor handles the click as usual.
  if (event.button !== 0 || model.mode !== "edit" || isFinal || model.busy) return;
  if (model.aim || model.suggestions.length === 0) return;
  const point = els.svg.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  const at = point.matrixTransform(els.svg.getScreenCTM().inverse());
  const reach = AIM_RING_IN * server.frame.ppi * RING_CLICK_REACH;
  let best = -1;
  let bestDistance = reach;
  model.suggestions.forEach((candidate, index) => {
    const distance = Math.hypot(candidate.x_in * server.frame.ppi - at.x, candidate.y_in * server.frame.ppi - at.y);
    if (distance <= bestDistance) {
      best = index;
      bestDistance = distance;
    }
  });
  if (best < 0) return;
  event.stopImmediatePropagation();
  event.preventDefault();
  model.suggestionIndex = best;
  refresh();
}

function placeAimYourself() {
  model.suggestions = [];
  if (model.mode !== "aim") setMode("aim");
  else refresh();
}

async function guarded(action) {
  if (model.busy) return;
  model.busy = true;
  refresh();
  try {
    await action();
  } catch (error) {
    failure(error);
  } finally {
    model.busy = false;
    refresh();
  }
}

function readExpected() {
  const value = Number(els.expected.value);
  if (!Number.isInteger(value) || value < 1 || value > MAX_EXPECTED) {
    throw new ApiError(`The expected shot count must be a whole number from 1 to ${MAX_EXPECTED}.`, "invalid_shots_expected", 0);
  }
  model.expected = value;
}

function adopt(detail) {
  model.shots = detail.shots.map(fromServerShot);
  model.rejections = detail.rejections.map((r) => ({ x_in: r.x_in, y_in: r.y_in, reason: r.reason }));
  model.aim = detail.aim;
  model.expected = detail.shots_expected;
  storedExpected = detail.shots_expected;
  model.selected = null;
}

async function save() {
  readExpected();
  const detail = await apiSend("PUT", `/api/groups/${groupId}/review`, {
    aim: model.aim ? { x_in: model.aim.x_in, y_in: model.aim.y_in } : null,
    shots: model.shots.map((s) => ({
      x_in: s.x_in,
      y_in: s.y_in,
      origin: s.origin,
      moved_by_user: s.moved_by_user,
      confidence: s.confidence,
    })),
    rejections: model.rejections,
    shots_expected: model.expected,
  });
  adopt(detail);
  return detail;
}

const SHARED_PHOTO_NOTICE =
  "Draw a region first: other groups share this photo, so drag a rectangle around this group's holes, then use Detect holes.";

let sharedNoticeShown = false;

function mustDrawRegion() {
  return !isFinal && server.other_groups_on_photo > 0 && model.region === null;
}

async function detect() {
  if (mustDrawRegion()) {
    showNotices([SHARED_PHOTO_NOTICE], "info");
    sharedNoticeShown = true;
    if (model.mode !== "region") setMode("region");
    return;
  }
  if (sharedNoticeShown) {
    showNotices([]);
    sharedNoticeShown = false;
  }
  readExpected();
  if (model.expected !== storedExpected) await save();
  const body = model.region
    ? { roi_px: { x0: model.region[0], y0: model.region[1], x1: model.region[2], y1: model.region[3] } }
    : {};
  const proposal = await apiSend("POST", `/api/groups/${groupId}/detect`, body);
  mergeProposal(proposal, model.region !== null);
  suggestAims(proposal.aim_candidates || []);
}

async function fillShortfall() {
  await save();
  mergeProposal(await apiSend("POST", `/api/groups/${groupId}/fill-shortfall`));
}

async function clickAim(xPx, yPx) {
  await guarded(async () => {
    const snap = await apiSend("POST", `/api/photos/${server.photo_id}/snap-aim`, { x_px: xPx, y_px: yPx });
    model.aim = { x_in: snap.x_in, y_in: snap.y_in };
    model.suggestions = [];
    els.aimStatus.textContent = snap.snapped ? "Aim snapped to the mark." : "Aim set where you clicked.";
    model.mode = "edit";
    editor.setMode("edit");
  });
}

async function finalizeGroup() {
  await guarded(async () => {
    await save();
    await apiSend("POST", `/api/groups/${groupId}/finalize`);
    window.location.assign(server.next_url);
  });
}

function referenceUrl() {
  const back = server.return_to ? `&return_to=${encodeURIComponent(server.return_to)}` : "";
  return `/photos/${server.photo_id}/reference?group_id=${groupId}${back}`;
}

function changeReference() {
  if (referenceChange.mode === "direct") {
    window.location.assign(referenceUrl());
    return;
  }
  const count = referenceChange.draft_groups;
  els.changeText.textContent = `This clears the saved shots and aim of ${count} draft group(s) on this photo. Continue?`;
  els.changeConfirm.hidden = false;
  els.changeContinue.focus();
}

function cancelReferenceChange() {
  els.changeConfirm.hidden = true;
  els.changeButton.focus();
}

async function resetReference() {
  await guarded(async () => {
    await apiSend("POST", `/api/photos/${server.photo_id}/reference/reset`);
    window.location.assign(referenceUrl());
  });
}

function setUpReferenceChange() {
  if (!referenceChange) return;
  els.changeBox.hidden = false;
  els.changeReason.textContent = referenceChange.reason || "";
  els.changeButton.addEventListener("click", changeReference);
  els.changeContinue.addEventListener("click", resetReference);
  els.changeCancel.addEventListener("click", cancelReferenceChange);
}

function explain() {
  const notices = [];
  if (isFinal) {
    notices.push("This group is final and can no longer be edited.");
  } else if (!hasPhoto) {
    notices.push("This group has no photo, so shots cannot be placed. Attach a photo when creating the group.");
  } else if (!server.reference_confirmed) {
    notices.push("The reference object on this photo is not confirmed yet.");
  }
  if (server.new_lot) notices.push("New lot: check the point of impact against the zero load.");
  if (notices.length) showNotices(notices, "info");
  if (!isFinal && hasPhoto && !server.reference_confirmed) {
    const link = document.createElement("a");
    link.href = `/photos/${server.photo_id}/reference?group_id=${groupId}`;
    link.textContent = "Confirm the reference";
    els.notices.append(link);
  }
  if (isFinal) {
    const link = document.createElement("a");
    link.href = `/groups/${groupId}`;
    link.textContent = "View this group";
    els.notices.append(link);
  }
}

let editor = null;
if (hasFrame) {
  editor = createEditor(els.svg, {
    imageUrl: server.image_url,
    ppi: server.frame.ppi,
    width: server.frame.width_px,
    height: server.frame.height_px,
    radiusIn: server.bullet_diameter_in / 2,
    onAdd(xIn, yIn) {
      if (isFinal) return;
      model.shots.push({ x_in: xIn, y_in: yIn, origin: "manual", moved_by_user: false, confidence: null, saved: false });
      model.selected = model.shots.length - 1;
      refresh();
    },
    onSelect(index) {
      model.selected = index;
      refresh();
    },
    onView(zoom) {
      els.zoomLevel.textContent = `${zoom.toFixed(1)}\u00d7`;
    },
    onMove(index, xIn, yIn) {
      if (isFinal) return;
      Object.assign(model.shots[index], { x_in: xIn, y_in: yIn, moved_by_user: true });
      refresh();
    },
    onAimClick: clickAim,
    onRegion(x0, y0, x1, y1, done) {
      model.region = [x0, y0, x1, y1];
      if (done) setMode("region");
      else refresh();
    },
  });
}

els.context.textContent = `${server.load_label} (lot ${server.lot_code}) at ${server.distance_yd} yd, ${server.purpose.replaceAll("_", " ")}`;
els.expected.addEventListener("change", () => {
  const value = Number(els.expected.value);
  if (Number.isInteger(value) && value >= 1 && value <= MAX_EXPECTED) model.expected = value;
  refresh();
});
els.detect.addEventListener("click", () => guarded(detect));
els.fill.addEventListener("click", () => guarded(fillShortfall));
els.save.addEventListener("click", () =>
  guarded(async () => {
    await save();
    flash("Saved.", true);
  }),
);
els.aim.addEventListener("click", () => setMode("aim"));
if (hasFrame) els.svg.addEventListener("pointerdown", selectRingAt, true);
els.useAim.addEventListener("click", useSuggestedAim);
els.nextAim.addEventListener("click", nextSuggestedAim);
els.manualAim.addEventListener("click", placeAimYourself);
els.region.addEventListener("click", () => setMode("region"));
els.remove.addEventListener("click", removeSelected);
els.zoomIn.addEventListener("click", () => editor && editor.zoomBy(BUTTON_ZOOM_STEP));
els.zoomOut.addEventListener("click", () => editor && editor.zoomBy(1 / BUTTON_ZOOM_STEP));
els.zoomFit.addEventListener("click", () => editor && editor.fit());
els.zoomRegion.addEventListener("click", () => editor && model.region && editor.zoomToRegion(model.region));
els.finalize.addEventListener("click", finalizeGroup);
document.addEventListener("keydown", (event) => {
  const typing = event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement;
  if (typing || !editor) return;
  if (event.key === "Delete" || event.key === "Backspace") {
    event.preventDefault();
    removeSelected();
  } else if (event.key === "+" || event.key === "=") {
    editor.zoomBy(BUTTON_ZOOM_STEP);
  } else if (event.key === "-") {
    editor.zoomBy(1 / BUTTON_ZOOM_STEP);
  } else if (event.key === "0") {
    editor.fit();
  }
});

setUpReferenceChange();
explain();
if (editor && model.region) editor.zoomToRegion(model.region);
els.hint.textContent = hasFrame && !isFinal
  ? EDIT_HINT
  : "";
refresh();
if (hasFrame && !isFinal && model.shots.length === 0 && model.rejections.length === 0) {
  guarded(detect);
}
