/** Same-origin HTTP transport. The operator token exists only in this closure. */
export function createApiClient() {
  let token = "";
  let session = 0;
  const pending = new Set();

  function endSession() {
    token = "";
    session += 1;
    for (const controller of pending) controller.abort();
    pending.clear();
  }

  function beginSession(value) {
    endSession();
    token = value;
  }

  async function request(path, options = {}, binary = false) {
    const controller = new AbortController();
    const requestSession = session;
    pending.add(controller);
    const headers = { Authorization: `Bearer ${token}`, ...options.headers };
    let body = options.body;
    if (body && typeof body !== "string" && !(body instanceof Blob)) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    try {
      const response = await fetch(path, {
        ...options,
        body,
        headers,
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) {
        let error;
        try {
          error = await response.json();
        } catch {
          error = { detail: `HTTP ${response.status}` };
        }
        throw new Error(
          typeof error.detail === "string"
            ? error.detail
            : JSON.stringify(error.detail),
        );
      }
      const result =
        response.status === 204
          ? null
          : await (binary ? response.blob() : response.json());
      if (requestSession !== session) {
        throw new DOMException("Session ended", "AbortError");
      }
      return result;
    } finally {
      pending.delete(controller);
    }
  }

  return {
    beginSession,
    endSession,
    json: (path, options) => request(path, options),
    download: (path) => request(path, {}, true),
  };
}
