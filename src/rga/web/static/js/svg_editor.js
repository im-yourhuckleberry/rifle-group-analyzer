/**
 * SVG editor for the review page. The photo is an <image> inside the SVG and the viewBox is the
 * visible window onto the working image in pixels, so zooming and panning only change the
 * viewBox. A point in plane inches is drawn at inches * ppi. Pointer positions are mapped to
 * working pixels through getScreenCTM, so drag, add and aim clicks stay exact at any zoom.
 * The editor owns drawing, zoom/pan and pointer input; the page keeps the shot list and decides
 * what each interaction means through the callbacks.
 */

import { BUTTON_ZOOM_STEP, createZoom } from "/static/js/svg_zoom.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const DRAG_THRESHOLD_PX = 4;
const AIM_ARM_IN = 0.35;
const CANDIDATE_RING_IN = 0.22;
const MIN_HIT_PX = 8;
// A click this many screen pixels outside a shot's circle still picks the shot, at any zoom.
const EDGE_SLACK_PX = 3;
const NATURAL_LONG_SIDE = 4000;
const REGION_PAD = 1.4;

function svgElement(name, attributes = {}) {
  const element = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  return element;
}

function clamp(value, low, high) {
  return Math.min(Math.max(value, low), high);
}

/**
 * Create the editor on `svg`.
 * Options: imageUrl, ppi, width, height (working px), radiusIn (bullet radius in inches), and the
 * callbacks onAdd(xIn, yIn), onSelect(index|null), onMoveStart(index), onMove(index, xIn, yIn),
 * onAimClick(xPx, yPx), onAimMove(xIn, yIn, done), onRegion(x0, y0, x1, y1, done) and
 * onView(zoomLevel).
 * A click on empty space deselects when a shot is selected and adds a shot otherwise. A dragged
 * shot keeps the offset from where it was grabbed, so it never jumps under the pointer. In aim mode
 * the click counts on release, and a drag pans instead of placing the aim; a drag that starts on
 * the placed aim mark moves the aim instead (with the same grab offset, and no snap).
 * `render({ shots, selected, aim, region, aimCandidates, activeCandidate })` draws everything;
 * `aimCandidates` are `{ x_in, y_in, label }` suggestions drawn as numbered rings, the one at
 * `activeCandidate` emphasised. Rings never take pointer input.
 * Returns { render, setMode, zoomBy, fit, zoomToRegion, zoomLevel, getView }.
 */
