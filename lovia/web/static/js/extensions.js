// Loads a deployer's extension modules (ChatUI.scripts) before the app starts,
// so whatever they register is in place for the first render. No DOM access:
// unit-tested under node with a fake importer.

/**
 * Import every module in `urls` in parallel, resolving once each has loaded,
 * failed, or taken `timeoutMs`. A slow one stops holding up the start but
 * keeps loading, and registers whenever it lands (the registries redraw on
 * registration, so late is fine). `onFailure` hears of every module that
 * fails, before the timeout or after it — once per module.
 * @param {readonly string[]} urls
 * @param {{ timeoutMs?: number, importer?: (url: string) => Promise<unknown>,
 *   onFailure?: (url: string, err: unknown) => void }} [opts]
 * @returns {Promise<void>}
 */
export async function loadExtensions(
  urls,
  { timeoutMs = 5000, importer = (url) => import(url), onFailure = () => {} } = {},
) {
  await Promise.all(urls.map(async (url) => {
    const load = Promise.resolve()
      .then(() => importer(url))
      .then(
        () => 'loaded',
        (err) => {
          console.error(`lovia: extension ${url} failed to load:`, err);
          onFailure(url, err);
          return 'failed';
        },
      );
    /** @type {ReturnType<typeof setTimeout> | undefined} */
    let timer;
    const late = new Promise((resolve) => {
      timer = setTimeout(resolve, timeoutMs, 'late');
    });
    const outcome = await Promise.race([load, late]);
    clearTimeout(timer);
    if (outcome === 'late') {
      console.warn(`lovia: extension ${url} is still loading after ${timeoutMs} ms; starting without it`);
    }
  }));
}
