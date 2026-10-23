// registry.js — the keyed registries behind window.lovia's register* calls
// (renderers, tool views, message actions, views, context providers).
//
// A registration stands in for the one under its key until it is disposed.
// Disposing it brings back the one it replaced, whatever order the disposals
// come in: a script that wraps a built-in renderer and later unregisters
// leaves the built-in, not nothing.

/** @template T */
export class Registry {
  /** @type {Map<string, { value: T }[]>} */
  #stacks = new Map();

  /**
   * Register `value` under `key`, in front of whatever is there.
   * @param {string} key
   * @param {T} value
   * @param {() => void} [changed] Called when disposing this registration
   *   changes the current value under `key` (it was the current one).
   * @returns {() => void} Disposes of it; a second call does nothing.
   */
  add(key, value, changed = () => {}) {
    const entry = { value };
    let stack = this.#stacks.get(key);
    if (!stack) this.#stacks.set(key, (stack = []));
    stack.push(entry);
    return () => {
      const i = stack.indexOf(entry);
      if (i < 0) return;
      stack.splice(i, 1);
      if (!stack.length && this.#stacks.get(key) === stack) this.#stacks.delete(key);
      if (i === stack.length) changed();
    };
  }

  /**
   * @param {string} key
   * @returns {T | undefined} The current value under `key`.
   */
  get(key) {
    return this.#stacks.get(key)?.at(-1)?.value;
  }

  /** @param {string} key */
  has(key) {
    return this.#stacks.has(key);
  }

  get size() {
    return this.#stacks.size;
  }

  /**
   * Each key's current value, keys in the order they were first registered.
   * @returns {Generator<[string, T]>}
   */
  *entries() {
    for (const [key, stack] of this.#stacks) yield [key, /** @type {T} */ (stack.at(-1)?.value)];
  }
}
