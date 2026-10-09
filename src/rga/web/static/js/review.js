/**
 * Review editor page, in two steps shown one at a time. Step "aim": only the aim point is drawn.
 * With a printed-circle reference it starts on the circle's exact centre ("Use the circle's
 * center"); "Mark it by hand" (or any click on the photo) places it exactly where clicked, with no
 * snapping (the printed mark is often shot out), and a drag fine-tunes it. Confirming saves it.
 * Step "shots": the proposed or stored shots are drawn and the user adds, drags and removes them,
 * sets the expected count and a region, then saves and finalizes. Each step has a Go back button
 * (shots -> aim -> the reference page). A region holds every detected shot: detected shots outside
 * it are dropped, only the user's own (manual or moved) shots may lie outside.
 * All requests go through api.js; user-visible text is set with textContent only.
 * The page shows shot counts, never a size or a point of impact.
 */
import { ApiError, apiSend } from "/static/js/api.js";
import { BUTTON_ZOOM_STEP, createEditor } from "/static/js/svg_editor.js";

const MAX_EXPECTED = 200;
const NO_PAIR_COST = 1e6;
const UNDO_LIMIT = 50;
const AIM_HINT = "Scroll to zoom and drag to pan. Click where you aimed to place the aim point there, then drag it to fine-tune it.";
const AIM_HELP = {
  center: "The aim point is on the exact center of your printed circle. To aim somewhere else, choose Mark it by hand or click the photo.",
  hand: "Click where you aimed: the aim point goes exactly there (it does not snap to the printed mark, which holes often cover). Drag it to fine-tune it.",
};
const EDIT_HINT =
  "Scroll to zoom and drag empty space to pan. Click an empty spot to add a shot (or to deselect), drag a shot to move it, select one and use Not a bullet hole to remove it. Undo (\u2318Z) takes back the last change.";

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
  expected: document.getElementById("expected-input"),
  detect: document.getElementById("detect-button"),
  stepAim: document.getElementById("step-aim"),
  stepShots: document.getElementById("step-shots"),
  stepperAim: document.getElementById("stepper-aim"),
  stepperShots: document.getElementById("stepper-shots"),
  confirmAim: document.getElementById("aim-confirm-button"),
  shotsBack: document.getElementById("shots-back-button"),
  remove: document.getElementById("delete-button"),
  undo: document.getElementById("undo-button"),
  aimStatus: document.getElementById("aim-status"),
  aimMode: document.getElementById("aim-mode"),
  aimCenterToggle: document.getElementById("aim-center-toggle"),
  aimHandToggle: document.getElementById("aim-hand-toggle"),
  aimHelp: document.getElementById("aim-help"),
  finalize: document.getElementById("finalize-button"),
  finalizeBox: document.getElementById("finalize-confirm"),
  finalizeConfirm: document.getElementById("finalize-confirm-button"),
  finalizeCancel: document.getElementById("finalize-cancel-button"),
  reason: document.getElementById("finalize-reason"),
  zoomIn: document.getElementById("zoom-in-button"),
  zoomOut: document.getElementById("zoom-out-button"),
  zoomFit: document.getElementById("zoom-fit-button"),
  zoomLevel: document.getElementById("zoom-level"),
  wrap: document.getElementById("editor-wrap"),
};
// What a busy button says while its request runs, so a slow detect never looks like a dead page:
// activity -> [button, idle label, working label].
const BUSY_LABELS = {
  detect: [els.detect, "Detect holes", "Detecting holes\u2026"],
  save: [els.finalize, "Done", "Saving\u2026"],
  finalize: [els.finalizeConfirm, "Lock and finish", "Finishing\u2026"],
};

let storedExpected = server.shots_expected;
const model = {
  shots: server.shots.map(fromServerShot),
  rejections: server.rejections.map((r) => ({ x_in: r.x_in, y_in: r.y_in, reason: r.reason })),
  aim: server.aim,
  expected: server.shots_expected,
  region: server.roi_px ? [server.roi_px.x0, server.roi_px.y0, server.roi_px.x1, server.roi_px.y1] : null,
  selected: null,
  // Without a confirmed reference no aim can be placed, so the page keeps its notices on "shots".
  step: isFinal || server.aim || !hasFrame ? "shots" : "aim",
  mode: "edit",
  busy: false,
  activity: null,
};
// Snapshots of the shots and rejections taken before each change, newest last.
const history = [];
// The confirmed circle reference's centre, offered as the aim (null for a grid reference).
const circleCenter = server.reference_center;
// How the aim is set: "center" (a circle reference's centre, the default unless a saved aim sits
// elsewhere) or "hand". The last hand-placed aim comes back when switching to "hand" again.
const aimAtCenter = (aim) =>
  circleCenter !== null && aim !== null && Math.hypot(aim.x_in - circleCenter.x_in, aim.y_in - circleCenter.y_in) < 1e-6;
