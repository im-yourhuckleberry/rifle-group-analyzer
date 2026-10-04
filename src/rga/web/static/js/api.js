/**
 * Same-origin JSON fetch wrapper. Every page script calls the API through
 * apiGet / apiSend / apiUpload and never calls fetch directly.
 * Failures throw an ApiError carrying { detail, code, status }.
 */

export class ApiError extends Error {
  constructor(detail, code, status) {
    super(detail);
    this.name = "ApiError";
    this.detail = detail;
    this.code = code;
    this.status = status;
  }
}

function checkPath(path) {
  if (typeof path !== "string" || !path.startsWith("/") || path.startsWith("//")) {
    throw new ApiError("API path must be a same-site path starting with a single '/'", "bad_path", 0);
  }
}

async function errorFrom(response) {
  let detail = `Request failed (${response.status})`;
  let code = `http_${response.status}`;
  try {
    const body = await response.json();
    if (body && typeof body.detail === "string") detail = body.detail;
    else if (body && Array.isArray(body.detail)) detail = body.detail.map((d) => d.msg).join("; ");
    if (body && typeof body.code === "string") code = body.code;
  } catch (_ignored) {
    // Non-JSON error body: keep the status-based defaults.
  }
  return new ApiError(detail, code, response.status);
}

async function request(path, init) {
  checkPath(path);
  let response;
  try {
    response = await fetch(path, { credentials: "same-origin", ...init });
  } catch (_networkFailure) {
    throw new ApiError("Could not reach the server", "network_error", 0);
  }
  if (!response.ok) throw await errorFrom(response);
  if (response.status === 204) return null;
  try {
    return await response.json();
  } catch (_badJson) {
    throw new ApiError("Server returned a malformed response", "bad_response", response.status);
  }
}

/** GET `path` and resolve with the parsed JSON body. */
export function apiGet(path) {
  return request(path, { method: "GET", headers: { Accept: "application/json" } });
}

/** Send `body` as JSON with `method` (POST, PUT, PATCH, DELETE); resolve with the parsed JSON body. */
export function apiSend(method, path, body) {
  const init = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  return request(path, init);
}

/** POST `file` to `path` as multipart/form-data (field name "file"); resolve with the parsed JSON body. */
export function apiUpload(path, file) {
  const form = new FormData();
  form.append("file", file);
  return request(path, { method: "POST", headers: { Accept: "application/json" }, body: form });
}
