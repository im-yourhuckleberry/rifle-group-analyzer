/**
 * Page script for /groups: a click on a row opens its group, hovering (or focusing) a row shows its
 * photo as a thumbnail at the row's right end, and the load list follows the chosen rifle. Rows
 * and filters work without it (each row has a real link; the server narrows the loads on submit).
 * A thumbnail is only fetched the first time its row is hovered.
 */

function wireRows() {
  const table = document.querySelector("[data-groups-table]");
  if (!table) return;
  table.addEventListener("click", (event) => {
    // In Edit mode a row click selects the row instead (groups_edit.js).
    if (table.classList.contains("is-editing")) return;
    if (event.target.closest("a")) return;
    const row = event.target.closest("tr[data-href]");
    if (row) window.location.assign(row.dataset.href);
  });
}

function wireThumbnails() {
  const table = document.querySelector("[data-groups-table]");
  if (!table) return;
  const reveal = (event) => {
    const row = event.target.closest("tr[data-group-id]");
    const thumb = row && row.querySelector("img.row-thumb[data-thumb]");
    if (!thumb) return;
    thumb.src = thumb.dataset.thumb;
    delete thumb.dataset.thumb;
  };
  table.addEventListener("mouseover", reveal);
  table.addEventListener("focusin", reveal);
}

function wireLoadsByRifle() {
  const rifle = document.getElementById("f-rifle");
  const load = document.getElementById("f-load");
  if (!rifle || !load) return;
  const narrow = () => {
    const wanted = rifle.value;
    Array.from(load.options).forEach((option) => {
      if (!option.dataset.rifleId) return;
      const hidden = wanted !== "" && option.dataset.rifleId !== wanted;
      option.hidden = hidden;
      option.disabled = hidden;
    });
    if (load.selectedOptions.length && load.selectedOptions[0].disabled) load.value = "";
  };
  rifle.addEventListener("change", narrow);
  narrow();
}

wireRows();
wireThumbnails();
wireLoadsByRifle();