export function createEditor(svg, options) {
  const { ppi, width, height, radiusIn } = options;
  const aspect = height / width;
  let mode = "edit";
  let state = { shots: [], selected: null, aim: null, region: null };
  let drag = null;

  const image = svgElement("image", {
    href: options.imageUrl,
    x: 0,
    y: 0,
    width,
    height,
    preserveAspectRatio: "none",
  });
  const layer = svgElement("g");
  svg.replaceChildren(image, layer);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  // A large natural size in the photo's shape: the page's CSS scales the drawing down to the room
  // it has (as wide as the column, as tall as the window allows) without changing its shape.
  const natural = NATURAL_LONG_SIDE / Math.max(width, height);
  svg.setAttribute("width", String(Math.round(width * natural)));
  svg.setAttribute("height", String(Math.round(height * natural)));
  const zoom = createZoom(svg, {
    bounds: { x: 0, y: 0, w: width, h: height },
    onView(level) {
      draw();
      if (options.onView) options.onView(level);
    },
  });

  function pixelsPerUnit() {
    return zoom.pixelsPerUnit();
  }

  function toWorking(event) {
    const mapped = zoom.unitsUnder(event.clientX, event.clientY);
    return { x: clamp(mapped.x, 0, width - 1), y: clamp(mapped.y, 0, height - 1) };
  }

  function onAim(point) {
    if (!state.aim || !options.onAimMove) return false;
    const reach = Math.max(AIM_ARM_IN * ppi * 0.6, MIN_HIT_PX * 1.5 / pixelsPerUnit());
    return Math.hypot(state.aim.x_in * ppi - point.x, state.aim.y_in * ppi - point.y) <= reach;
  }

  function shotAt(point) {
    const radius = radiusIn * ppi;
    const reach = Math.max(radius * 1.15, radius + EDGE_SLACK_PX / pixelsPerUnit(), MIN_HIT_PX / pixelsPerUnit());
    let best = null;
    let bestDistance = reach;
    state.shots.forEach((shot, index) => {
      const distance = Math.hypot(shot.x_in * ppi - point.x, shot.y_in * ppi - point.y);
      if (distance <= bestDistance) {
        best = index;
        bestDistance = distance;
      }
    });
    return best;
  }

  function draw() {
    const labelScale = 1 / pixelsPerUnit();
    layer.replaceChildren();
    if (state.region) {
      const [x0, y0, x1, y1] = state.region;
      layer.append(
        svgElement("rect", {
          class: "aim-mark",
          x: x0,
          y: y0,
          width: x1 - x0,
          height: y1 - y0,
          "stroke-dasharray": "10 6",
        }),
      );
    }
    state.shots.forEach((shot, index) => {
      const cx = shot.x_in * ppi;
      const cy = shot.y_in * ppi;
      const classes = ["shot", shot.origin];
      if (index === state.selected) classes.push("selected");
      layer.append(svgElement("circle", { class: classes.join(" "), cx, cy, r: radiusIn * ppi }));
      if (shot.origin === "estimated") {
        const label = svgElement("text", {
          class: "shot-label",
          transform: `translate(${cx + radiusIn * ppi} ${cy - radiusIn * ppi}) scale(${labelScale})`,
        });
        label.textContent = "est";
        layer.append(label);
      }
    });
    (state.aimCandidates || []).forEach((candidate, index) => {
      const cx = candidate.x_in * ppi;
      const cy = candidate.y_in * ppi;
      const ring = CANDIDATE_RING_IN * ppi;
      const classes = ["aim-candidate"];
      if (index === state.activeCandidate) classes.push("active");
      layer.append(svgElement("circle", { class: classes.join(" "), cx, cy, r: ring }));
      const label = svgElement("text", {
        class: "aim-candidate-label",
        transform: `translate(${cx + ring} ${cy - ring}) scale(${labelScale})`,
      });
      label.textContent = `${index + 1} ${candidate.label}`;
      layer.append(label);
    });
    if (state.aim) {
      const cx = state.aim.x_in * ppi;
      const cy = state.aim.y_in * ppi;
      const arm = AIM_ARM_IN * ppi;
      layer.append(svgElement("line", { class: "aim-mark", x1: cx - arm, y1: cy, x2: cx + arm, y2: cy }));
      layer.append(svgElement("line", { class: "aim-mark", x1: cx, y1: cy - arm, x2: cx, y2: cy + arm }));
      layer.append(svgElement("circle", { class: "aim-mark", cx, cy, r: arm / 2 }));
    }
  }

  svg.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const point = toWorking(event);
    svg.setPointerCapture(event.pointerId);
    if (mode === "region") {
      drag = { kind: "region", start: point };
      return;
    }
    if (mode === "aim" && onAim(point)) {
      drag = {
        kind: "aim-move",
        start: point,
        offset: { x: state.aim.x_in * ppi - point.x, y: state.aim.y_in * ppi - point.y },
        moved: false,
      };
      return;
    }
    const index = mode === "aim" ? null : shotAt(point);
    if (index === null) {
      drag = {
        kind: mode === "aim" ? "aim" : "empty",
        start: point,
        clientStart: { x: event.clientX, y: event.clientY },
        pan: zoom.startPan(event.clientX, event.clientY),
        panning: false,
      };
      return;
    }
    options.onSelect(index);
    const shot = state.shots[index];
    drag = {
      kind: "shot",
      index,
      start: point,
      offset: { x: shot.x_in * ppi - point.x, y: shot.y_in * ppi - point.y },
      moved: false,
    };
  });

  svg.addEventListener("pointermove", (event) => {
    if (!drag) return;
    if (drag.kind === "empty" || drag.kind === "aim") {
      const dx = event.clientX - drag.clientStart.x;
      const dy = event.clientY - drag.clientStart.y;
      if (!drag.panning && Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return;
      drag.panning = true;
      svg.classList.add("is-grabbing");
      drag.pan.move(event.clientX, event.clientY);
      return;
    }
    const point = toWorking(event);
    if (drag.kind === "aim-move") {
      const travelled = Math.hypot(point.x - drag.start.x, point.y - drag.start.y) * pixelsPerUnit();
      if (!drag.moved && travelled < DRAG_THRESHOLD_PX) return;
      drag.moved = true;
      svg.classList.add("is-grabbing");
      moveAim(drag, point, false);
      return;
    }
    if (drag.kind === "shot") {
      const travelled = Math.hypot(point.x - drag.start.x, point.y - drag.start.y) * pixelsPerUnit();
      if (!drag.moved && travelled < DRAG_THRESHOLD_PX) return;
      if (!drag.moved && options.onMoveStart) options.onMoveStart(drag.index);
      drag.moved = true;
      svg.classList.add("is-grabbing");
      moveShot(drag, point);
    } else if (drag.kind === "region") {
      options.onRegion(
        Math.min(drag.start.x, point.x),
        Math.min(drag.start.y, point.y),
        Math.max(drag.start.x, point.x),
        Math.max(drag.start.y, point.y),
        false,
      );
    }
  });

  function moveShot(shotDrag, point) {
    const x = clamp(point.x + shotDrag.offset.x, 0, width - 1);
    const y = clamp(point.y + shotDrag.offset.y, 0, height - 1);
    options.onMove(shotDrag.index, x / ppi, y / ppi);
  }

  function moveAim(aimDrag, point, done) {
    const x = clamp(point.x + aimDrag.offset.x, 0, width - 1);
    const y = clamp(point.y + aimDrag.offset.y, 0, height - 1);
    options.onAimMove(x / ppi, y / ppi, done);
  }

  function finish(event) {
    if (!drag) return;
    const point = toWorking(event);
    const finished = drag;
    drag = null;
    svg.classList.remove("is-grabbing");
    if (finished.pan) finished.pan.end();
    if (finished.kind === "aim-move") {
      if (finished.moved) moveAim(finished, point, true);
      else options.onAimClick(point.x, point.y);
    } else if (finished.kind === "aim") {
      if (!finished.panning) options.onAimClick(point.x, point.y);
    } else if (finished.kind === "shot" && finished.moved) {
      moveShot(finished, point);
    } else if (finished.kind === "empty" && !finished.panning) {
      if (state.selected !== null) options.onSelect(null);
      else options.onAdd(point.x / ppi, point.y / ppi);
    } else if (finished.kind === "region") {
      const x0 = Math.round(Math.min(finished.start.x, point.x));
      const y0 = Math.round(Math.min(finished.start.y, point.y));
      const x1 = Math.round(Math.max(finished.start.x, point.x));
      const y1 = Math.round(Math.max(finished.start.y, point.y));
      if (x1 - x0 >= 8 && y1 - y0 >= 8) options.onRegion(x0, y0, x1, y1, true);
    }
  }
  svg.addEventListener("pointerup", finish);
  svg.addEventListener("pointercancel", () => {
    if (drag && drag.pan) drag.pan.end();
    drag = null;
    svg.classList.remove("is-grabbing");
  });

  zoom.fit();

  return {
    render(next) {
      state = next;
      draw();
    },
    setMode(next) {
      mode = next;
      if (drag && drag.pan) drag.pan.end();
      drag = null;
    },
    zoomBy: zoom.zoomBy,
    fit() {
      zoom.fit({ animate: true });
    },
    zoomToRegion(region, { animate = false } = {}) {
      const [x0, y0, x1, y1] = region;
      const w = Math.max((x1 - x0) * REGION_PAD, ((y1 - y0) * REGION_PAD) / aspect);
      zoom.setView({ x: (x0 + x1) / 2 - w / 2, y: (y0 + y1) / 2 - (w * aspect) / 2, w }, { animate });
    },
    zoomLevel: zoom.zoomLevel,
    getView: zoom.getView,
  };
}

export { BUTTON_ZOOM_STEP };
