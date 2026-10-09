/**
 * Groups tab Edit mode: Edit shows a checkbox on every row (a row click then selects it instead of
 * opening it), Delete asks in a dialog whose Cancel is the large, focused default, and only its
 * small Delete button removes the chosen groups (`POST /api/groups/delete`), then reloads.
 * Every request goes through api.js; user text reaches the page only as text.
 */
import { ApiError, apiSend } from "/static/js/api.js";

const table = document.querySelector("[data-groups-table]");
const toggle = document.getElementById("groups-edit");

function wire() {
  const bar = document.getElementById("groups-edit-bar");
  const done = document.getElementById("groups-edit-done");
  const count = document.getElementById("groups-selected");
  const remove = document.getElementById("groups-delete");
  const all = document.getElementById("groups-select-all");
  const dialog = document.getElementById("groups-delete-dialog");
  const what = document.getElementById("groups-delete-count");
  const cancel = document.getElementById("groups-delete-cancel");
  const confirm = document.getElementById("groups-delete-confirm");
  const error = document.getElementById("groups-delete-error");
  const boxes = [...table.querySelectorAll("input.group-select")];
  let busy = false;

  const chosen = () => boxes.filter((box) => box.checked).map((box) => Number(box.value));
  const label = (n) => `${n} group${n === 1 ? "" : "s"}`;

  function refresh() {
    const n = chosen().length;
    count.textContent = `${n} selected`;
    remove.disabled = n === 0;
    remove.textContent = n === 0 ? "Delete" : `Delete ${label(n)}`;
    all.checked = n > 0 && n === boxes.length;
    all.indeterminate = n > 0 && n < boxes.length;
    for (const box of boxes) box.closest("tr").classList.toggle("is-selected", box.checked);
  }

  function setEditing(on) {
    table.classList.toggle("is-editing", on);
    bar.hidden = !on;
    toggle.setAttribute("aria-pressed", String(on));
    toggle.textContent = on ? "Editing" : "Edit";
    if (!on) for (const box of boxes) box.checked = false;
    refresh();
  }

  toggle.addEventListener("click", () => setEditing(!table.classList.contains("is-editing")));
  done.addEventListener("click", () => setEditing(false));
  all.addEventListener("change", () => {
    for (const box of boxes) box.checked = all.checked;
    refresh();
  });
  for (const box of boxes) box.addEventListener("change", refresh);
  table.addEventListener("click", (event) => {
    if (!table.classList.contains("is-editing")) return;
    const row = event.target.closest("tr[data-group-id]");
    if (!row) return;
    if (event.target.closest("a")) event.preventDefault();
    if (event.target.closest("input")) return;
    const box = row.querySelector("input.group-select");
    box.checked = !box.checked;
    refresh();
  });

  remove.addEventListener("click", () => {
    const n = chosen().length;
    if (n === 0) return;
    what.textContent = label(n);
    confirm.textContent = `Delete ${label(n)}`;
    error.hidden = true;
    dialog.showModal();
    cancel.focus();
  });
  cancel.addEventListener("click", () => {
    if (!busy) dialog.close();
  });
  dialog.addEventListener("cancel", (event) => {
    if (busy) event.preventDefault();
  });
  confirm.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    confirm.disabled = true;
    cancel.disabled = true;
    confirm.textContent = "Deleting…";
    try {
      await apiSend("POST", "/api/groups/delete", { group_ids: chosen() });
      window.location.reload();
    } catch (problem) {
      error.textContent = problem instanceof ApiError ? problem.detail : "The groups could not be deleted; try again.";
      error.hidden = false;
      busy = false;
      confirm.disabled = false;
      cancel.disabled = false;
      confirm.textContent = `Delete ${label(chosen().length)}`;
    }
  });
  refresh();
}

if (table && toggle) wire();
