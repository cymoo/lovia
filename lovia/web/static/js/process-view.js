// One background command in the panel's viewer: what it was, how it stands,
// and its log — fetched by pages, followed while more may come. The viewer is
// files.js's; this is a guest in it (see `openGuest`).
import { t } from './i18n.js';
import { api } from './api.js';
import { icon } from './icons.js';
import { toast } from './toast.js';
import { copyToClipboard } from './ui.js';
import { formatDateTime } from './util.js';
import { createLogView } from './log-view.js';
import { elapsedNode, fmtElapsed } from './elapsed.js';

const PAGE_CHARS = 256 * 1024; // the server's cap
const POLL_MS = 1000;
const POLL_MAX_MS = 5000;

/** "Exited with code 1", "Stopped by you", … for a command that is over. */
export function outcomeText(p) {
  if (p.status === 'exited') {
    return p.exit_code === 0 ? t('bg.exitOk') : t('bg.exitCode', { code: p.exit_code ?? '?' });
  }
  const key = {
    user: 'bg.endedByUser',
    model: 'bg.endedByModel',
    shutdown: 'bg.endedByShutdown',
    closed: 'bg.endedByClosed',
    lost: 'bg.endedByLost',
  }[p.ended_by];
  return t(key || 'bg.stopped');
}

/**
 * @param {string} sessionId
 * @param {any} proc A row of GET …/processes.
 * @param {{ stop: (processId: string) => Promise<void> }} actions
 * @returns {import('./files.js').ViewerGuest & { update: (proc: any) => void }}
 */
export function processGuest(sessionId, proc, { stop }) {
  let p = proc;
  const log = createLogView();
  const status = document.createElement('div');
  status.className = 'lv-proc-note';
  status.hidden = true;

  // ---- the header block
  const info = document.createElement('div');
  info.className = 'lv-proc-info';
  const command = document.createElement('div');
  command.className = 'lv-proc-info-cmd';
  const code = document.createElement('code');
  code.textContent = p.command;
  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'lv-btn-icon';
  copy.title = t('bg.copyCommand');
  copy.setAttribute('aria-label', copy.title);
  copy.innerHTML = icon('copy', { size: 13 });
  copy.addEventListener('click', async () => {
    if (await copyToClipboard(p.command)) toast(t('toast.contentCopied'));
  });
  command.append(code, copy);
  const meta = document.createElement('div');
  meta.className = 'lv-proc-info-meta';
  const stopBtn = document.createElement('button');
  stopBtn.type = 'button';
  stopBtn.className = 'lv-btn lv-btn-ghost lv-btn-sm';
  stopBtn.innerHTML = icon('square', { size: 12 });
  stopBtn.append(t('bg.stop'));
  stopBtn.addEventListener('click', async () => {
    stopBtn.disabled = true;
    await stop(p.process_id);
    stopBtn.disabled = false;
  });
  info.append(command, meta);

  function renderInfo() {
    const bits = [];
    if (p.cwd) {
      const cwd = document.createElement('span');
      cwd.className = 'lv-proc-info-cwd';
      cwd.textContent = p.cwd;
      cwd.title = p.cwd;
      bits.push(cwd);
    }
    if (p.started_at) {
      const when = document.createElement('span');
      when.textContent = formatDateTime(p.started_at, { seconds: true });
      bits.push(when);
    }
    const how = document.createElement('span');
    if (p.status === 'running') {
      how.append(t('task.running'), ' ', elapsedNode(p.started_at));
    } else {
      const took = p.ended_at ? ` · ${fmtElapsed(p.ended_at - p.started_at)}` : '';
      how.textContent = outcomeText(p) + took;
      how.classList.toggle('lv-danger', p.status === 'exited' && p.exit_code !== 0);
    }
    bits.push(how);
    meta.replaceChildren(...bits);
    if (p.status === 'running') meta.append(stopBtn);
  }
  renderInfo();

  // ---- the log: one fetch at a time, the next one timed by the last
  let next = 0;
  let timer = null;
  let delay = POLL_MS;
  let over = false; // read to its end, or gone
  let mounted = false;
  let busy = false;

  function schedule(ms) {
    clearTimeout(timer);
    timer = null;
    if (mounted && !over && !document.hidden) timer = setTimeout(fetchPage, ms);
  }

  async function fetchPage() {
    if (busy) return;
    busy = true;
    try {
      const page = await api.processOutput(sessionId, p.process_id, {
        after: next,
        limit: PAGE_CHARS,
      });
      if (!mounted) return;
      status.hidden = true;
      // Offsets count characters as the server does, not UTF-16 units.
      const start = Math.max(next, page.base);
      if (start > next) log.gap(start - next);
      if (page.text) log.append(page.text);
      next = page.next;
      over = page.eof;
      if (over && !next) say(t('bg.noOutput'));
      delay = page.text ? POLL_MS : Math.min(delay * 2, POLL_MAX_MS);
      // A page cut by the cap has more right behind it.
      schedule(page.text && page.next - start >= PAGE_CHARS ? 0 : delay);
    } catch (err) {
      if (!mounted) return;
      if (err?.status === 404) {
        over = true;
        say(t('bg.logGone'));
      } else {
        say(err?.message || t('bg.logFailed'));
        schedule(POLL_MAX_MS);
      }
    } finally {
      busy = false;
    }
  }

  function say(message) {
    status.textContent = message;
    status.hidden = false;
  }

  const onVisible = () => {
    if (!document.hidden && timer === null && !busy) schedule(0);
  };

  return {
    key: `cmd:${p.process_id}`,
    name: p.command.split('\n')[0],
    title: p.command,
    download: api.processLogUrl(sessionId, p.process_id),
    info,
    content: log.el,
    text: () => log.text(),
    mount(body) {
      mounted = true;
      body.append(status, log.el);
      log.watch();
      document.addEventListener('visibilitychange', onVisible);
      schedule(0);
    },
    moved: () => log.watch(),
    leaving: () => log.unwatch(),
    unmount() {
      mounted = false;
      clearTimeout(timer);
      timer = null;
      log.unwatch();
      document.removeEventListener('visibilitychange', onVisible);
    },
    /** The command as just listed: its state, and maybe more output. */
    update(row) {
      const ended = p.status === 'running' && row.status !== 'running';
      p = row;
      renderInfo();
      if (ended && mounted && !over && !busy) schedule(0);
    },
  };
}
