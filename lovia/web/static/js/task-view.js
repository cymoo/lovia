// One subagent task in the panel's viewer: what it was asked, what it is
// doing, what it reported — not its transcript, which is a chat of its own,
// one button away. The viewer is files.js's; this is a guest in it (see
// `openGuest`).
import { t } from './i18n.js';
import { store } from './store.js';
import { api } from './api.js';
import { icon } from './icons.js';
import { addCodeBlockControls } from './ui.js';
import { highlightIn, renderMarkdownInto } from './util.js';
import { builtinSummary } from './builtin-views.js';
import { toolLabel } from './tool-views.js';
import { elapsedNode, fmtElapsed } from './elapsed.js';

// While the task is at work and on screen: its step is told by no event.
const POLL_MS = 3000;
// How long a stop waits for its second click.
export const CONFIRM_MS = 4000;

/** A tool call as one line: "Read file a.md", as its card reads. */
export function stepLine(step) {
  return step ? builtinSummary(step.name, step.args) || toolLabel(step.name) : t('chat.thinking');
}

const OUTCOME = {
  completed: 'task.completed',
  failed: 'task.failed',
  stopped: 'task.cancelled',
  interrupted: 'task.interrupted',
};

/**
 * How a task stands, in a word or two.
 * @param {import('./background-items.js').BackgroundItem} item
 */
export function taskStatusText(item) {
  if (item.state === 'approval') {
    return t(item.waitsFor === 'question' ? 'bg.needsAnswer' : 'task.needsApproval');
  }
  if (item.live) return t(item.state === 'stopping' ? 'bg.stopping' : 'task.running');
  const took =
    item.endedAt && item.startedAt ? ` · ${fmtElapsed(item.endedAt - item.startedAt)}` : '';
  return t(OUTCOME[item.state] || 'task.completed') + took;
}

/**
 * @param {import('./background-items.js').BackgroundItem} first
 * @param {{ stop: (id: string) => Promise<void>, open: (id: string) => void }} actions
 * @returns {import('./files.js').ViewerGuest & { update: (item: any) => void }}
 */
