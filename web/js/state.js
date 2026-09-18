// Single store. Views subscribe and read; nothing reaches into another
// module's DOM. set() shallow-merges a patch and notifies subscribers with
// the full new state.

function initialState() {
  return {
    route: { name: null, params: {} },
    theme: readStoredTheme(),
    play: {
      gameId: null,
      chess: null,
      fen: null,
      orientation: 'white',
      userColor: 'white',
      isUserTurn: false,
      terminated: false,
      result: null,
      clock: null,
      hintCredits: 6,
      moveHistorySan: [],
      lastFeedback: null,
      guard: null, // { uci, deltaCp } when an inline confirm is pending
      thinking: false,
      error: null,
    },
    review: { externalId: null, data: null, ply: 0, summary: null, summaryLoading: false },
    drills: { queue: [], current: null, feedback: null, loading: false },
    study: { session: null, profile: null, loading: false },
    chat: { available: null, reason: '', messages: [], streaming: false, collapsed: window.innerWidth < 768 },
  };
}

function readStoredTheme() {
  try {
    return localStorage.getItem('chess-coach-theme');
  } catch {
    return null;
  }
}

export function storeTheme(theme) {
  try {
    if (theme) localStorage.setItem('chess-coach-theme', theme);
    else localStorage.removeItem('chess-coach-theme');
  } catch {
    // storage may be unavailable (private mode, disabled) — theme just
    // won't persist across reloads.
  }
}

let state = initialState();
const listeners = new Set();

export function getState() {
  return state;
}

// Shallow patch at the top level; nested slices should be replaced wholesale
// by the caller (e.g. set({ play: { ...getState().play, fen } })).
export function setState(patch) {
  state = { ...state, ...patch };
  for (const fn of listeners) fn(state);
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
