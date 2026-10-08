/**
 * Shared zoom and pan for SVG pages (the review editor, the group results plot and the reference
 * page). The view is a window `{ x, y, w, h }` onto `bounds`, clamped to 1x-16x and kept inside
 * it. Pointer positions map to SVG units through getScreenCTM, so anchoring and hit tests stay
 * exact at any zoom and in the middle of an animation.
 *
 * Motion: wheel zoom and drag-pan follow the input 1:1. Button, key and fit changes animate on a
 * critically damped spring that starts from the view on screen and re-targets with its current
 * velocity, so pressing + three times quickly lands at 1.5^3 without a hitch. Panning past an edge
 * rubber-bands; on release the pan keeps the pointer's velocity, coasts to the projected resting
 * point and springs back inside the bounds. With prefers-reduced-motion every change is instant.
 * While a spring runs the SVG carries `data-animating`, so tests and callers can wait for rest.
 */

export const MAX_ZOOM = 16;
export const BUTTON_ZOOM_STEP = 1.5;
const WHEEL_ZOOM_RATE = 0.0018;
const PINCH_ZOOM_RATE = 0.012;
const WHEEL_LINE_PX = 16;
const SPRING_RESPONSE_S = 0.35;
const DECELERATION_RATE = 0.998;
const RUBBER_BAND_CONSTANT = 0.55;
const VELOCITY_WINDOW_MS = 80;
const MAX_FRAME_S = 1 / 30;
const SETTLE_SCREEN_PX = 0.25;
const SETTLE_SCREEN_PX_PER_S = 4;

const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

function clamp(value, low, high) {
  return Math.min(Math.max(value, low), high);
}

/** Distance a release velocity (units/s) coasts before stopping, as scroll deceleration does. */
export function project(velocity, decelerationRate = DECELERATION_RATE) {
  return ((velocity / 1000) * decelerationRate) / (1 - decelerationRate);
}

/** How far past an edge the view follows a drag that overshoots it by `overshoot`. */
export function rubberband(overshoot, dimension, constant = RUBBER_BAND_CONSTANT) {
  return (overshoot * dimension * constant) / (dimension + constant * Math.abs(overshoot));
}

/**
 * Attach zoom to `svg`. `options.bounds` is `{ x, y, w, h }`, the full (1x) view; `options.onView`
 * is called as `onView(zoomLevel, view)` on every change, every frame of an animation included;
 * call `fit()` once to set the first view (instantly).
 * By default the view is applied as the SVG viewBox. A page that zooms some other way passes
 * `options.apply(view)` and `options.frame`, the untransformed element the view fills on screen.
 * `zoomBy`, `fit({ animate: true })` and `setView(view, { animate: true })` animate; `startPan`
 * returns `{ move(x, y), end() }` and `end()` must be called on every release.
 * Returns { setView, zoomAt, zoomBy, fit, zoomLevel, getView, pixelsPerUnit, unitsUnder, startPan }.
 * The wheel listener lives on `svg` only, so the page scrolls normally elsewhere.
 */
