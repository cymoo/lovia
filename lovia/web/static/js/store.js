// Shared reactive store for the lovia chat UI.
const _listeners = new Map();

export const store = {
  sessionId: null,
  agent: null,
  agents: [],
  sessions: [],
  chatEpoch: 0,
  emptyTitle: "Wake up, Neo.",
  emptyDescription: "The Matrix has you.",
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
  todoCollapsed: false,
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
