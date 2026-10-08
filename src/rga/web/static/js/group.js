/**
 * Group results page: the plot's layer toggles (photo, spread circle, mean POI), the correction
 * toggle and its "i" card, and viewBox zoom and pan of the plot (shared `svg_zoom.js`).
 * Client-side only; checkboxes show or hide SVG groups through a CSS class and the correction
 * toggle adds a class to the plot. Nothing is stored or sent.
 */

import { wireShare } from "/static/js/share_card.js";
import { BUTTON_ZOOM_STEP, createZoom } from "/static/js/svg_zoom.js";

const HIDDEN = "is-hidden";
const plot = document.querySelector(".group-plot");

for (const box of document.querySelectorAll("input[data-layer]")) {
  const layer = document.querySelector(`.layer-${box.dataset.layer}`);
  if (!layer) continue;
  const apply = () => layer.classList.toggle(HIDDEN, !box.checked);
  box.addEventListener("change", apply);
  apply();
}

const correction = document.querySelector("input[data-correction]");
if (correction && plot) {
  const parts = document.querySelectorAll(".layer-correction, .layer-correction-note");
  const apply = () => {
    plot.classList.toggle("correction-on", correction.checked);
    for (const part of parts) part.classList.toggle(HIDDEN, !correction.checked);
  };
  correction.addEventListener("change", apply);
  apply();
}

const infoButton = document.getElementById("correction-info");
const infoCard = document.getElementById("correction-card");
if (infoButton && infoCard) {
  const setOpen = (open) => {
    infoCard.classList.toggle(HIDDEN, !open);
    infoButton.setAttribute("aria-expanded", String(open));
  };
  infoButton.addEventListener("click", (event) => {
    event.stopPropagation();
    setOpen(infoCard.classList.contains(HIDDEN));
  });
  document.addEventListener("click", (event) => {
    if (!infoCard.contains(event.target)) setOpen(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setOpen(false);
  });
}

const PLOT_FRAME = plot ? plot.parentElement : null;

function counterScaled() {
  const placed = [];
  for (const label of plot.querySelectorAll(".shot-label")) {
    const x = Number(label.getAttribute("x"));
    const y = Number(label.getAttribute("y"));
    label.setAttribute("x", "0");
    label.setAttribute("y", "0");
    placed.push({ node: label, at: `translate(${x} ${y}) ` });
  }
  const poi = plot.querySelector(".layer-poi");
  if (poi) placed.push({ node: poi, at: `${poi.getAttribute("transform")} ` });
  const aim = plot.querySelector("g.aim-mark");
  if (aim) placed.push({ node: aim, at: "" });
  return placed;
}

function wireZoom() {
  const [x, y, w, h] = plot.getAttribute("viewBox").split(" ").map(Number);
  const readout = document.getElementById("plot-zoom-level");
  const markers = counterScaled();
  const zoom = createZoom(plot, {
    bounds: { x, y, w, h },
    onView(level) {
      readout.textContent = `${level.toFixed(1)}\u00d7`;
      for (const marker of markers) marker.node.setAttribute("transform", `${marker.at}scale(${1 / level})`);
    },
  });
  zoom.fit();
  document.getElementById("plot-zoom-in").addEventListener("click", () => zoom.zoomBy(BUTTON_ZOOM_STEP));
  document.getElementById("plot-zoom-out").addEventListener("click", () => zoom.zoomBy(1 / BUTTON_ZOOM_STEP));
  document.getElementById("plot-zoom-fit").addEventListener("click", () => zoom.fit({ animate: true }));

  let pan = null;
  plot.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    plot.setPointerCapture(event.pointerId);
    pan = zoom.startPan(event.clientX, event.clientY);
    PLOT_FRAME.classList.add("is-panning");
  });
  plot.addEventListener("pointermove", (event) => {
    if (pan) pan.move(event.clientX, event.clientY);
  });
  const stop = () => {
    if (pan) pan.end();
    pan = null;
    PLOT_FRAME.classList.remove("is-panning");
  };
  plot.addEventListener("pointerup", stop);
  plot.addEventListener("pointercancel", stop);
}

if (plot && document.getElementById("plot-zoom-fit")) wireZoom();
wireShare();
