/**
 * "Add new group" dialog (home and Groups pages): choose a rifle and the target photo, then
 * Continue uploads the photo and opens the New group page for that rifle with the photo attached
 * (`/groups/new?rifle_id=...&photo_id=...`); the usual flow carries on: details, reference, aim, shots.
 * Every request goes through api.js; user text reaches the page only as text.
 */
import { ApiError, apiUpload } from "/static/js/api.js";

const dialog = document.getElementById("new-group-dialog");

function wire() {
  const form = document.getElementById("new-group-form");
  const rifle = document.getElementById("ng-rifle");
  const photo = document.getElementById("ng-photo");
  const error = document.getElementById("ng-error");
  const go = document.getElementById("ng-continue");
  let busy = false;

  function fail(message) {
    if (!error) return;
    error.textContent = message;
    error.hidden = false;
  }

  function open() {
    if (error) error.hidden = true;
    dialog.showModal();
    (rifle || dialog.querySelector("[data-new-group-close]")).focus();
  }

  for (const button of document.querySelectorAll("[data-new-group-open]")) {
    button.addEventListener("click", open);
  }
  for (const button of dialog.querySelectorAll("[data-new-group-close]")) {
    button.addEventListener("click", () => {
      if (!busy) dialog.close();
    });
  }
  dialog.addEventListener("cancel", (event) => {
    if (busy) event.preventDefault();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || !rifle) return;
    error.hidden = true;
    if (rifle.value === "") {
      fail("Choose a rifle.");
      return;
    }
    const file = photo.files[0];
    if (!file) {
      fail("Choose the target photo.");
      return;
    }
    busy = true;
    go.disabled = true;
    go.textContent = "Uploading…";
    try {
      const uploaded = await apiUpload("/api/photos", file);
      window.location.assign(`/groups/new?rifle_id=${Number(rifle.value)}&photo_id=${uploaded.id}`);
    } catch (problem) {
      fail(problem instanceof ApiError ? problem.detail : "The photo could not be uploaded; try again.");
      busy = false;
      go.disabled = false;
      go.textContent = "Continue";
    }
  });
}

if (dialog) wire();
