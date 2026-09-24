// types.d.ts — hand-written ambient types for the browser globals the bundled
// web UI leans on, so `checkJs` (see jsconfig.json) can type-check the code
// WITHOUT pulling any npm packages (no package.json, no node_modules).
//
// The markdown / sanitizer / highlighter / diagram libraries arrive from CDN
// <script> tags (templates/index.html) and expose themselves as globals. Only
// the handful of members the UI actually calls are declared. Each may be absent
// at runtime (offline, blocked CDN, SRI mismatch); every call site guards with
// `typeof X !== 'undefined'`, so they are declared as possibly-undefined.
//
// This file has no import/export on purpose: that keeps it a *global* script so
// the `interface Error`/`interface Window` blocks merge into the built-ins.

/** Minimal surface of marked (https://marked.js.org) used by the UI. */
interface MarkedToken {
  type: string;
  raw: string;
}
interface MarkedStatic {
  parse(src: string): string;
  setOptions(opts: { gfm?: boolean; breaks?: boolean }): void;
  defaults: object;
  /** Block + inline lex; the result carries the `links` a Parser needs. */
  lexer(src: string): MarkedToken[];
  Lexer: new (options?: object) => {
    blockTokens(src: string, tokens: MarkedToken[]): MarkedToken[];
    /** Link-reference definitions the lexer collected (keyed by label). */
    tokens: { links: Record<string, object> };
  };
  /** One instance de-duplicates heading ids across successive parse() calls. */
  Parser: new (options?: object) => {
    parse(tokens: MarkedToken[]): string;
    slugger: { seen: Record<string, number> };
  };
}

/** Minimal surface of DOMPurify (https://github.com/cure53/DOMPurify). */
interface DOMPurifyStatic {
  sanitize(dirty: string): string;
}

/** Minimal surface of highlight.js (https://highlightjs.org). */
interface HLJSStatic {
  highlightElement(el: Element): void;
}

/** One rendered diagram from mermaid's async `render`. */
interface MermaidRenderResult {
  svg: string;
  bindFunctions?: (element: Element) => void;
}

/** Minimal surface of mermaid (https://mermaid.js.org). */
interface MermaidStatic {
  initialize(config: Record<string, unknown>): void;
  /**
   * Validate diagram source. With `suppressErrors`, resolves to `false` for
   * invalid/incomplete input (e.g. a half-streamed fenced block) instead of
   * rejecting.
   */
  parse(text: string, opts?: { suppressErrors?: boolean }): Promise<boolean>;
  render(id: string, text: string): Promise<MermaidRenderResult>;
}

// The CDN globals. `var` (not `const`/`let`) is required for an ambient global
// declaration; `| undefined` models "the <script> may not have loaded".
declare var marked: MarkedStatic | undefined;
declare var DOMPurify: DOMPurifyStatic | undefined;
declare var hljs: HLJSStatic | undefined;
declare var mermaid: MermaidStatic | undefined;

interface Window {
  /** Safari's prefixed AudioContext — probed as a fallback in settings.js. */
  webkitAudioContext?: typeof AudioContext;
}

interface Error {
  /**
   * HTTP status code attached by api.js's response helpers so callers can
   * branch on it (e.g. 401 → prompt for a token, 404 → not-found copy).
   */
  status?: number;
  /** The API's machine-readable error code (see lovia/web/errors.py). */
  code?: string;
  /** The API's suggested fix, when it sent one (also folded into `message`). */
  hint?: string;
}
