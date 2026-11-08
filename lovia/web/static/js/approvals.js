// Approval cards: the words on them. The card itself lives in chat.js (it is
// woven into the bubble and the composer); what it says is decided here.

import { currentLang, t } from './i18n.js';
import { toolLabel } from './tool-views.js';

/**
 * The card's question, by the tool's own label ("Run command?"), or by its
 * name when it has none. The server's reason, when there is one, goes under.
 * @param {string} name
 */
export function approvalHeading(name) {
  const label = toolLabel(name);
  return label === name ? t('approval.askTool', { name }) : t('approval.ask', { label });
}

/**
 * A grant scope as a person reads it: the command for `shell:` (the card
 * already says it is about commands), "read ~/notes" for a path scope, and
 * anything else — a deployer's own kinds — verbatim.
 * @param {string} scope
 */
export function scopeLabel(scope) {
  const i = scope.indexOf(':');
  const kind = scope.slice(0, i);
  const what = scope.slice(i + 1);
  if (i > 0 && what) {
    if (kind === 'shell') return what;
    if (kind === 'read') return t('approval.scopeRead', { path: what });
    if (kind === 'write') return t('approval.scopeWrite', { path: what });
  }
  return scope;
}

/**
 * Which of a card's scopes "always allow" will remember. Nothing is picked
 * until the user says so; the checkbox picks all of them, and each chip can
 * then be left out — so the common case stays one click, and a broad scope
 * (`python`, `git`) need not ride along with harmless ones.
 * @param {string[]} scopes the request's own, in its order
 */
export function scopePick(scopes) {
  const picked = new Set();
  return {
    /** @returns {'none' | 'some' | 'all'} */
    state() {
      if (picked.size === 0) return 'none';
      return picked.size === scopes.length ? 'all' : 'some';
    },
    /** @param {string} scope */
    has: (scope) => picked.has(scope),
    /** @param {boolean} on */
    setAll(on) {
      picked.clear();
      if (on) scopes.forEach((s) => picked.add(s));
    },
    /**
     * Include or leave out one scope. From nothing picked, a chip picks just
     * itself: clicking the one you want is a choice too.
     * @param {string} scope
     */
    toggle(scope) {
      if (!scopes.includes(scope)) return;
      if (picked.has(scope)) picked.delete(scope);
      else picked.add(scope);
    },
    /** The picked scopes, in the request's order. */
    picked: () => scopes.filter((s) => picked.has(s)),
  };
}

/**
 * Split the model's own summary of the call (a string `description`
 * argument, which the shell tool asks for) from the arguments that act. The
 * card shows it apart, marked as the model's words: "read-only" in a
 * description is a claim nothing checked; the command is what runs.
 * @param {string | undefined} args the call's JSON arguments
 * @returns {{ note: string | null, rest: string | undefined }}
 */
export function splitNote(args) {
  let obj;
  try {
    obj = JSON.parse(args ?? '');
  } catch {
    return { note: null, rest: args };
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return { note: null, rest: args };
  const { description, ...rest } = obj;
  if (typeof description !== 'string' || !description.trim()) return { note: null, rest: args };
  return { note: description.trim(), rest: JSON.stringify(rest) };
}

/**
 * The card each pending call id was drawn as. A provider may reuse a call id
 * across turns, so the page can hold two cards under one id: a settling frame
 * is for the newest — never the first a selector happens to find, which would
 * rewrite an old card's record and leave the live one open.
 */
export function cardRegistry() {
  /** @type {Map<string, HTMLElement>} */
  const cards = new Map();
  return {
    /** @param {string} id @param {HTMLElement} node */
    add(id, node) {
      cards.set(id, node);
    },
    /**
     * The card a settling frame is for, forgotten once returned: null when
     * none is on the page (another chat is showing; a granted note).
     * @param {string} id
     */
    take(id) {
      const node = cards.get(id);
      cards.delete(id);
      return node?.isConnected ? node : null;
    },
  };
}

/**
 * Scopes as a sentence's list, in the page language ("pwd、uname和git",
 * "pwd, uname, and git").
 * @param {string[]} scopes
 */
export function scopeList(scopes) {
  const labels = scopes.map(scopeLabel);
  try {
    return new Intl.ListFormat(currentLang(), { type: 'conjunction' }).format(labels);
  } catch {
    return labels.join(', '); // a deployer language Intl does not know
  }
}

/** @typedef {'approve' | 'deny' | 'timeout' | 'cancel'} ApprovalOutcome */

/**
 * How an `approval_resolved` frame settles the card.
 * @param {{ approved: boolean, cause: 'user' | 'timeout' | 'cancel' }} data
 * @returns {ApprovalOutcome}
 */
export function approvalOutcome(data) {
  if (data.approved) return 'approve';
  return data.cause === 'user' ? 'deny' : data.cause;
}

/** @type {Record<ApprovalOutcome, string>} */
const OUTCOME_TEXT = {
  approve: 'approval.approved',
  deny: 'approval.denied',
  timeout: 'approval.timedOut',
  cancel: 'approval.cancelled',
};

/** @param {ApprovalOutcome} outcome */
export const outcomeText = (outcome) => t(OUTCOME_TEXT[outcome]);
