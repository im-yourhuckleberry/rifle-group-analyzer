/**
 * Page script for /rifles and /rifles/{id}: every write goes through api.js, errors go to #flash
 * as text, and a successful write reloads the page or moves to the created record.
 */

import { apiGet, apiSend } from "/static/js/api.js";

function showError(error) {
  const flash = document.getElementById("flash");
  const message = error && error.detail ? error.detail : error && error.message ? error.message : "Something went wrong";
  if (!flash) return;
  flash.classList.remove("ok");
  flash.textContent = message;
  flash.hidden = false;
}

function clearError() {
  const flash = document.getElementById("flash");
  if (flash) {
    flash.textContent = "";
    flash.hidden = true;
  }
}

function control(form, name) {
  return form.elements.namedItem(name);
}

function textValue(form, name) {
  const el = control(form, name);
  if (!el) return null;
  const value = el.value.trim();
  return value === "" ? null : value;
}

function numberValue(form, name, label) {
  const raw = textValue(form, name);
  if (raw === null) return null;
  const value = Number(raw);
  if (!Number.isFinite(value)) throw new Error(`${label || name} must be a number`);
  return value;
}

function onSubmit(form, handler) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const buttons = form.querySelectorAll("button");
    buttons.forEach((b) => {
      b.disabled = true;
    });
    clearError();
    try {
      await handler(form);
    } catch (error) {
      showError(error);
      buttons.forEach((b) => {
        b.disabled = false;
      });
    }
  });
}

function reload() {
  window.location.reload();
}

function normalized(name) {
  return name.trim().toLowerCase();
}

/** Select the option for `inches` in the diameter dropdown, adding one if it is not listed. */
function selectDiameter(select, inches) {
  let option = Array.from(select.options).find((o) => o.value !== "" && Number(o.value) === inches);
  if (!option) {
    option = document.createElement("option");
    option.value = String(inches);
    option.textContent = `${inches.toFixed(3)} in`;
    select.appendChild(option);
  }
  select.value = option.value;
}

async function wireCartridgePrefill(form) {
  const cartridge = control(form, "cartridge");
  const diameter = control(form, "bullet_diameter_in");
  const list = document.getElementById("cartridge-list");
  if (!cartridge || !diameter) return;
  let calibers = [];
  try {
    calibers = await apiGet("/api/calibers");
  } catch (error) {
    showError(error);
    return;
  }
  const byName = new Map(calibers.map((c) => [normalized(c.cartridge), c.bullet_diameter_in]));
  if (list) {
    calibers.forEach((c) => {
      const option = document.createElement("option");
      option.value = c.cartridge;
      list.appendChild(option);
    });
  }
  let edited = diameter.value !== "" && !byName.has(normalized(cartridge.value));
  diameter.addEventListener("change", () => {
    edited = true;
  });
  const prefill = () => {
    const known = byName.get(normalized(cartridge.value));
    if (known !== undefined && !edited) selectDiameter(diameter, known);
  };
  cartridge.addEventListener("input", prefill);
  cartridge.addEventListener("change", () => {
    edited = false;
    prefill();
  });
}

function rifleBody(form, existing) {
  const body = {
    name: textValue(form, "name"),
    cartridge: textValue(form, "cartridge"),
    scope: textValue(form, "scope"),
    notes: control(form, "notes").value,
  };
  const diameter = numberValue(form, "bullet_diameter_in", "Bullet diameter");
  if (diameter !== null) body.bullet_diameter_in = diameter;
  if (!existing && body.scope === null) delete body.scope;
  return body;
}

function wireNewRifle(form) {
  wireCartridgePrefill(form);
  onSubmit(form, async () => {
    const rifle = await apiSend("POST", "/api/rifles", rifleBody(form, false));
    window.location.assign(`/rifles/${rifle.id}`);
  });
}

function wireEditRifle(form, rifleId) {
  wireCartridgePrefill(form);
  onSubmit(form, async () => {
    await apiSend("PATCH", `/api/rifles/${rifleId}`, rifleBody(form, true));
    reload();
  });
}

