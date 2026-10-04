/**
 * Settings page: saves which share-card layers start checked. The request goes through api.js;
 * the saved values come back from the server and are shown as the checkbox state.
 */
import { apiSend, ApiError } from "/static/js/api.js";

const section = document.getElementById("share-settings");
if (section) {
  const boxes = [...section.querySelectorAll("input[data-share-default]")];
  const save = document.getElementById("save-share-defaults");
  const status = document.getElementById("share-defaults-status");
  const flash = document.getElementById("flash");

  const isOn = (layer) => boxes.some((box) => box.dataset.shareDefault === layer && box.checked);

  function current() {
    return {
      photo: isOn("photo"),
      spread: isOn("spread"),
      thing_hitter: isOn("thing_hitter"),
      poi: isOn("poi"),
      correction: isOn("correction"),
      numbers: isOn("numbers"),
    };
  }

  function fail(error) {
    status.textContent = "";
    flash.textContent =
      error instanceof ApiError
        ? error.code === "busy"
          ? `${error.detail} (try again)`
          : error.detail
        : "Something went wrong";
    flash.hidden = false;
  }

  save.addEventListener("click", async () => {
    flash.hidden = true;
    status.textContent = "Saving...";
    save.disabled = true;
    try {
      const saved = await apiSend("PUT", "/api/settings/share-defaults", current());
      for (const box of boxes) box.checked = saved[box.dataset.shareDefault] === true;
      status.textContent = "Saved. New share cards start with these layers.";
    } catch (error) {
      fail(error);
    } finally {
      save.disabled = false;
    }
  });
  for (const box of boxes) {
    box.addEventListener("change", () => {
      status.textContent = "";
    });
  }
}
