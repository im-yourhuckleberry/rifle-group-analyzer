/**
 * Shared viewBox zoom and pan for SVG pages (the review editor and the group results plot).
 * Zooming only changes the SVG viewBox, so everything inside it zooms together. The view is
 * clamped to 1x-16x of `bounds` and kept inside it. Pointer positions map to SVG units through
 * getScreenCTM, so anchoring and hit tests stay exact at any zoom.
 */

export const MAX_ZOOM = 16;
export const BUTTON_ZOOM_STEP = 1.5;
const WHEEL_ZOOM_RATE = 0.0018;
const PINCH_ZOOM_RATE = 0.012;
const WHEEL_LINE_PX = 16;

function clamp(value, low, high) {
  return Math.min(Math.max(value, low), high);
}

/**
 * Attach zoom to `svg`. `options.bounds` is `{ x, y, w, h }`, the full (1x) view; `options.onView`
 * is called as `onView(zoomLevel, view)` after every change; call `fit()` once to set the first view.
 * Returns { setView, zoomAt, zoomBy, fit, zoomLevel, getView, pixelsPerUnit, unitsUnder, startPan }.
 * The wheel listener lives on `svg` only, so the page scrolls normally elsewhere.
 */
export function createZoom(svg, options) {
  const { bounds } = options;
  const aspect = bounds.h / bounds.w;
  let view = { ...bounds };

  function pixelsPerUnit() {
    const rect = svg.getBoundingClientRect();
    return rect.width > 0 ? rect.width / view.w : 1;
  }

  function unitsUnder(clientX, clientY) {
    const point = svg.createSVGPoint();
    point.x = clientX;
    point.y = clientY;
    const mapped = point.matrixTransform(svg.getScreenCTM().inverse());
    return { x: mapped.x, y: mapped.y };
  }

  function setView(next) {
    const w = clamp(next.w, bounds.w / MAX_ZOOM, bounds.w);
    const h = w * aspect;
    view = {
      x: clamp(next.x, bounds.x, bounds.x + bounds.w - w),
      y: clamp(next.y, bounds.y, bounds.y + bounds.h - h),
      w,
      h,
    };
    svg.setAttribute("viewBox", `${view.x} ${view.y} ${view.w} ${view.h}`);
    if (options.onView) options.onView(bounds.w / view.w, { ...view });
  }

  function zoomAt(factor, clientX, clientY) {
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

  return {
    setView,
    zoomAt,
    zoomBy(factor) {
      const rect = svg.getBoundingClientRect();
      zoomAt(factor, rect.left + rect.width / 2, rect.top + rect.height / 2);
    },
    fit() {
      setView({ ...bounds });
    },
    zoomLevel: () => bounds.w / view.w,
    getView: () => ({ ...view }),
    pixelsPerUnit,
    unitsUnder,
    startPan(clientX, clientY) {
      const origin = { ...view };
      const scale = pixelsPerUnit();
      return {
        move(x, y) {
          setView({
            x: origin.x - (x - clientX) / scale,
            y: origin.y - (y - clientY) / scale,
            w: origin.w,
          });
        },
      };
    },
  };
}
