/**
 * API client.
 *
 * Session cookies rather than a bearer token: the panel is served from the
 * same origin as the API, so there is nothing to store in JavaScript and
 * signing out actually ends the session on the server. Django wants the CSRF
 * token echoed back on writes, which is what readCookie handles.
 */

export class ApiError extends Error {
  status: number;
  /** Field name to message, when the server rejected specific inputs. */
  fields: Record<string, string[]>;
  /**
   * The parsed response body, untouched.
   *
   * Some failures carry a flag rather than a message -- a sign-in that needs a
   * second factor, say. Flattening those into a field map loses the flag, so
   * the original is kept for callers that need to branch on it.
   */
  body: unknown;

  constructor(
    status: number,
    message: string,
    fields: Record<string, string[]> = {},
    body: unknown = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.fields = fields;
    this.body = body;
  }
}

function readCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(^| )${name}=([^;]+)`));
  return match ? decodeURIComponent(match[2]) : null;
}

/** Turn a DRF error body into one sentence plus per-field detail. */
function parseError(status: number, body: unknown): ApiError {
  if (typeof body === "string" && body) return new ApiError(status, body, {}, body);

  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;

    if (typeof record.error === "string") {
      return new ApiError(status, record.error, {}, body);
    }
    if (typeof record.message === "string") {
      return new ApiError(status, record.message, {}, body);
    }
    if (typeof record.detail === "string") {
      return new ApiError(status, record.detail, {}, body);
    }

    // Anything left is a field-level rejection: one key per input, each with a
    // list of reasons. Booleans and flags are skipped so a message is chosen
    // from something a person can actually read.
    const fields: Record<string, string[]> = {};
    for (const [key, value] of Object.entries(record)) {
      if (typeof value === "boolean") continue;
      fields[key] = Array.isArray(value) ? value.map(String) : [String(value)];
    }
    const first = Object.entries(fields)[0];
    const message = first ? first[1][0] : "The server rejected that request.";
    return new ApiError(status, message, fields, body);
  }

  const fallback: Record<number, string> = {
    401: "Your session has ended. Sign in again.",
    403: "You do not have permission to do that.",
    404: "That no longer exists.",
    429: "Too many requests. Wait a moment and try again.",
    502: "The management API is not responding.",
    503: "The management API is not responding.",
  };
  return new ApiError(status, fallback[status] ?? `Request failed (${status}).`, {}, body);
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };

  if (body !== undefined) headers["Content-Type"] = "application/json";

  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const token = readCookie("csrftoken");
    if (token) headers["X-CSRFToken"] = token;
  }

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Cannot reach the management API. Is it running?");
  }

  // The panel forces enrollment before anything else is usable, so this is
  // handled centrally rather than at each call site.
  if (response.status === 403) {
    const clone = response.clone();
    try {
      const payload = await clone.json();
      if (payload?.totp_setup_required) {
        window.dispatchEvent(new CustomEvent("gateway:totp-setup-required"));
      }
    } catch {
      /* not a JSON body; fall through to normal handling */
    }
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let payload: unknown = text;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      /* leave it as text; parseError handles both */
    }
  }

  if (!response.ok) throw parseError(response.status, payload);
  return payload as T;
}

export const api = {
  get: <T,>(path: string) => request<T>("GET", path),
  post: <T,>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  patch: <T,>(path: string, body: unknown) => request<T>("PATCH", path, body),
  delete: (path: string) => request<void>("DELETE", path),
};

/** Fetch the CSRF cookie before the first write of a session. */
export function primeCsrf(): Promise<unknown> {
  return api.get("/api/v1/auth/csrf/").catch(() => null);
}
