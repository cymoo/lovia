// i18n.js: a deployer's ChatUI.messages merge over the bundled strings, and
// the page language resolves user pick > default_lang > browser > English.
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { mergeMessages, resolveLang } from '../../../lovia/web/static/js/i18n.js';

const builtin = {
  en: { 'nav.chats': 'Chats', 'composer.stop': 'Stop' },
  zh: { 'nav.chats': '对话', 'composer.stop': '停止' },
};

test('overrides win, new languages are added, the bundled ones stay intact', () => {
  const { dicts } = mergeMessages(builtin, {
    zh: { 'nav.chats': '会话' },
    ja: { 'nav.chats': 'チャット' },
  });
  assert.equal(dicts.zh['nav.chats'], '会话');
  assert.equal(dicts.zh['composer.stop'], '停止');
  assert.deepEqual(dicts.ja, { 'nav.chats': 'チャット' });
  assert.equal(builtin.zh['nav.chats'], '对话');
});

test("only unknown keys under the UI's own prefixes are flagged", () => {
  const { unknown } = mergeMessages(builtin, {
    en: { 'nav.chat': 'typo', 'acme.help': 'Help', 'composer.stop': 'Halt' },
    zh: { 'nav.chat': '拼错' },
  });
  assert.deepEqual(unknown, ['nav.chat']);
});

test('no messages leaves the dictionaries as shipped', () => {
  const { dicts, unknown } = mergeMessages(builtin, undefined);
  assert.deepEqual(dicts, builtin);
  assert.deepEqual(unknown, []);
});

const available = ['en', 'zh', 'pt-BR'];

test("the user's pick wins, then default_lang, then the browser", () => {
  const browser = ['zh-CN'];
  assert.equal(resolveLang({ pref: 'en', defaultLang: 'pt-BR', browser, available }), 'en');
  assert.equal(resolveLang({ pref: 'auto', defaultLang: 'pt-BR', browser, available }), 'pt-BR');
  assert.equal(resolveLang({ pref: 'auto', defaultLang: null, browser, available }), 'zh');
});

test('browser tags match exactly, then by primary subtag, case-insensitively', () => {
  const pick = (browser) => resolveLang({ pref: 'auto', defaultLang: null, browser, available });
  assert.equal(pick(['PT-br']), 'pt-BR');
  assert.equal(pick(['zh-TW', 'en']), 'zh');
  assert.equal(pick(['fr-FR', 'en-GB']), 'en');
  assert.equal(pick(['fr']), 'en');
  assert.equal(pick([]), 'en');
});

test('a pick or default without strings is skipped', () => {
  assert.equal(resolveLang({ pref: 'ko', defaultLang: 'ja', browser: ['zh'], available }), 'zh');
});
