/**
 * Dope page: fetch the load's rows through api.js and render them as a table.
 * Every value is written with textContent; failures go to #flash.
 */
import { apiGet } from "/static/js/api.js";

const MIL = 2;
const root = document.querySelector('[data-page="dope"]');
const mount = document.getElementById("dope-table");

function flash(message) {
  const box = document.getElementById("flash");
  box.textContent = message;
  box.hidden = false;
}

function signed(value) {
  const text = value.toFixed(MIL);
  return value > 0 ? `+${text}` : text;
}

function cell(row, text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  row.appendChild(td);
  return td;
}

function render(rows) {
  mount.replaceChildren();
  if (rows.length === 0) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent =
      "No finalized groups with a recorded elevation yet. Record the elevation on a group and finalize it to see it here.";
    mount.appendChild(empty);
    return;
  }
  const table = document.createElement("table");
  const head = table.createTHead().insertRow();
  for (const [label, numeric] of [
    ["Distance (yd)", true],
    ["Date", false],
    ["Dialed (mil)", true],
    ["Group center (mil)", true],
    ["Centered elevation (mil)", true],
    ["Shots", true],
    ["Notes", false],
  ]) {
    const th = document.createElement("th");
    th.textContent = label;
    if (numeric) th.className = "num";
    head.appendChild(th);
  }
  const body = table.createTBody();
  for (const item of rows) {
    const tr = body.insertRow();
    cell(tr, String(item.distance_yd), "num");
    cell(tr, item.shot_time ? `${item.shot_date} ${item.shot_time.slice(0, 5)}` : item.shot_date);
    cell(tr, item.elevation_dialed_mil.toFixed(MIL), "num");
    cell(tr, signed(item.center_dy_mil), "num");
    cell(tr, item.centered_elevation_mil.toFixed(MIL), "num");
    cell(tr, String(item.shots), "num");
    cell(tr, "");
  }
  mount.appendChild(table);
}

async function load() {
  try {
    const result = await apiGet(`/api/loads/${Number(root.dataset.loadId)}/dope`);
    render(result.rows);
  } catch (error) {
    flash(error.detail || error.message || "The dope table could not be loaded");
  }
}

load();