const aimMode = { mode: circleCenter !== null && (!model.aim || aimAtCenter(model.aim)) ? "center" : "hand", handAim: null };
if (aimMode.mode === "center" && !isFinal && hasFrame) model.aim = { ...circleCenter };
if (aimMode.mode === "hand") aimMode.handAim = model.aim;
let aimNote = "";
let shotsOpened = false;

function remember() {
  history.push({
    shots: model.shots.map((s) => ({ ...s })),
    rejections: model.rejections.map((r) => ({ ...r })),
  });
  if (history.length > UNDO_LIMIT) history.shift();
}

function undo() {
  if (isFinal || model.busy || model.step !== "shots" || history.length === 0) return;
  const previous = history.pop();
  model.shots = previous.shots;
  model.rejections = previous.rejections;
  model.selected = null;
  refresh();
}

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
  const onAim = model.step === "aim";
  els.stepAim.hidden = !onAim;
  els.stepShots.hidden = onAim;
  els.stepperAim.className = onAim ? "current" : "done";
  els.stepperShots.className = onAim ? "" : "current";
  const count = model.shots.length;
  els.counter.textContent = `${count} of ${model.expected} shots`;
  els.counter.classList.toggle("mismatch", count !== model.expected);
  els.expected.value = String(model.expected);
  const centerMode = aimMode.mode === "center";
  els.aimMode.hidden = isFinal || circleCenter === null;
  els.aimCenterToggle.setAttribute("aria-pressed", String(centerMode));
  els.aimHandToggle.setAttribute("aria-pressed", String(!centerMode));
  els.aimCenterToggle.disabled = model.busy;
  els.aimHandToggle.disabled = model.busy;
  els.aimHelp.textContent = AIM_HELP[aimMode.mode];
  els.remove.disabled = isFinal || model.selected === null || model.busy;
  els.undo.disabled = isFinal || history.length === 0 || model.busy;
  els.aimStatus.textContent = !model.aim
    ? "Click the photo where you aimed."
    : centerMode
      ? "Aim point: the circle's exact center."
      : aimNote || "Aim point is set.";
  els.confirmAim.disabled = isFinal || !model.aim || model.busy;
  els.shotsBack.hidden = isFinal;
  els.shotsBack.disabled = model.busy;
  const blocker = finalizeBlocker();
  // Done is always available: it saves, and offers to lock the group once nothing blocks it.
  els.finalize.disabled = isFinal || !hasFrame || model.busy;
  if (blocker !== "") els.finalizeBox.hidden = true;
  els.finalizeConfirm.disabled = model.busy;
  els.finalizeCancel.disabled = model.busy;
  els.reason.textContent = blocker;
  for (const [key, [button, idle, working]] of Object.entries(BUSY_LABELS)) {
    button.textContent = model.activity === key ? working : idle;
  }
  els.wrap.setAttribute("aria-busy", String(model.activity === "detect"));
  for (const button of [els.detect]) {
    button.disabled = isFinal || !hasFrame || model.busy;
  }
  for (const button of [els.zoomIn, els.zoomOut, els.zoomFit]) button.disabled = !hasFrame;
  els.expected.disabled = isFinal || model.busy;
  if (referenceChange) {
    els.changeButton.disabled = referenceChange.mode === "blocked" || model.busy;
    els.changeContinue.disabled = model.busy;
    els.changeCancel.disabled = model.busy;
  }
  if (editor) {
    editor.render({
      shots: onAim ? [] : model.shots,
      selected: onAim ? null : model.selected,
      aim: model.aim,
      region: onAim ? null : model.region,
      aimCandidates: [],
      activeCandidate: -1,
    });
  }
}

function hintText() {
  if (!hasFrame || isFinal) return "";
  if (model.step === "aim") return AIM_HINT;
  return model.mode === "region" ? "Drag a rectangle around this group's holes, then use Detect holes." : EDIT_HINT;
}

