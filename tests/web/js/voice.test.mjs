// Voice input's pure parts: the WAV the server takes (voice-recorder.js) and
// how transcribed text is spaced into the composer (util.js spaced).
//
//   node --test tests/web/js/

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { spaced } from '../../../lovia/web/static/js/util.js';
import {
  SAMPLE_RATE,
  SILENCE_DB,
  downmix,
  encodeWav,
  peakDb,
} from '../../../lovia/web/static/js/voice-recorder.js';

test('a WAV header says 16 kHz, mono, 16-bit', () => {
  const view = new DataView(encodeWav(new Float32Array(SAMPLE_RATE)));
  const ascii = (at) => String.fromCharCode(...[0, 1, 2, 3].map((i) => view.getUint8(at + i)));
  assert.equal(ascii(0), 'RIFF');
  assert.equal(ascii(8), 'WAVE');
  assert.equal(ascii(36), 'data');
  assert.equal(view.getUint16(20, true), 1); // PCM
  assert.equal(view.getUint16(22, true), 1); // mono
  assert.equal(view.getUint32(24, true), 16000);
  assert.equal(view.getUint16(34, true), 16);
  assert.equal(view.getUint32(40, true), SAMPLE_RATE * 2);
  assert.equal(view.byteLength, 44 + SAMPLE_RATE * 2);
  assert.equal(view.getUint32(4, true), view.byteLength - 8);
});

test('samples scale to 16-bit and clip at full scale', () => {
  const view = new DataView(encodeWav(new Float32Array([0, 1, -1, 2, -2, 0.5])));
  const at = (i) => view.getInt16(44 + i * 2, true);
  assert.deepEqual([0, 1, 2, 3, 4].map(at), [0, 32767, -32768, 32767, -32768]);
  assert.equal(at(5), 16383);
});

test('five minutes fits under the server limit', () => {
  // The page stops a take at 300 s; the route refuses past 10 MiB.
  assert.ok(44 + 300 * SAMPLE_RATE * 2 < 10 * 1024 * 1024);
});

test('downmix averages the channels', () => {
  const one = new Float32Array([0.5, -0.5]);
  assert.equal(downmix([one]), one);
  assert.deepEqual([...downmix([new Float32Array([1, 0]), new Float32Array([0, -1])])], [0.5, -0.5]);
});

test('the silence line sits far under a quiet voice', () => {
  assert.equal(peakDb(new Float32Array(100)), -Infinity);
  assert.ok(peakDb(new Float32Array([0, 0.0005, -0.0002])) < SILENCE_DB); // noise floor
  assert.ok(peakDb(new Float32Array([0, 0.01, -0.02])) > SILENCE_DB); // a whisper
  assert.equal(peakDb(new Float32Array([1])), 0);
});

test('Latin words get a space from their neighbours', () => {
  assert.equal(spaced('hello', 'world', ''), ' world');
  assert.equal(spaced('Done.', 'Next', ''), ' Next');
  assert.equal(spaced('', 'fix the', 'bug'), 'fix the ');
  assert.equal(spaced('run', '42', 'times'), ' 42 ');
});

test('no space where one is already there, or none belongs', () => {
  assert.equal(spaced('hello ', 'world', ' again'), 'world');
  assert.equal(spaced('', 'world', ''), 'world');
  assert.equal(spaced('(', 'word', ')'), 'word');
  assert.equal(spaced('line\n', 'word', '\nline'), 'word');
});

test('Chinese and Japanese text takes no spaces', () => {
  assert.equal(spaced('你好', '世界', '吗'), '世界');
  assert.equal(spaced('你好', 'world', '吗'), 'world');
  assert.equal(spaced('hello', '世界', 'again'), '世界');
});
