/**
 * Suppressor picker: a checkbox, and, only while it is checked, a dropdown of the saved
 * suppressors with "Add new..." to type and remember another. Markup comes from
 * `suppressor_picker.html`. Requests go through api.js; names reach the page only as text.
 */
import { apiSend, ApiError } from "/static/js/api.js";

const NEW_CHOICE = "__new__";

/**
 * Wire the picker inside `root` ([data-suppressor-picker]). `onError(message)` shows failures.
 * Returns `{ suppressed(), suppressorId() }`: whether the box is checked, and the chosen saved
 * suppressor id (null when unchecked, "Not named", or an unsaved new name).
 */
export function wireSuppressorPicker(root, onError) {
  const check = root.querySelector("[data-suppressor-check]");
  const choice = root.querySelector("[data-suppressor-choice]");
  const select = root.querySelector("[data-suppressor-select]");
  const fresh = root.querySelector("[data-suppressor-new]");
  const input = root.querySelector("[data-suppressor-name]");
  const save = root.querySelector("[data-suppressor-save]");

  const refresh = () => {
    choice.hidden = !check.checked;
    fresh.hidden = !check.checked || select.value !== NEW_CHOICE;
  };

  async function remember() {
    const name = input.value.trim();
    if (name === "") {
      onError("Type the suppressor's name first.");
      return;
    }
    try {
      const saved = await apiSend("POST", "/api/suppressors", { name });
      let option = [...select.options].find((o) => o.value === String(saved.id));
      if (!option) {
        option = document.createElement("option");
        option.value = String(saved.id);
        option.textContent = saved.name;
        select.insertBefore(option, select.querySelector(`option[value="${NEW_CHOICE}"]`));
      }
      select.value = option.value;
      input.value = "";
      refresh();
    } catch (error) {
      onError(error instanceof ApiError ? error.detail : "Could not save the suppressor");
    }
  }

  check.addEventListener("change", refresh);
  select.addEventListener("change", () => {
    refresh();
    if (select.value === NEW_CHOICE) input.focus();
  });
  save.addEventListener("click", remember);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      remember();
    }
  });
  refresh();

  return {
    suppressed: () => check.checked,
    suppressorId: () => {
      if (!check.checked || select.value === "" || select.value === NEW_CHOICE) return null;
      return Number(select.value);
    },
    needsSaving: () => check.checked && select.value === NEW_CHOICE,
  };
}
