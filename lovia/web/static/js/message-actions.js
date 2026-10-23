// message-actions.js — deployer actions in a message's footer, beside copy
// (lovia.registerMessageAction). The registry and the buttons live here;
// chat.js knows the messages: it describes one (MessageRef) and re-renders
// footers when watchMessageActions says the actions changed.

import { ICONS, icon } from './icons.js';
import { Registry } from './registry.js';
import { toast } from './toast.js';

const FIELDS = new Set(['label', 'icon', 'when', 'pressed', 'run']);

/** @type {Registry<MessageAction>} */
const actions = new Registry();
/** @type {Set<() => void>} */
const watchers = new Set();

/**
 * Add an action to the messages' footers, or replace the one under `id`.
 * @param {string} id
 * @param {MessageAction} action
 * @returns {() => void} Removes it, bringing back the action it replaced.
 */
export function registerMessageAction(id, action) {
  if (typeof id !== 'string' || !id) {
    throw new TypeError('lovia.registerMessageAction: expected an id');
  }
  if (!action || typeof action !== 'object') {
    throw new TypeError(`lovia.registerMessageAction(${JSON.stringify(id)}): expected an action`);
  }
  for (const key of Object.keys(action)) {
    if (!FIELDS.has(key)) {
      throw new TypeError(`lovia.registerMessageAction: unknown field ${JSON.stringify(key)}`);
    }
  }
  if (typeof action.label !== 'string' || !action.label) {
    throw new TypeError('lovia.registerMessageAction: `label` must be a non-empty string');
  }
  if (action.icon !== undefined && typeof action.icon !== 'string') {
    throw new TypeError('lovia.registerMessageAction: `icon` must be a string');
  }
  for (const key of /** @type {const} */ (['when', 'pressed', 'run'])) {
    if (action[key] !== undefined && typeof action[key] !== 'function') {
      throw new TypeError(`lovia.registerMessageAction: \`${key}\` must be a function`);
    }
  }
  if (!action.run) throw new TypeError('lovia.registerMessageAction: `run` is required');
  const dispose = actions.add(id, Object.freeze({ ...action }), refreshMessageActions);
  refreshMessageActions();
  return dispose;
}

/** Draw every message's actions again — `when` and `pressed` are re-asked. */
export function refreshMessageActions() {
  for (const fn of watchers) fn();
}

/** @param {() => void} fn Called whenever the actions need drawing again. */
export function watchMessageActions(fn) {
  watchers.add(fn);
}

/**
 * Ask one of an action's predicates; one that throws counts as false.
 * @param {string} id
 * @param {((msg: MessageRef) => boolean) | undefined} fn
 * @param {MessageRef} msg
 * @param {boolean} fallback The answer when the action leaves it out.
 */
function ask(id, fn, msg, fallback) {
  if (!fn) return fallback;
  try {
    return !!fn(msg);
  } catch (err) {
    console.error(`lovia: message action "${id}" failed:`, err);
    return false;
  }
}

/**
 * A built-in icon name, SVG markup (from the deployer's own script), or text
 * such as an emoji; the label when there is no icon.
 * @param {HTMLElement} el
 * @param {MessageAction} action
 */
function setGlyph(el, action) {
  const glyph = action.icon ?? '';
  if (Object.hasOwn(ICONS, glyph)) el.innerHTML = icon(glyph, { size: 13 });
  else if (glyph.trimStart().startsWith('<svg')) el.innerHTML = glyph;
  else {
    el.textContent = glyph || action.label;
    el.classList.add('msg-action--text');
  }
}

/**
 * The actions still running, per message: a redraw (a ref arriving, a
 * refresh) replaces the buttons, and must not hand back an enabled one.
 * @type {WeakMap<object, Set<string>>}
 */
const running = new WeakMap();

/**
 * The buttons for one message, in registration order.
 * @param {() => MessageRef} describe The message as it is now — asked again on
 *   a click, so `run` sees a ref that arrived after the buttons were drawn.
 * @param {object} [message] What identifies the message across redraws (its
 *   element): an action running on it keeps its button disabled.
 * @returns {HTMLButtonElement[]}
 */
export function messageActionButtons(describe, message = describe) {
  if (!actions.size) return [];
  const msg = describe();
  /** @type {HTMLButtonElement[]} */
  const out = [];
  for (const [id, action] of actions.entries()) {
    if (!ask(id, action.when, msg, true)) continue;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'msg-action';
    btn.dataset.action = id;
    btn.title = action.label;
    btn.setAttribute('aria-label', action.label);
    if (action.pressed) {
      btn.setAttribute('aria-pressed', String(ask(id, action.pressed, msg, false)));
    }
    setGlyph(btn, action);
    btn.disabled = running.get(message)?.has(id) ?? false;
    btn.addEventListener('click', async () => {
      const busy = running.get(message) ?? new Set();
      if (busy.has(id)) return;
      running.set(message, busy.add(id));
      btn.disabled = true;
      try {
        await action.run(describe());
      } catch (err) {
        console.error(`lovia: message action "${id}" failed:`, err);
        toast(err instanceof Error ? err.message : String(err), { type: 'error' });
      } finally {
        busy.delete(id);
        // This button may have been redrawn meanwhile: redraw again, enabled.
        refreshMessageActions();
      }
    });
    out.push(btn);
  }
  return out;
}
