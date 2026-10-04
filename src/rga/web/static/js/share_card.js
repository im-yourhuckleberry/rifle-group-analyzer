/**
 * Group page share card: layer checkboxes, a live preview of the share image and a download link.
 * The preview is an <img> pointed at `/api/groups/{id}/share.png?layers=...`; checkboxes start from
 * the saved share defaults (embedded in `data-defaults`, set in Settings) each time the card opens,
 * and a layer the group cannot offer stays disabled with the reason the server rendered. Nothing
 * is stored from here.
 */

const HIDDEN = "is-hidden";
const REDRAW_DELAY_MS = 250;

export function wireShare() {
  const open = document.getElementById("share-open");
  const card = document.getElementById("share-card");
  const root = document.querySelector('[data-page="group"]');
  if (!open || !card || !root) return;
  const groupId = Number(root.dataset.groupId);
  const boxes = [...card.querySelectorAll("input[data-share-layer]")];
  const preview = document.getElementById("share-preview");
  const download = document.getElementById("share-download");
  const status = document.getElementById("share-status");

  const chosen = () => boxes.filter((box) => box.checked && !box.disabled).map((box) => box.dataset.shareLayer);

  let pending = null;

  function address() {
    return `/api/groups/${groupId}/share.png?layers=${chosen().join(",")}`;
  }

  function redraw() {
    pending = null;
    status.textContent = "Drawing the preview...";
    preview.src = address();
  }

  function refresh() {
    status.textContent = "Drawing the preview...";
    download.href = address();
    if (pending !== null) clearTimeout(pending);
    pending = setTimeout(redraw, REDRAW_DELAY_MS);
  }

  const saved = JSON.parse(card.dataset.defaults || "{}");

  function presetFromDefaults() {
    for (const box of boxes) box.checked = !box.disabled && saved[box.dataset.shareLayer] === true;
  }

  preview.addEventListener("load", () => {
    status.textContent = "";
  });
  preview.addEventListener("error", () => {
    status.textContent = "The preview could not be drawn. Try again in a moment.";
  });
  for (const box of boxes) box.addEventListener("change", refresh);
  open.addEventListener("click", () => {
    const opening = card.classList.contains(HIDDEN);
    card.classList.toggle(HIDDEN, !opening);
    open.setAttribute("aria-expanded", String(opening));
    if (opening) {
      presetFromDefaults();
      refresh();
    }
  });
}
