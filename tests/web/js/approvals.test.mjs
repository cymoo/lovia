// Approval cards (approvals.js): the question, the scope chips, the outcome.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  approvalHeading,
  approvalOutcome,
  cardRegistry,
  outcomeText,
  scopeLabel,
} from '../../../lovia/web/static/js/approvals.js';
import { setDefaultView } from '../../../lovia/web/static/js/tool-views.js';

test('the question uses the tool label, else names the tool', () => {
  setDefaultView('shell', { label: () => 'Run command' });
  assert.equal(approvalHeading('shell'), 'Run command?');
  assert.equal(approvalHeading('create_schedule'), 'Allow create_schedule?');
});

test('scope chips: the command for shell, a verb for paths, else verbatim', () => {
  assert.equal(scopeLabel('shell:git push'), 'git push');
  assert.equal(scopeLabel('read:~/notes'), 'read ~/notes');
  assert.equal(scopeLabel('write:src'), 'write src');
  assert.equal(scopeLabel('mail:external'), 'mail:external');
  assert.equal(scopeLabel('shell:'), 'shell:');
  assert.equal(scopeLabel('plain'), 'plain');
});

test('a reused call id settles the newest card, once', () => {
  // A provider may reuse an id across turns: the earlier card (already
  // decided) must keep its record; the frame is for the one still open.
  const cards = cardRegistry();
  const first = { isConnected: true };
  const second = { isConnected: true };
  cards.add('c1', first);
  cards.add('c1', second);
  assert.equal(cards.take('c1'), second);
  assert.equal(cards.take('c1'), null); // settled: a repeat frame finds nothing
});

test('a card no longer on the page is not settled', () => {
  const cards = cardRegistry();
  cards.add('c1', { isConnected: false }); // another chat is showing now
  assert.equal(cards.take('c1'), null);
  assert.equal(cards.take('nope'), null);
});

test('a frame settles the card by who decided', () => {
  assert.equal(approvalOutcome({ approved: true, cause: 'user' }), 'approve');
  assert.equal(approvalOutcome({ approved: false, cause: 'user' }), 'deny');
  assert.equal(approvalOutcome({ approved: false, cause: 'timeout' }), 'timeout');
  assert.equal(approvalOutcome({ approved: false, cause: 'cancel' }), 'cancel');
  assert.match(outcomeText('timeout'), /denied automatically/);
});
