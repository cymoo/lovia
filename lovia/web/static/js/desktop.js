// desktop.js — "Show in Finder" / "Open in editor" menu items. They act on
// the machine lovia web runs on, so the server offers them (info.desktop)
// only where the browser is on it too, and never for a remote workspace.

import { api } from './api.js';
import { store } from './store.js';
import { t } from './i18n.js';
import { icon } from './icons.js';
import { toast } from './toast.js';

/**
 * The menu items for one entry; none where the server offers no desktop
 * actions or the files are on another machine.
 * @param {{ path: string, isDir: boolean, root?: string | null, remote?: boolean }} entry
 *   `root`: the project the entry belongs to, when not the Files panel's.
 */
export function desktopItems({ path, isDir, root, remote }) {
  const desktop = store.serverInfo?.desktop;
  if (!desktop || remote) return [];
  /** @param {'reveal' | 'edit'} action */
  const run = (action) => () =>
    void api.workspaceDesktop(action, { agent: store.agent, path, root: root || undefined }).catch((err) =>
      toast(String(err.message || err), { type: 'error' }),
    );
  const items = [{ label: t(`desktop.reveal.${desktop.reveal}`), icon: icon('folder', { size: 15 }), onSelect: run('reveal') }];
  if (desktop.edit && !isDir) {
    items.push({ label: t('desktop.edit'), icon: icon('external-link', { size: 15 }), onSelect: run('edit') });
  }
  return items;
}
