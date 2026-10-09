/**
 * Asks before leaving a group that is still being created: a click on a top-bar link or on a link
 * marked `data-leave` (Exit, Cancel) opens the "Leave this group?" card, whose large, focused
 * Keep working stays on the page and whose small Leave follows the link. Ctrl/Cmd/Shift-clicks
 * (a new tab or window) are left alone.
 */
const dialog = document.getElementById("leave-dialog");

function wire() {
  const stay = document.getElementById("leave-stay");
  const go = document.getElementById("leave-go");
  let target = null;

  document.addEventListener(
    "click",
    (event) => {
      const link = event.target.closest("a[href]");
      if (!link || link.closest("dialog")) return;
      if (!link.closest(".site-header") && !link.hasAttribute("data-leave")) return;
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      target = link.href;
      dialog.showModal();
      stay.focus();
    },
    true,
  );
  stay.addEventListener("click", () => dialog.close());
  go.addEventListener("click", () => {
    if (target) window.location.assign(target);
  });
}

if (dialog) wire();
