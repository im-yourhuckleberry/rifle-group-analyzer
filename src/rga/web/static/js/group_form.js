/**
 * Rifle page groups: the "Add a group" form and the per-row photo actions. A chosen photo is
 * uploaded at once and fills in the date and time it was taken ("from photo"); weather opens once
 * the distance reaches the prompt distance and the group cannot be added until it is entered or
 * skipped. Every request goes through api.js; user text reaches the page only as text.
 */
import { apiSend, apiUpload, ApiError } from "/static/js/api.js";
import { wireSuppressorPicker } from "/static/js/suppressor_picker.js";

const card = document.getElementById("new-group-card");
const flash = document.getElementById("flash");
const state = card ? JSON.parse(card.dataset.state) : null;
const returnTo = state ? encodeURIComponent(`/rifles/${state.rifle_id}`) : "";

function show(message, kind) {
  flash.textContent = message;
  flash.classList.toggle("ok", kind === "ok");
  flash.hidden = false;
}

function describe(error) {
  if (error instanceof ApiError) {
    return error.code === "busy" ? `${error.detail} (try again)` : error.detail;
  }
  return "Something went wrong";
}

async function run(action) {
  flash.hidden = true;
  try {
    return await action();
  } catch (error) {
    show(describe(error));
    return undefined;
  }
}

function value(id) {
  return document.getElementById(id).value.trim();
}

function numberOrNull(id) {
  const text = value(id);
  return text === "" ? null : Number(text);
}

function nextPageFor(groupId, photoId, confirmed) {
  if (confirmed) return `/groups/${groupId}/review?return_to=${returnTo}`;
  return `/photos/${photoId}/reference?group_id=${groupId}&return_to=${returnTo}`;
}

async function attachPhoto(groupId, photoId, confirmed) {
  const patched = await run(() => apiSend("PATCH", `/api/groups/${groupId}`, { photo_id: photoId }));
  if (patched) window.location.href = nextPageFor(groupId, photoId, confirmed);
}

async function importPhoto(input) {
  const file = input.files[0];
  if (!file) return;
  const photo = await run(() => apiUpload("/api/photos", file));
  if (photo) await attachPhoto(Number(input.dataset.groupId), photo.id, photo.reference_confirmed);
}

async function useExisting(button) {
  const groupId = Number(button.dataset.groupId);
  const select = document.querySelector(`.js-existing[data-group-id="${groupId}"]`);
  if (!select || select.value === "") {
    show("Choose which group's photo to use.");
    return;
  }
  const photoId = Number(select.value);
  await attachPhoto(groupId, photoId, Boolean(state.photo_confirmed[String(photoId)]));
}

for (const input of document.querySelectorAll(".js-import")) {
  input.addEventListener("change", () => importPhoto(input));
}
for (const button of document.querySelectorAll(".js-use-existing")) {
  button.addEventListener("click", () => useExisting(button));
}

