// Approval cards: the words on them. The card itself lives in chat.js (it is
// woven into the bubble and the composer); what it says is decided here.

import { t } from './i18n.js';
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