export function createZoom(svg, options) {
  const { bounds } = options;
  const aspect = bounds.h / bounds.w;
  const frame = options.frame || svg;
  const apply = options.apply || ((v) => svg.setAttribute("viewBox", `${v.x} ${v.y} ${v.w} ${v.h}`));
  const omega = (2 * Math.PI) / SPRING_RESPONSE_S;
  let view = { ...bounds };
  let spring = null;

  function pixelsPerUnit() {
    const rect = frame.getBoundingClientRect();
    return rect.width > 0 ? rect.width / view.w : 1;
  }

  function unitsUnder(clientX, clientY) {
    const point = svg.createSVGPoint();
    point.x = clientX;
    point.y = clientY;
    const mapped = point.matrixTransform(svg.getScreenCTM().inverse());
    return { x: mapped.x, y: mapped.y };
  }

  function inside(next) {
    const w = clamp(next.w, bounds.w / MAX_ZOOM, bounds.w);
    const h = w * aspect;
    return {
      x: clamp(next.x, bounds.x, bounds.x + bounds.w - w),
      y: clamp(next.y, bounds.y, bounds.y + bounds.h - h),
      w,
      h,
    };
  }

  function show(next) {
    view = { x: next.x, y: next.y, w: next.w, h: next.w * aspect };
    apply(view);
    if (options.onView) options.onView(bounds.w / view.w, { ...view });
  }

  function stop() {
    if (!spring) return;
    cancelAnimationFrame(spring.frame);
    spring = null;
    delete svg.dataset.animating;
  }

  function step(now) {
    const dt = Math.min(Math.max(now - spring.last, 0) / 1000, MAX_FRAME_S);
    spring.last = now;
    const decay = Math.exp(-omega * dt);
    const next = {};
    let resting = true;
    const ppu = pixelsPerUnit();
    for (const key of ["x", "y", "w"]) {
      // Exact critically damped step for displacement d and velocity v.
      const d = view[key] - spring.goal[key];
      const c = spring.velocity[key] + omega * d;
      const nd = (d + c * dt) * decay;
      spring.velocity[key] = (c - omega * (d + c * dt)) * decay;
      next[key] = spring.goal[key] + nd;
      if (Math.abs(nd) * ppu > SETTLE_SCREEN_PX || Math.abs(spring.velocity[key]) * ppu > SETTLE_SCREEN_PX_PER_S) {
        resting = false;
      }
    }
    if (resting) {
      const goal = spring.goal;
      stop();
      show(goal);
      return;
    }
    show(next);
    spring.frame = requestAnimationFrame(step);
  }

  function animateTo(target, velocity = null) {
    const goal = inside(target);
    if (reducedMotion.matches) {
      stop();
      show(goal);
      return;
    }
    const carried = velocity || (spring ? spring.velocity : { x: 0, y: 0, w: 0 });
    if (spring) {
      spring.goal = goal;
      spring.velocity = { ...carried };
      return;
    }
    spring = { goal, velocity: { ...carried }, last: performance.now(), frame: 0 };
    svg.dataset.animating = "true";
    spring.frame = requestAnimationFrame(step);
  }

  /** The view a change should build on: the spring's goal while animating, else the view. */
  function base() {
    return spring ? spring.goal : view;
  }

  function setView(next, { animate = false } = {}) {
    if (animate) {
      animateTo(next);
      return;
    }
    stop();
    show(inside(next));
  }

  function zoomAt(factor, clientX, clientY) {
    stop();
    const anchor = unitsUnder(clientX, clientY);
    const w = clamp(view.w / factor, bounds.w / MAX_ZOOM, bounds.w);
    const ratio = w / view.w;
    setView({
      x: anchor.x - (anchor.x - view.x) * ratio,
      y: anchor.y - (anchor.y - view.y) * ratio,
      w,
    });
  }

  svg.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      const lines = event.deltaMode === 1 ? WHEEL_LINE_PX : 1;
      const rate = event.ctrlKey ? PINCH_ZOOM_RATE : WHEEL_ZOOM_RATE;
      zoomAt(Math.exp(-event.deltaY * lines * rate), event.clientX, event.clientY);
    },
    { passive: false },
  );

  function startPan(clientX, clientY) {
    // Grabbing the view stops any animation where it is: the pan starts from what is on screen.
    stop();
    const origin = { ...view };
    const scale = pixelsPerUnit();
    const samples = [{ t: performance.now(), x: clientX, y: clientY }];

    function follow(raw, low, high, dimension) {
      if (raw < low) return low - rubberband(low - raw, dimension);
      if (raw > high) return high + rubberband(raw - high, dimension);
      return raw;
    }

    return {
      move(x, y) {
        const now = performance.now();
        samples.push({ t: now, x, y });
        while (samples.length > 2 && now - samples[0].t > VELOCITY_WINDOW_MS) samples.shift();
        show({
          x: follow(origin.x - (x - clientX) / scale, bounds.x, bounds.x + bounds.w - origin.w, origin.w),
          y: follow(origin.y - (y - clientY) / scale, bounds.y, bounds.y + bounds.h - origin.h, origin.h),
          w: origin.w,
        });
      },
      end() {
        const now = performance.now();
        const recent = samples.filter((s) => now - s.t <= VELOCITY_WINDOW_MS);
        let velocity = { x: 0, y: 0, w: 0 };
        // Reduced motion: no coasting; a pan past an edge just settles back inside.
        if (recent.length >= 2 && !reducedMotion.matches) {
          const first = recent[0];
          const last = recent[recent.length - 1];
          const seconds = Math.max((last.t - first.t) / 1000, 1e-3);
          velocity = { x: -(last.x - first.x) / seconds / scale, y: -(last.y - first.y) / seconds / scale, w: 0 };
        }
        const current = { ...view };
        const target = { x: current.x + project(velocity.x), y: current.y + project(velocity.y), w: current.w };
        const settled = inside(target);
        if (settled.x === current.x && settled.y === current.y) return;
        animateTo(target, velocity);
      },
    };
  }

  return {
    setView,
    zoomAt,
    zoomBy(factor) {
      const from = base();
      const w = clamp(from.w / factor, bounds.w / MAX_ZOOM, bounds.w);
      const cx = from.x + from.w / 2;
      const cy = from.y + (from.w * aspect) / 2;
      animateTo({ x: cx - w / 2, y: cy - (w * aspect) / 2, w });
    },
    fit({ animate = false } = {}) {
      setView({ ...bounds }, { animate });
    },
    zoomLevel: () => bounds.w / view.w,
    getView: () => ({ ...view }),
    pixelsPerUnit,
    unitsUnder,
    startPan,
  };
}
