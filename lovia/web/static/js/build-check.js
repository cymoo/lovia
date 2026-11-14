// Notices that the server now runs a different build than this page loaded.
//
// Every build mounts its assets under a version-stamped prefix (`_asset_token`
// in app.py), so once the server restarts on an upgrade (or edited files), the
// prefix this page came from is gone and a probe of it 404s. The open tab would
// keep running its old modules against the new API, and its lazy loads (charts
// → echarts) would fail, so the caller offers a reload. Never reload by itself:
// a draft or a pending approval would be lost.

/**
 * @param {{ base: string | undefined, onStale: () => void, fetch?: typeof fetch }} deps
 * @returns {() => Promise<void>} Probe now; a no-op once stale, or without a base.
 */
export function createBuildCheck({ base, onStale, fetch: get = globalThis.fetch }) {
  let stale = false;
  return async () => {
    if (stale || !base) return;
    let res;
    try {
      res = await get(`${base}favicon.svg`, { method: 'HEAD', cache: 'no-store' });
    } catch {
      return; // offline or mid-restart: the next check retries
    }
    // Only a missing prefix means a new build: a proxy's 401, a login redirect
    // or a 5xx says nothing about it. Re-test `stale` — probes may overlap.
    if (res.status !== 404 || stale) return;
    stale = true;
    onStale();
  };
}
