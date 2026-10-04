/**
 * Group page Context card: edit date, time, distance, position, suppressor and weather through
 * PATCH /api/groups/{id}. The distance field is disabled in the page while the group is part of a
 * zero. Every request goes through api.js; messages reach the page only as text.
 */
import { apiSend, ApiError } from "/static/js/api.js";
import { wireSuppressorPicker } from "/static/js/suppressor_picker.js";

const card = document.getElementById("context-card");
const root = document.querySelector('[data-page="group"]');
const flash = document.getElementById("flash");
const WEATHER_PROMPT_YD = 200;

function show(message) {
  flash.textContent = message;
  flash.classList.remove("ok");
  flash.hidden = false;
}

function value(id) {
  return document.getElementById(id).value.trim();
}

function numberOrNull(id) {
  const text = value(id);
  return text === "" ? null : Number(text);
}

function wire() {
  const groupId = Number(root.dataset.groupId);
  const suppressors = wireSuppressorPicker(card.querySelector("[data-suppressor-picker]"), show);
  const distance = document.getElementById("c-distance");
  const skip = document.getElementById("c-skip-weather");

  const warning = document.createElement("p");
  warning.id = "c-distance-warning";
  warning.className = "notice info";
  warning.hidden = true;
  warning.textContent = "Changing the distance recalculates every mil value of this group.";
  distance.closest(".field").append(warning);
  const weatherPanel = document.getElementById("c-weather");
  const weatherMissing = () =>
    Number(value("c-distance")) >= WEATHER_PROMPT_YD && !skip.checked && value("c-temp") === "";
  distance.addEventListener("input", () => {
    warning.hidden = value("c-distance") === distance.dataset.original;
    if (weatherMissing()) weatherPanel.open = true;
  });

  async function save() {
    if (suppressors.needsSaving()) {
      show("Save the new suppressor first, or choose one from the list.");
      return;
    }
    if (value("c-date") === "") {
      show("Enter the date of the group.");
      return;
    }
    if (weatherMissing()) {
      weatherPanel.open = true;
      show("Enter the temperature, or skip weather, for a group at this distance.");
      return;
    }
    flash.hidden = true;
    const time = value("c-time");
    const body = {
      shot_date: value("c-date"),
      shot_time: time === "" ? null : time,
      position: value("c-position"),
      suppressor: suppressors.suppressed(),
      suppressor_id: suppressors.suppressorId(),
      weather_skipped: skip.checked,
      temperature_f: skip.checked ? null : numberOrNull("c-temp"),
      density_altitude_ft: skip.checked ? null : numberOrNull("c-da"),
      wind_mph: skip.checked ? null : numberOrNull("c-wind"),
      wind_clock: skip.checked ? null : numberOrNull("c-clock"),
    };
    if (!distance.disabled && value("c-distance") !== distance.dataset.original) {
      body.distance_yd = Number(value("c-distance"));
    }
    try {
      await apiSend("PATCH", `/api/groups/${groupId}`, body);
      window.location.reload();
    } catch (error) {
      show(error instanceof ApiError ? error.detail : "Something went wrong");
    }
  }

  document.getElementById("c-save").addEventListener("click", save);
}

if (card && root) wire();
