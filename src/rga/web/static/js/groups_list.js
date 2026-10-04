/**
 * Page script for /groups: a click on a row opens its group, and the load list follows the chosen
 * rifle. Both work without it (each row has a real link; the server narrows the loads on submit).
 */

function wireRows() {
  const table = document.querySelector("[data-groups-table]");
  if (!table) return;
  table.addEventListener("click", (event) => {
    if (event.target.closest("a")) return;
    const row = event.target.closest("tr[data-href]");
    if (row) window.location.assign(row.dataset.href);
  });
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
wireLoadsByRifle();
