import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseReport } from '../../../lovia/web/static/js/subagent-report.js';

test('a done report parses into its parts', () => {
  const r = parseReport('[subagent t2: done] agent=lovia.sub elapsed=1171s\n## Findings\n\n- a');
  assert.deepEqual(r, {
    id: 't2', ok: true, agent: 'lovia.sub', elapsed: 1171, body: '## Findings\n\n- a',
  });
});

test('a failed report carries its error as the body', () => {
  const r = parseReport('[subagent t1: failed] agent=coder elapsed=3s\nRuntimeError: boom');
  assert.equal(r?.ok, false);
  assert.equal(r?.body, 'RuntimeError: boom');
});

test('an empty body and a name with spaces still parse', () => {
  assert.deepEqual(parseReport('[subagent t3: done] agent=my agent elapsed=0s\n'), {
    id: 't3', ok: true, agent: 'my agent', elapsed: 0, body: '',
  });
});

test('anything else is a message', () => {
  for (const text of [
    'please read [subagent t1: done] agent=x elapsed=1s',
    '[subagent t1: running] agent=x elapsed=1s\nbody',
    '[subagent t1: done] agent=x\nno elapsed',
    'hello',
  ]) assert.equal(parseReport(text), null, text);
});