function setMode(mode) {
  // Shots step only: toggles the region tool. The aim step keeps the editor in aim mode.
  model.mode = model.mode === mode ? "edit" : mode;
  if (editor) editor.setMode(model.mode);
  els.hint.textContent = hintText();
  refresh();
}

function showStep(step) {
  model.step = step;
  model.selected = null;
  model.mode = step === "aim" ? "aim" : "edit";
  if (editor) editor.setMode(model.mode);
  els.hint.textContent = hintText();
  els.finalizeBox.hidden = true;
  refresh();
}

function removeSelected() {
  if (isFinal || model.selected === null) return;
  remember();
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

function isUserPlaced(shot) {
  return shot.origin === "manual" || shot.moved_by_user;
}

function outsideRegion(shot) {
  if (!model.region) return false;
  const [x0, y0, x1, y1] = model.region;
  const x = shot.x_in * server.frame.ppi;
  const y = shot.y_in * server.frame.ppi;
  return x < x0 || x > x1 || y < y0 || y > y1;
}

function inRegionOrUserPlaced(shot) {
  return isUserPlaced(shot) || !outsideRegion(shot);
}

function mergeProposal(proposal, replaceUnsavedProposals = false, undoable = true) {
  // Unmoved estimated shots are placeholders the proposal replaces. A Detect inside a region also
  // replaces the earlier unsaved, unmoved proposals (the whole-photo guess); saved, moved and
  // manual shots always stay.
  // With a region, detected shots outside it go too, saved or not; the user's own shots stay.
  const replaceable = (s) => isPlaceholder(s) || (replaceUnsavedProposals && isUnsavedProposal(s));
  if (undoable) remember();
  const kept = model.shots.filter((s) => !replaceable(s) && inRegionOrUserPlaced(s));
  const proposed = proposal.shots.map(fromProposal).filter(inRegionOrUserPlaced);
  const fresh = unmatchedProposals(kept, proposed, proposal.bullet_diameter_in);
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

function setAimMode(mode) {
  if (isFinal || model.busy || model.step !== "aim" || circleCenter === null || aimMode.mode === mode) return;
  if (mode === "center") {
    aimMode.handAim = model.aim;
    model.aim = { ...circleCenter };
  } else {
    model.aim = aimMode.handAim;
    aimNote = "";
  }
  aimMode.mode = mode;
  refresh();
}

function placeAim(aim, note) {
  // By hand, exactly where it was put: no snapping to the printed mark, which holes often hide.
  aimMode.mode = "hand";
  aimMode.handAim = aim;
  model.aim = aim;
  aimNote = note;
  refresh();
}

async function guarded(action, activity = null) {
  if (model.busy) return;
  model.busy = true;
  model.activity = activity;
  refresh();
  try {
    await action();
  } catch (error) {
    failure(error);
  } finally {
    model.busy = false;
    model.activity = null;
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

function regionBody() {
  return model.region
    ? { roi_px: { x0: model.region[0], y0: model.region[1], x1: model.region[2], y1: model.region[3] } }
    : {};
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

async function detect(automatic = false) {
  // Always the whole photo (or the stored region): a photo other groups share is detected too, and
  // a hole that belongs to another group comes off with "Not a bullet hole".
  readExpected();
  if (model.expected !== storedExpected) await save();
  const proposal = await apiSend("POST", `/api/groups/${groupId}/detect`, regionBody());
  mergeProposal(proposal, model.region !== null, !automatic);
}

async function openShots() {
  // The first visit to the shots step brings in the detection (Undo does not take it back).
  if (shotsOpened || isFinal || !hasFrame) return;
  shotsOpened = true;
  if (model.shots.length > 0 || model.rejections.length > 0) return;
  await guarded(() => detect(true), "detect");
}

async function confirmAim() {
  if (!model.aim || model.busy) return;
  let saved = false;
  await guarded(async () => {
    await save();
    saved = true;
  }, "save");
  if (!saved) return;
  showStep("shots");
  await openShots();
}

function backToAim() {
  if (model.busy || isFinal) return;
  showStep("aim");
}

function clickAim(xPx, yPx) {
  if (isFinal || model.busy || model.step !== "aim") return;
  placeAim({ x_in: xPx / server.frame.ppi, y_in: yPx / server.frame.ppi }, "Aim point is set where you clicked.");
}

async function done() {
  // Done saves the work; when the group is ready it then asks to lock it (finalize), else it says
  // what is still missing.
  if (els.finalize.disabled) return;
  let saved = false;
  await guarded(async () => {
    await save();
    saved = true;
  }, "save");
  if (!saved) return;
  const blocker = finalizeBlocker();
  if (blocker !== "") {
    flash(`Saved. To finish: ${blocker}`, true);
    return;
  }
  clearFlash();
  els.finalizeBox.hidden = false;
  els.finalizeConfirm.focus();
}


function cancelFinalize() {
  els.finalizeBox.hidden = true;
  els.finalize.focus();
}

async function finalizeGroup() {
  await guarded(async () => {
    await save();
    await apiSend("POST", `/api/groups/${groupId}/finalize`);
    window.location.assign(server.next_url);
  }, "finalize");
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
  els.changeButton.hidden = false;
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
      if (model.shots.length >= model.expected) {
        // Never more shots than were fired: a click past the expected count adds nothing.
        flash(
          `Already ${model.shots.length} of ${model.expected} shots. Remove one with Not a bullet hole, or raise Shots fired.`,
        );
        return;
      }
      remember();
      model.shots.push({ x_in: xIn, y_in: yIn, origin: "manual", moved_by_user: false, confidence: null, saved: false });
      refresh();
    },
    onSelect(index) {
      model.selected = index;
      refresh();
    },
    onView(zoom) {
      els.zoomLevel.textContent = `${zoom.toFixed(1)}\u00d7`;
    },
    onMoveStart() {
      if (!isFinal) remember();
    },
    onMove(index, xIn, yIn) {
      if (isFinal) return;
      Object.assign(model.shots[index], { x_in: xIn, y_in: yIn, moved_by_user: true });
      refresh();
    },
    onAimClick: clickAim,
    onAimMove(xIn, yIn, done) {
      // Dragging the placed aim mark: it stays exactly where it is dropped (no snap).
      if (isFinal || model.busy || model.step !== "aim") return;
      placeAim({ x_in: xIn, y_in: yIn }, done ? "Aim point is set where you dropped it." : aimNote);
    },
    onRegion(x0, y0, x1, y1, done) {
      model.region = [x0, y0, x1, y1];
      if (!done) {
        refresh();
        return;
      }
      if (model.shots.some((s) => !inRegionOrUserPlaced(s))) {
        remember();
        model.shots = model.shots.filter(inRegionOrUserPlaced);
        model.selected = null;
      }
      setMode("region");
    },
  });
}

els.context.textContent = `${server.load_label} (lot ${server.lot_code}) at ${server.distance_yd} yd, ${server.purpose.replaceAll("_", " ")}`;
els.expected.addEventListener("change", () => {
  const value = Number(els.expected.value);
  if (Number.isInteger(value) && value >= 1 && value <= MAX_EXPECTED) model.expected = value;
  refresh();
});
els.detect.addEventListener("click", () => guarded(() => detect(), "detect"));
els.confirmAim.addEventListener("click", confirmAim);
els.shotsBack.addEventListener("click", backToAim);
els.aimCenterToggle.addEventListener("click", () => setAimMode("center"));
els.aimHandToggle.addEventListener("click", () => setAimMode("hand"));
els.remove.addEventListener("click", removeSelected);
els.undo.addEventListener("click", undo);
els.zoomIn.addEventListener("click", () => editor && editor.zoomBy(BUTTON_ZOOM_STEP));
els.zoomOut.addEventListener("click", () => editor && editor.zoomBy(1 / BUTTON_ZOOM_STEP));
els.zoomFit.addEventListener("click", () => editor && editor.fit());
els.finalize.addEventListener("click", done);
els.finalizeConfirm.addEventListener("click", finalizeGroup);
els.finalizeCancel.addEventListener("click", cancelFinalize);
els.finalizeBox.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !model.busy) cancelFinalize();
});
document.addEventListener("keydown", (event) => {
  const typing = event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement;
  if (typing || !editor) return;
  const onShots = model.step === "shots";
  if (onShots && (event.metaKey || event.ctrlKey) && !event.shiftKey && event.key.toLowerCase() === "z") {
    event.preventDefault();
    undo();
  } else if (onShots && (event.key === "Delete" || event.key === "Backspace")) {
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
showStep(model.step);
if (hasFrame && !isFinal) {
  if (model.step === "shots") openShots();
}
