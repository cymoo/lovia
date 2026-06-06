// Shared reactive store for the lovia chat UI.
const _listeners = new Map();

export const store = {
  sessionId: null,
  agent: null,
  agents: [],
  sessions: [],
  streaming: false,
  bubble: null,
  body: null,
  rawText: "",
  toolNodes: new Map(),
  reasoningText: "",
  reasoningNode: null,
  reasoningStart: 0,
  reasoningEnd: 0,
  lastMessage: null,
  theme: localStorage.getItem("lovia-theme") || "light",

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
  }
};
