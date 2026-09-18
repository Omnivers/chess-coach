// fetch wrapper against the same-origin API, plus an SSE line reader for
// /coach/chat. Same origin: the page is served by FastAPI itself, so every
// path below is relative — never a hardcoded host:port.

const API = '';

export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

async function request(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(API + path, opts);
  } catch (networkErr) {
    throw new ApiError(`Network error contacting ${path}: ${networkErr.message}`, 0, null);
  }
  if (!res.ok) {
    let detail = null;
    try {
      detail = await res.json();
    } catch {
      // body wasn't JSON — fall back to the status text below.
    }
    const message = (detail && (detail.detail || detail.message)) || res.statusText || `HTTP ${res.status}`;
    throw new ApiError(message, res.status, detail);
  }
  if (res.status === 204) return null;
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

export const api = {
  get: (path) => request('GET', path),
  post: (path, body) => request('POST', path, body),
};

// Reads a text/event-stream body of `data: {...}` lines terminated by
// `data: [DONE]`, invoking onDelta(text) for each frame carrying a delta.
// Never uses innerHTML; callers render deltas via textContent.
export async function streamChat(messages, gameId, onDelta) {
  const res = await fetch(API + '/coach/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, game_id: gameId ?? null }),
  });
  if (!res.ok || !res.body) {
    let detail = null;
    try {
      detail = await res.json();
    } catch {
      // ignore
    }
    throw new ApiError((detail && detail.detail) || res.statusText || 'chat stream failed', res.status, detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith('data:')) continue;
      const payload = trimmed.slice(5).trim();
      if (payload === '[DONE]') return;
      try {
        const parsed = JSON.parse(payload);
        if (typeof parsed.delta === 'string') onDelta(parsed.delta);
      } catch {
        // malformed frame — skip rather than crash the stream.
      }
    }
  }
}