function wireZeroLoad(form, rifleId) {
  onSubmit(form, async () => {
    const raw = control(form, "load_id").value;
    await apiSend("PUT", `/api/rifles/${rifleId}/zero-load`, {
      load_id: raw === "" ? null : Number(raw),
    });
    reload();
  });
}

const FACTORY_TEXT = ["maker", "product", "bullet_type"];
const HANDLOAD_TEXT = ["bullet", "primer", "brass"];
const NEW_HANDLOAD_WEIGHT = "handload_bullet_weight_gr";

function kindFields(kind) {
  return kind === "factory"
    ? { text: FACTORY_TEXT, number: ["bullet_weight_gr"] }
    : { text: HANDLOAD_TEXT, number: ["bullet_weight_gr", "coal_in"] };
}

function loadBody(form, kind) {
  const fields = kindFields(kind);
  const body = { notes: control(form, "notes").value };
  const label = textValue(form, "label");
  if (label !== null) body.label = label;
  fields.text.forEach((name) => {
    body[name] = textValue(form, name);
  });
  fields.number.forEach((name) => {
    const source = kind === "handload" && name === "bullet_weight_gr" && control(form, NEW_HANDLOAD_WEIGHT) ? NEW_HANDLOAD_WEIGHT : name;
    body[name] = numberValue(form, source, name === "coal_in" ? "COAL" : "Bullet weight");
  });
  body.trued_mv_fps = numberValue(form, "trued_mv_fps", "Trued muzzle velocity");
  return body;
}

function wireNewLoad(form, rifleId) {
  const kind = control(form, "kind");
  const sync = () => {
    form.querySelectorAll("[data-kind-fields]").forEach((box) => {
      box.hidden = box.dataset.kindFields !== kind.value;
    });
  };
  kind.addEventListener("change", sync);
  sync();
  onSubmit(form, async () => {
    const body = loadBody(form, kind.value);
    body.kind = kind.value;
    if (body.label === undefined && kind.value === "handload") {
      const parts = [body.bullet_weight_gr === null ? null : `${body.bullet_weight_gr} gr`, body.bullet];
      const named = parts.filter((part) => part).join(" ");
      if (named !== "") body.label = named;
    }
    if (body.label === undefined) throw new Error("A label is required");
    await apiSend("POST", `/api/rifles/${rifleId}/loads`, body);
    reload();
  });
}

function wireEditLoad(form) {
  onSubmit(form, async () => {
    await apiSend("PATCH", `/api/loads/${form.dataset.loadId}`, loadBody(form, form.dataset.kind));
    reload();
  });
}

function wireNewLot(form) {
  onSubmit(form, async () => {
    const code = textValue(form, "lot_code");
    if (code === null) throw new Error("Enter a lot number or batch ID");
    await apiSend("POST", `/api/loads/${form.dataset.loadId}/lots`, { lot_code: code });
    reload();
  });
}

function wireNewZero(form, rifleId) {
  onSubmit(form, async () => {
    const body = {
      rifle_id: rifleId,
      load_id: Number(control(form, "load_id").value),
      mode: control(form, "mode").value,
    };
    const lot = textValue(form, "lot_code");
    if (lot !== null) body.lot_code = lot;
    const zero = await apiSend("POST", "/api/zeros", body);
    window.location.assign(`/zeroing/${zero.id}`);
  });
}

function init() {
  const root = document.querySelector("[data-page]");
  if (!root) return;
  const page = root.dataset.page;
  if (page === "rifles-list") {
    wireNewRifle(root.querySelector('[data-form="new-rifle"]'));
    return;
  }
  if (page !== "rifle-detail") return;
  const rifleId = Number(root.dataset.rifleId);
  wireEditRifle(root.querySelector('[data-form="edit-rifle"]'), rifleId);
  const zero = root.querySelector('[data-form="zero-load"]');
  if (zero) wireZeroLoad(zero, rifleId);
  wireNewLoad(root.querySelector('[data-form="new-load"]'), rifleId);
  root.querySelectorAll('[data-form="edit-load"]').forEach(wireEditLoad);
  root.querySelectorAll('[data-form="new-lot"]').forEach(wireNewLot);
  const newZero = root.querySelector('[data-form="new-zero"]');
  if (newZero) wireNewZero(newZero, rifleId);
}

init();
