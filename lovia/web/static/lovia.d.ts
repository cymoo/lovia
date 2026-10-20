// lovia.d.ts — types for `window.lovia`, the chat page's public extension API
// (web-server.md, "Extension scripts"). In an extension script:
//
//   // @ts-check
//   /// <reference path="<site-packages>/lovia/web/static/lovia.d.ts" />
//
// `python -c "import lovia.web, pathlib; print(pathlib.Path(lovia.web.__file__).parent / 'static/lovia.d.ts')"`
// prints the path. tsc checks the implementation (js/lovia.js) against these
// declarations, so they cannot drift apart. No top-level import or export:
// this is a global declaration file.

interface LoviaState {
  /** The chat on screen; null for a new chat not saved yet. */
  readonly sessionId: string | null;
  /** The agent new messages go to. */
  readonly agent: string | null;
  readonly theme: 'light' | 'dark';
  /** The UI's language code: 'en', 'zh', or one added via ChatUI.messages. */
  readonly lang: string;
  /** A run is streaming into the view. */
  readonly busy: boolean;
}

/** `lovia.on` event names and what each listener receives. */
interface LoviaEvents {
  /** Another chat is on screen (null: a new one); also when a new chat gets saved. */
  session: { sessionId: string | null };
  agent: { agent: string | null };
  /** A run began streaming into the view: sent, or reattached after a reload. */
  'run-start': { sessionId: string | null };
  /**
   * The view stopped streaming it: `done` (answered), `error`, `cancelled`
   * (stopped by the user), or `detached` (the user switched chats, or the
   * connection dropped — the run may go on server-side).
   */
  'run-end': {
    sessionId: string | null;
    status: 'done' | 'error' | 'cancelled' | 'detached';
    /** The last answered leg's token usage, when the run answered. */
    usage: Record<string, number> | null;
  };
  theme: { theme: 'light' | 'dark' };
}

/** A message in the chat on screen, as a message action sees it. */
interface MessageRef {
  readonly sessionId: string | null;
  /**
   * The user message's id, `"<run_id>:<n>"`, stable across reloads and
   * rewinds; a reply carries the ref of the user message it answers. null
   * until the server names the message (when its run finishes), or when the
   * chat's store keeps no checkpoints.
   */
  readonly ref: string | null;
  readonly role: 'user' | 'assistant';
  /** The message as Markdown (what copy puts on the clipboard). */
  readonly text: string;
}

/** A button in a message's footer, beside copy. */
interface MessageAction {
  /** The tooltip and accessible name. */
  label: string;
  /** A built-in icon name, SVG markup, or text such as an emoji; the label when absent. */
  icon?: string;
  /** Whether `msg` shows the action (default: every message), e.g. `m => m.ref !== null`. */
  when?(msg: MessageRef): boolean;
  /** Draw the button pressed (`aria-pressed`), e.g. a rating already given. */
  pressed?(msg: MessageRef): boolean;
  /** Called on a click; the button is disabled until it settles. A thrown error is toasted. */
  run(msg: MessageRef): void | Promise<void>;
}

interface LoviaApi {
  /** The lovia version serving the page, for feature checks. */
  readonly version: string;
  /**
   * Settles once the app's start is over: the page is wired up, and the first
   * chat-list request and the chat in the URL have loaded or failed. Pending
   * if the page finds the user signed out. Don't await it at a script's top
   * level: the page waits for your script before it starts.
   */
  readonly ready: Promise<void>;
  /** A snapshot of the page's state; read it again for the current one. */
  readonly state: LoviaState;
  /** Listen for an event; returns a function that stops listening. */
  on<E extends keyof LoviaEvents>(event: E, fn: (payload: LoviaEvents[E]) => void): () => void;

  /** Draw the UI component `kind` that tools return with `ui_result(kind, …)`. */
  registerRenderer: typeof import('./js/components.js').registerRenderer;
  /** The renderer registered for `kind`, e.g. to wrap a built-in one. */
  getRenderer: typeof import('./js/components.js').getRenderer;
  /** Change how tool `name`'s cards (or, with '*', every other tool's) read. */
  registerToolView: typeof import('./js/tool-views.js').registerToolView;
  /** Add a button to messages' footers, or replace the one under `id`; null removes it. */
  registerMessageAction(id: string, action: MessageAction | null): void;
  /** Draw every message's actions again, after the state their `when` or `pressed` read changed. */
  refreshMessageActions(): void;
  /**
   * Add `key` to the client context each run starts with: `fn()` is read when
   * a message (or a new schedule) is sent, and reaches the server's
   * `create_app(run_context=...)` as `trigger.client[key]`. Return
   * JSON-serializable data; undefined leaves the key out. UI state only — the
   * browser controls it, so it is no proof of who the user is.
   */
  provideContext(key: string, fn: () => unknown): void;

  /** Send `text` as the user (queued for the next turn while a run streams). */
  send(text: string): void;
  /** Put `text` in the composer for the user to edit and send. */
  setDraft(text: string): void;
  /** Show a new, empty chat. */
  newChat(): void;
  /** Show chat `id`. */
  openSession(id: string): Promise<void>;
  /** A transient notification. */
  toast(text: string, opts?: { type?: 'info' | 'error' }): void;

  /** The page's API client: the mount prefix and sign-in handling built in. */
  readonly api: typeof import('./js/api.js').api;
  /** `fetch` for your own routes: `path` (starting with '/') gets the mount prefix, and a 401 the page's sign-in handling. */
  fetch(path: string, init?: RequestInit): Promise<Response>;
  /** A UI string in the page's language (`{name}` placeholders from `params`). */
  t(key: string, params?: Record<string, string | number>): string;
}

interface Window {
  /** The chat page's public extension API. */
  lovia: LoviaApi;
}
