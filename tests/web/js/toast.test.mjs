// Notification replacement, lifetime, and priority with a deterministic clock.
import assert from 'node:assert/strict';
import { test } from 'node:test';

let caseId = 0;
async function fixture(t) {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  class Element {
    children = [];
    attrs = {};
    listeners = new Map();
    className = '';
    textContent = '';
    parent = null;
    classList = {
      add: (name) => { if (!this.classList.contains(name)) this.className += ` ${name}`; },
      remove: (name) => { this.className = this.className.split(' ').filter((n) => n !== name).join(' '); },
      contains: (name) => this.className.split(' ').includes(name),
    };
    appendChild(el) { this.children.push(el); el.parent = this; }
    remove() {
      if (this.parent) this.parent.children = this.parent.children.filter((el) => el !== this);
      this.parent = null;
    }
    setAttribute(k, v) { this.attrs[k] = v; }
    addEventListener(type, fn, opts) {
      const list = this.listeners.get(type) || [];
      list.push({ fn, once: opts?.once });
      this.listeners.set(type, list);
    }
    fire(type) {
      for (const listener of [...(this.listeners.get(type) || [])]) {
        if (listener.once) this.listeners.set(type, this.listeners.get(type).filter((l) => l !== listener));
        listener.fn();
      }
    }
  }
  const body = new Element();
  globalThis.document = {
    body,
    createElement: () => new Element(),
    getElementById: (id) => body.children.find((el) => el.id === id),
  };
  globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
  t.after(() => { delete globalThis.document; delete globalThis.requestAnimationFrame; });
  const { toast } = await import(`../../../lovia/web/static/js/toast.js?case=${++caseId}`);
  const pills = () => body.children[0]?.children || [];
  return { toast, pills, tick: (ms) => t.mock.timers.tick(ms) };
}

test('rapid mode changes update one element and restart its lifetime', async (t) => {
  const { toast, pills, tick } = await fixture(t);
  const first = toast('steer', { key: 'mode', timeout: 1500 });
  tick(1000);
  assert.equal(toast('queue', { key: 'mode', timeout: 1500 }), first);
  assert.deepEqual(pills().map((el) => el.textContent), ['queue']);
  tick(500); // original deadline must not dismiss the updated notice
  assert.ok(first.classList.contains('show'));
  tick(1000);
  assert.ok(!first.classList.contains('show'));
  tick(400);
  assert.equal(pills().length, 0);
});

test('identical notices coalesce but unrelated keys stay independent', async (t) => {
  const { toast, pills } = await fixture(t);
  const error = toast('offline', { type: 'error' });
  assert.equal(toast('offline', { type: 'error' }), error);
  toast('offline', { key: 'mode' });
  assert.equal(pills().length, 2);
});

test('a burst never leaves more than two elements, including exiting pills', async (t) => {
  const { toast, pills, tick } = await fixture(t);
  toast('first');
  const second = toast('second');
  tick(0);
  second.fire('click');
  toast('third');
  assert.deepEqual(pills().map((el) => el.textContent), ['first', 'third']);
  for (let i = 0; i < 20; i++) {
    toast(`notice ${i}`);
    assert.equal(pills().length, 2);
  }
});

test('ordinary notices cannot displace errors, including when both slots are errors', async (t) => {
  const { toast, pills } = await fixture(t);
  toast('error A', { type: 'error' });
  toast('info A');
  toast('info B');
  assert.deepEqual(pills().map((el) => el.textContent), ['error A', 'info B']);
  toast('error B', { type: 'error' });
  toast('mode', { key: 'mode' });
  assert.deepEqual(pills().map((el) => el.textContent), ['error A', 'error B']);
  toast('error C', { type: 'error' });
  assert.deepEqual(pills().map((el) => el.textContent), ['error B', 'error C']);
});

test('click dismissal and old transition callbacks cannot remove a newer notice', async (t) => {
  const { toast, pills, tick } = await fixture(t);
  const old = toast('steer', { key: 'mode' });
  tick(0);
  old.fire('click');
  const next = toast('queue', { key: 'mode' });
  old.fire('transitionend');
  tick(400);
  assert.deepEqual(pills(), [next]);
  tick(2800);
  tick(400);
  assert.equal(pills().length, 0);
});

test('keyed updates refresh severity and the screen reader role', async (t) => {
  const { toast, tick } = await fixture(t);
  const el = toast('working', { key: 'operation' });
  tick(0);
  assert.equal(toast('failed', { key: 'operation', type: 'error' }), el);
  assert.equal(el.attrs.role, 'alert');
  assert.ok(el.classList.contains('toast-error'));
});

test('a notice dismissed before its animation frame never reappears', async (t) => {
  const { toast, tick } = await fixture(t);
  const el = toast('dismiss now');
  el.fire('click');
  tick(0);
  assert.ok(!el.classList.contains('show'));
});