export function taskGuest(first, { stop, open }) {
  let item = first;
  let summary = /** @type {any} */ (null);

  // ---- the header block
  const info = document.createElement('div');
  info.className = 'lv-proc-info';
  const status = document.createElement('div');
  status.className = 'lv-proc-info-meta';
  info.append(status);
  const stopBtn = document.createElement('button');
  stopBtn.type = 'button';
  stopBtn.className = 'lv-btn lv-btn-ghost lv-btn-sm';
  let armed = null;
  const disarm = () => {
    clearTimeout(armed);
    armed = null;
    stopBtn.classList.remove('lv-btn-danger');
    stopBtn.innerHTML = icon('square', { size: 12 });
    stopBtn.append(t('bg.stop'));
  };
  disarm();
  stopBtn.addEventListener('click', () => {
    if (!armed) {
      stopBtn.classList.add('lv-btn-danger');
      stopBtn.textContent = t('bg.confirmStop');
      armed = setTimeout(disarm, CONFIRM_MS);
      return;
    }
    disarm();
    void stop(item.id);
  });
  const openBtn = document.createElement('button');
  openBtn.type = 'button';
  openBtn.className = 'lv-btn lv-btn-ghost lv-btn-sm';
  openBtn.innerHTML = icon('external-link', { size: 12 });
  openBtn.append(t('bg.openChat'));
  openBtn.addEventListener('click', () => open(item.id));

  function renderInfo() {
    const how = document.createElement('span');
    how.textContent = taskStatusText(item);
    how.classList.toggle('lv-danger', item.failed);
    status.replaceChildren(how);
    if (item.state === 'running' && item.startedAt) {
      how.append(' ', elapsedNode(item.startedAt));
    }
    const buttons = document.createElement('span');
    buttons.className = 'lv-proc-info-actions';
    if (item.live && item.state !== 'stopping') buttons.append(stopBtn);
    buttons.append(openBtn);
    status.append(buttons);
  }
  renderInfo();

  // ---- the body
  const content = document.createElement('div');
  content.className = 'lv-task-detail';
  const prompt = document.createElement('details');
  prompt.className = 'lv-task-prompt';
  const promptHead = document.createElement('summary');
  promptHead.textContent = t('bg.prompt');
  const promptBody = document.createElement('div');
  promptBody.className = 'lv-task-prompt-body';
  prompt.append(promptHead, promptBody);
  const step = document.createElement('div');
  step.className = 'lv-task-step';
  const stats = document.createElement('div');
  stats.className = 'lv-proc-note';
  const answer = document.createElement('div');
  content.append(prompt, step, stats, answer);

  function renderBody() {
    if (!summary) return;
    promptBody.textContent = summary.prompt;
    prompt.hidden = !summary.prompt;
    step.hidden = item.state !== 'running';
    step.textContent = stepLine(summary.step);
    const counts = [t('bg.toolCalls', { n: summary.tool_calls })];
    if (summary.failed_calls) counts.push(t('bg.toolCallsFailed', { n: summary.failed_calls }));
    stats.textContent = counts.join(' · ');
    stats.hidden = !summary.tool_calls;
    renderAnswer();
  }

  let shownAnswer = /** @type {string | null} */ (null);
  function renderAnswer() {
    const text = summary.error || summary.report || '';
    if (text === shownAnswer) return; // keep the selection and the scroll
    shownAnswer = text;
    answer.replaceChildren();
    if (!text) return;
    const head = document.createElement('div');
    head.className = 'lv-files-group-label lv-task-answer-head';
    head.textContent = t(summary.error ? 'task.failed' : 'bg.report');
    // The transcript's typography, as a Markdown file in the viewer.
    const turn = document.createElement('div');
    turn.className = 'lv-turn';
    const body = document.createElement('div');
    body.className = 'lv-body';
    turn.append(body);
    if (summary.error) body.textContent = summary.error;
    else {
      renderMarkdownInto(body, text);
      highlightIn(body);
      addCodeBlockControls(body);
    }
    answer.append(head, turn);
  }

  // ---- loading
  let mounted = false;
  let timer = null;
  let busy = false;
  let again = false;
  let offPanel = null;
  const shown = () =>
    content.getClientRects().length > 0 && getComputedStyle(content).visibility === 'visible';

  async function load() {
    clearTimeout(timer);
    if (busy) {
      again = true;
      return;
    }
    busy = true;
    try {
      summary = await api.sessionSummary(item.id);
      if (mounted) renderBody();
    } catch {
      /* transient: the next load tries again */
    } finally {
      busy = false;
    }
    if (!mounted) return;
    if (again) {
      again = false;
      return void load();
    }
    poll();
  }
  function poll() {
    clearTimeout(timer);
    if (!mounted || !item.live) return;
    timer = setTimeout(() => {
      if (document.hidden || !shown()) poll();
      else void load();
    }, POLL_MS);
  }
  const onVisible = () => {
    if (!document.hidden && shown()) void load();
  };

  return {
    key: item.key,
    name: item.tag ? `${item.tag} ${item.title}` : item.title,
    title: item.title,
    get copy() {
      return { label: t('bg.copyPrompt'), text: summary?.prompt ?? item.title };
    },
    download: null,
    wrap: false,
    info,
    content,
    text: () => summary?.report || summary?.error || summary?.prompt || item.title,
    mount() {
      mounted = true;
      document.addEventListener('visibilitychange', onVisible);
      offPanel = store.on('panel-changed', onVisible);
      void load();
    },
    moved() {},
    leaving() {},
    unmount() {
      mounted = false;
      clearTimeout(timer);
      disarm();
      document.removeEventListener('visibilitychange', onVisible);
      offPanel?.();
    },
    /** The task as the list has it now. */
    update(next) {
      const moved = next.state !== item.state || next.live !== item.live;
      item = next;
      renderInfo();
      if (moved && mounted) void load();
      else renderBody();
    },
  };
}