function wireGroupForm() {
  const suppressors = wireSuppressorPicker(card.querySelector("[data-suppressor-picker]"), show);
  const weatherIds = ["w-temp", "w-da", "w-wind", "w-clock"];
  let photo = null;
  let weatherSkipped = false;
  let weatherPrefilled = false;

  const dateSource = document.getElementById("g-date-source");
  const panel = document.getElementById("weather-panel");

  function weatherNeeded() {
    const distance = Number(value("g-distance"));
    return Number.isFinite(distance) && distance >= state.weather_prompt_yd;
  }

  function weatherEntered() {
    return weatherSkipped || value("w-temp") !== "";
  }

  function refreshGate() {
    const needed = weatherNeeded();
    const blocked = needed && !weatherEntered();
    document.getElementById("weather-required").hidden = !needed;
    document.getElementById("weather-optional").hidden = needed;
    document.getElementById("weather-notice").hidden = !blocked;
    document.getElementById("weather-skipped-badge").hidden = !weatherSkipped;
    document.getElementById("group-blocked").hidden = !blocked;
    document.getElementById("add-group").disabled = blocked;
    if (needed && !weatherEntered()) panel.open = true;
  }

  function setWeather(source) {
    const fields = {
      "w-temp": source.temperature_f,
      "w-da": source.density_altitude_ft,
      "w-wind": source.wind_mph,
      "w-clock": source.wind_clock,
    };
    for (const [id, field] of Object.entries(fields)) {
      document.getElementById(id).value = field === null || field === undefined ? "" : String(field);
    }
    weatherSkipped = Boolean(source.weather_skipped);
  }

  function syncWeatherToDate() {
    const defaults = state.defaults;
    const sameDay = defaults.source_group_id !== null && defaults.shot_date === value("g-date");
    if (sameDay && !weatherEntered() && !weatherPrefilled) {
      setWeather(defaults);
      weatherPrefilled = true;
    } else if (!sameDay && weatherPrefilled) {
      setWeather({});
      weatherPrefilled = false;
    }
    refreshGate();
  }

  function markEdited() {
    weatherPrefilled = false;
  }

  async function chosePhoto() {
    const file = document.getElementById("g-photo").files[0];
    photo = null;
    if (!file) {
      dateSource.textContent = "entered";
      return;
    }
    const uploaded = await run(() => apiUpload("/api/photos", file));
    if (!uploaded) return;
    photo = uploaded;
    if (uploaded.taken_at) {
      const [day, time] = uploaded.taken_at.split("T");
      document.getElementById("g-date").value = day;
      document.getElementById("g-time").value = time.slice(0, 5);
      dateSource.textContent = "from photo";
      syncWeatherToDate();
    } else {
      dateSource.textContent = "entered (the photo has no date)";
    }
  }

  function photoTimeUnchanged() {
    if (!photo || !photo.taken_at) return false;
    const [day, time] = photo.taken_at.split("T");
    return value("g-date") === day && value("g-time") === time.slice(0, 5);
  }

  async function addGroup() {
    const lotCode = value("g-lot");
    const shots = Number(value("g-shots"));
    if (lotCode === "") {
      show("Enter the lot or batch code of the ammunition.");
      return;
    }
    if (!Number.isInteger(shots) || shots < 1) {
      show("Shots fired must be a whole number of at least 1.");
      return;
    }
    if (value("g-date") === "") {
      show("Enter the date of the group.");
      return;
    }
    if (suppressors.needsSaving()) {
      show("Save the new suppressor first, or choose one from the list.");
      return;
    }
    const time = value("g-time");
    const fromPhoto = photoTimeUnchanged();
    const exactTime = fromPhoto ? photo.taken_at.split("T")[1] : time;
    const body = {
      load_id: Number(value("g-load")),
      lot_code: lotCode,
      shots_expected: shots,
      shot_date: value("g-date"),
      shot_time: exactTime === "" ? null : exactTime,
      utc_offset: fromPhoto ? photo.taken_at_offset : null,
      distance_yd: Number(value("g-distance")),
      position: value("g-position"),
      suppressor: suppressors.suppressed(),
      suppressor_id: suppressors.suppressorId(),
      elevation_dialed_mil: numberOrNull("g-elevation"),
      purpose: "general",
      photo_id: photo ? photo.id : null,
      notes: document.getElementById("g-notes").value,
    };
    if (weatherSkipped) {
      body.weather_skipped = true;
    } else {
      body.temperature_f = numberOrNull("w-temp");
      body.density_altitude_ft = numberOrNull("w-da");
      body.wind_mph = numberOrNull("w-wind");
      body.wind_clock = numberOrNull("w-clock");
    }
    const group = await run(() => apiSend("POST", "/api/groups", body));
    if (!group) return;
    if (photo) window.location.href = nextPageFor(group.id, photo.id, photo.reference_confirmed);
    else window.location.reload();
  }

  function refreshLotOptions() {
    const list = document.getElementById("g-lot-options");
    list.replaceChildren();
    for (const code of state.lots[document.getElementById("g-load").value] || []) {
      const option = document.createElement("option");
      option.value = code;
      list.append(option);
    }
  }

  document.getElementById("g-photo").addEventListener("change", chosePhoto);
  document.getElementById("g-date").addEventListener("input", () => {
    dateSource.textContent = "entered";
    syncWeatherToDate();
  });
  document.getElementById("g-time").addEventListener("input", () => {
    if (!photoTimeUnchanged()) dateSource.textContent = "entered";
  });
  document.getElementById("g-distance").addEventListener("input", refreshGate);
  for (const id of weatherIds) {
    document.getElementById(id).addEventListener("input", () => {
      weatherSkipped = false;
      markEdited();
      refreshGate();
    });
  }
  document.getElementById("skip-weather").addEventListener("click", () => {
    setWeather({ weather_skipped: true });
    weatherPrefilled = false;
    refreshGate();
  });
  document.getElementById("g-load").addEventListener("change", refreshLotOptions);
  document.getElementById("add-group").addEventListener("click", addGroup);
  refreshLotOptions();
  syncWeatherToDate();
}

if (card && document.getElementById("add-group")) wireGroupForm();
