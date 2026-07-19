// Shared reactive store for the lovia chat UI.
const _listeners = new Map();

export const store = {
  sessionId: null,
  agent: null,
  agents: [],
  sessions: [],
  activeRuns: new Set(), // session ids with a live background run (sidebar dot)
  chatEpoch: 0,
  // Mirrors the server defaults in web/ui.py — used only if app-config is
  // missing or unparsable.
  emptyTitle: "Where shall we begin?",
  emptyDescription: "A good question is already half the answer.",
  emptyExamples: [],
  sidebarCollapsed: localStorage.getItem("lovia-sidebar-collapsed") === "1",
  streaming: false,
  turnNode: null,
  bubble: null,
  body: null,
  rawText: "",
  toolNodes: new Map(),
  reasoningText: "",
  reasoningNode: null,
  reasoningStart: 0,
  reasoningEnd: 0,
  todoNode: null,
  // On phones the panel is bottom-anchored and would cover the conversation
  // (and approval buttons) — start it collapsed there; the toggle still works.
  // Guarded like the theme detection below.
  todoCollapsed: !!(
    window.matchMedia && window.matchMedia("(max-width: 720px)").matches
  ),
  todos: [],
  lastMessage: null,
  // Set when the current run created a brand-new session whose title is being
  // generated server-side, so the chat view knows to poll for it.
  titlePending: false,
  // Saved preference wins; otherwise follow the OS on first load.
  theme:
    localStorage.getItem("lovia-theme") ||
    (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light"),

  on(event, fn) {
    if (!_listeners.has(event)) _listeners.set(event, []);
    _listeners.get(event).push(fn);
    return () => {
      const arr = _listeners.get(event);
      if (arr) {
        const i = arr.indexOf(fn);
        if (i >= 0) arr.splice(i, 1);
      }
    };
  },

  emit(event, data) {
    const arr = _listeners.get(event);
    if (arr) arr.forEach(fn => fn(data));
  },

  syncURL(sessionId) {
    const url = new URL(window.location);
    if (sessionId) {
      url.searchParams.set('session', sessionId);
    } else {
      url.searchParams.delete('session');
    }
    window.history.replaceState({}, '', url);
  },

  readSessionFromURL() {
    const params = new URLSearchParams(window.location.search);
    return params.get('session');
  },
};
