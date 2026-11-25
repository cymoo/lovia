// voice-recorder.js — capture one take from the microphone and turn it into
// the WAV the server takes (16 kHz, mono, 16-bit). No page knowledge: the
// composer (voice.js) and Settings' test (voice-settings.js) both record
// through it.

export const SAMPLE_RATE = 16000;
// A take quieter than this throughout is a muted or unplugged microphone,
// not a soft voice: speech peaks tens of dB above it.
export const SILENCE_DB = -60;

/**
 * Whether this page may record at all; a string says why not.
 * @returns {null | 'insecure' | 'unsupported'}
 */
export function captureBlocked() {
  if (!window.isSecureContext) return 'insecure';
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
    return 'unsupported';
  }
  return null;
}

/**
 * Mono 16-bit PCM in a WAV container.
 * @param {Float32Array} samples In [-1, 1].
 * @param {number} [rate]
 * @returns {ArrayBuffer}
 */
export function encodeWav(samples, rate = SAMPLE_RATE) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const ascii = (at, text) => {
    for (let i = 0; i < text.length; i++) view.setUint8(at + i, text.charCodeAt(i));
  };
  ascii(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  ascii(8, 'WAVE');
  ascii(12, 'fmt ');
  view.setUint32(16, 16, true); // fmt chunk size
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true); // bytes per second
  view.setUint16(32, 2, true); // bytes per frame
  view.setUint16(34, 16, true); // bits per sample
  ascii(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buffer;
}

/**
 * Average the channels into one.
 * @param {Float32Array[]} channels Equal lengths.
 * @returns {Float32Array}
 */
export function downmix(channels) {
  if (channels.length === 1) return channels[0];
  const out = new Float32Array(channels[0].length);
  for (const channel of channels) {
    for (let i = 0; i < out.length; i++) out[i] += channel[i];
  }
  for (let i = 0; i < out.length; i++) out[i] /= channels.length;
  return out;
}

/**
 * The loudest sample, in dBFS (-Infinity for digital silence).
 * @param {Float32Array} samples
 */
export function peakDb(samples) {
  let peak = 0;
  for (let i = 0; i < samples.length; i++) {
    const a = Math.abs(samples[i]);
    if (a > peak) peak = a;
  }
  return 20 * Math.log10(peak);
}

/**
 * A recorded blob (whatever container the browser chose) as the server's WAV.
 * Decoding in a 16 kHz context resamples it on the way.
 * @param {Blob} recorded
 * @returns {Promise<{ wav: Blob, seconds: number, peakDb: number }>}
 */
export async function toWav(recorded) {
  const Offline = window.OfflineAudioContext || /** @type {any} */ (window).webkitOfflineAudioContext;
  const ctx = new Offline(1, 1, SAMPLE_RATE);
  const audio = await ctx.decodeAudioData(await recorded.arrayBuffer());
  const channels = [];
  for (let i = 0; i < audio.numberOfChannels; i++) channels.push(audio.getChannelData(i));
  const samples = downmix(channels);
  return {
    wav: new Blob([encodeWav(samples)], { type: 'audio/wav' }),
    seconds: samples.length / SAMPLE_RATE,
    peakDb: peakDb(samples),
  };
}

/**
 * @typedef {object} Capture
 * @property {() => Promise<Blob>} stop End the take; resolves with what was recorded.
 * @property {() => void} discard End it and keep nothing.
 * @property {() => number} level The microphone's level right now, 0–1.
 * @property {number} startedAt `performance.now()` when recording began.
 */

/**
 * Ask for the microphone and start recording.
 *
 * Call it synchronously from the gesture that starts the take: the audio
 * context behind `level()` is made before the permission prompt, or Safari
 * leaves it suspended once the prompt is answered.
 *
 * `lost` is called when the microphone goes away under the take — unplugged,
 * permission withdrawn, or muted while the page is hidden (iOS Safari, in
 * the background): stop and keep what there is.
 * @param {{ lost?: () => void }} [opts]
 * @returns {Promise<Capture>} Rejects with getUserMedia's error.
 */
export async function startCapture({ lost = () => {} } = {}) {
  const Ctx = window.AudioContext || /** @type {any} */ (window).webkitAudioContext;
  /** @type {AudioContext | null} */
  let ctx = null;
  try {
    ctx = Ctx ? new Ctx() : null;
  } catch {
    ctx = null; // no meter; the take still records
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  } catch (err) {
    ctx?.close().catch(() => {});
    throw err;
  }
  const release = () => {
    stream.getTracks().forEach((track) => track.stop());
    ctx?.close().catch(() => {});
  };

  /** @type {AnalyserNode | null} */
  let analyser = null;
  /** @type {Float32Array<ArrayBuffer> | null} */
  let frame = null;
  if (ctx) {
    try {
      ctx.resume?.().catch(() => {});
      const node = ctx.createAnalyser();
      node.fftSize = 1024;
      ctx.createMediaStreamSource(stream).connect(node);
      analyser = node;
      frame = new Float32Array(node.fftSize);
    } catch {
      // No meter (Firefox refuses a source whose sample rate differs from
      // the output's): the take records all the same.
    }
  }

  let recorder;
  try {
    recorder = new MediaRecorder(stream);
  } catch (err) {
    release();
    throw err;
  }
  /** @type {Blob[]} */
  const chunks = [];
  recorder.addEventListener('dataavailable', (e) => {
    if (e.data.size) chunks.push(e.data);
  });
  const stopped = new Promise((resolve) => recorder.addEventListener('stop', resolve));
  recorder.start();

  let over = false;
  const [track] = stream.getAudioTracks();
  const onLost = () => {
    if (!over) lost();
  };
  track?.addEventListener('ended', onLost);
  track?.addEventListener('mute', () => {
    if (document.hidden) onLost();
  });

  const end = async () => {
    over = true;
    if (recorder.state !== 'inactive') recorder.stop();
    await stopped;
    release();
  };

  return {
    startedAt: performance.now(),
    level() {
      if (!analyser || !frame) return 0;
      analyser.getFloatTimeDomainData(frame);
      let sum = 0;
      for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
      // RMS of speech sits well under 1: scaled so a normal voice fills the meter.
      return Math.min(1, Math.sqrt(sum / frame.length) * 4);
    },
    async stop() {
      await end();
      return new Blob(chunks, { type: recorder.mimeType || chunks[0]?.type || '' });
    },
    discard() {
      void end();
    },
  };
}
