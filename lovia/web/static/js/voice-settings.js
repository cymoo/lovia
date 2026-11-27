// voice-settings.js — Settings → Voice input: where a recorded take goes to
// become text (the `voice` section of config.json), and a one-sentence test
// through the same route the composer uses.

import { api } from './api.js';
import { t } from './i18n.js';
import {
  cfg,
  el,
  field,
  ghostBtn,
  input,
  loadConfig,
  notice,
  readOnlyNotice,
} from './model-config.js';
import { toast } from './toast.js';
import { SILENCE_DB, captureBlocked, startCapture, toWav } from './voice-recorder.js';

const OFF = '';
const CUSTOM = '+custom';
// What a transcription model tends to be called, to pick them out of a
// connection's model list.
const TRANSCRIBES = /transcri|whisper|asr|sense-?voice|voxtral/i;
const LANGUAGES = ['zh', 'en', 'ja', 'ko', 'de', 'fr', 'es'];

function select(options, value) {
  const node = /** @type {HTMLSelectElement} */ (el('select', 'lv-dialog-input'));
  for (const [val, label] of options) {
    const opt = /** @type {HTMLOptionElement} */ (el('option', '', label));
    opt.value = val;
    node.appendChild(opt);
  }
  node.value = value;
  return node;
}

const hostOf = (url) => {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
};

/** The Settings → Voice input pane. */
export function buildVoicePane() {
  const pane = el('div', 'lv-cfg-pane');

  async function render() {
    if (!cfg()) {
      try {
        await loadConfig();
      } catch (err) {
        pane.replaceChildren(el('div', 'lv-cfg-error', String(err.message || err)));
        return;
      }
    }
    const c = cfg();
    const voice = c.voice;
    pane.replaceChildren(el('div', 'lv-cfg-pane-title', t('voice.settingsTitle')));
    const ro = readOnlyNotice();
    if (ro) pane.appendChild(ro);
    // A connection that leads nowhere turned voice input off without a
    // word anywhere else: say which, so it doesn't read as a lost setting.
    if (voice.problem) {
      pane.appendChild(notice(t(`voice.problem.${voice.problem}`, { id: voice.connection })));
    }
    const form = el('div', 'lv-cfg-form');

    const lenders = c.models.filter((m) => m.flavor === 'openai');
    const options = [
      [OFF, t('voice.off')],
      ...lenders.map((m) => [m.id, t('voice.borrow', { name: m.display_name })]),
      [CUSTOM, t('voice.custom')],
    ];
    const stored = voice.connection ?? (voice.base_url ? CUSTOM : OFF);
    // Kept in the list while it is what the file says, so a save of
    // something else on this page doesn't silently rewrite it.
    if (voice.connection && !lenders.some((m) => m.id === voice.connection)) {
      options.splice(1, 0, [voice.connection, voice.connection]);
    }
    const where = select(options, stored);
    form.appendChild(field(t('voice.connection'), where));

    const baseUrl = input(voice.base_url ?? '', { mono: true, placeholder: 'https://api.openai.com/v1' });
    const baseField = field(t('voice.address'), baseUrl, t('voice.addressHint'));
    /** @type {'keep' | 'edit' | 'clear'} */
    let keyMode = voice.api_key.set ? 'keep' : 'edit';
    const keyIn = input('', { type: 'password', placeholder: t('cfg.apiKeyNone') });
    const keyBox = el('div', 'lv-cfg-key');
    const renderKey = () => {
      keyBox.replaceChildren();
      if (keyMode === 'keep') {
        const row = el('div', 'lv-cfg-key-set');
        row.append(
          el('span', 'lv-cfg-key-dot'),
          el('span', 'lv-cfg-key-mask', t('cfg.apiKeySet', { hint: voice.api_key.hint || '' })),
          ghostBtn(t('cfg.replace'), () => {
            keyMode = 'edit';
            renderKey();
            keyIn.focus();
          }),
          ghostBtn(t('cfg.clear'), () => {
            keyMode = 'clear';
            renderKey();
            syncSave();
          }),
        );
        keyBox.appendChild(row);
      } else if (keyMode === 'clear') {
        const row = el('div', 'lv-cfg-key-set');
        row.append(
          el('span', 'lv-cfg-key-mask', t('cfg.apiKeyNone')),
          ghostBtn(t('cfg.keep'), () => {
            keyMode = 'keep';
            renderKey();
            syncSave();
          }),
        );
        keyBox.appendChild(row);
      } else {
        keyBox.appendChild(keyIn);
      }
    };
    renderKey();
    const keyField = field(t('cfg.apiKey'), keyBox, t('cfg.apiKeyNote'));
    form.append(baseField, keyField);

    const model = input(voice.model ?? '', { mono: true, placeholder: 'gpt-transcribe' });
    const known = /** @type {HTMLDataListElement} */ (el('datalist'));
    known.id = 'voice-models';
    model.setAttribute('list', known.id);
    const modelField = field(t('voice.model'), model, t('voice.modelHint'));
    modelField.appendChild(known);
    form.appendChild(modelField);

    const languages = [[OFF, t('voice.languageAuto')], ...LANGUAGES.map((code) => [code, t(`voice.lang.${code}`)])];
    if (voice.language && !LANGUAGES.includes(voice.language)) {
      languages.push([voice.language, voice.language]);
    }
    const language = select(languages, voice.language ?? OFF);
    const languageField = field(t('voice.language'), language, t('voice.languageHint'));
    form.appendChild(languageField);

    const sentTo = el('div', 'lv-cfg-hint');
    form.appendChild(sentTo);

    // The models the borrowed connection lists, for the model field's
    // suggestions. Asked once per connection; an endpoint that lists none
    // just leaves the field free-form.
    const listed = new Map();
    async function suggest() {
      const id = where.value;
      known.replaceChildren();
      if (id === OFF || id === CUSTOM) return;
      if (!listed.has(id)) {
        listed.set(
          id,
          api.testConnection({ profile_id: id }).then(
            (res) => (res.models ?? []).filter((name) => TRANSCRIBES.test(name)),
            () => [],
          ),
        );
      }
      const names = await listed.get(id);
      if (where.value !== id) return;
      known.replaceChildren(
        ...names.map((name) => {
          const opt = /** @type {HTMLOptionElement} */ (el('option'));
          opt.value = name;
          return opt;
        }),
      );
    }

    /** Saves ``body``; the pane is drawn again from what was saved. */
    const apply = async (body, done = '') => {
      // Held still while it saves: the redraw would drop newer edits.
      form.inert = true;
      try {
        await api.setVoice(body);
        await loadConfig();
        if (done) toast(done);
      } catch (err) {
        toast(String(err.message || err), { type: 'error' });
        await loadConfig().catch(() => {});
      }
      await render();
    };

    // The typed fields — a custom endpoint's address and key, the model —
    // have a Save; the two lists save as they are picked.
    const save = el('button', 'lv-btn lv-btn-sm lv-btn-primary', t('cfg.save'));
    save.setAttribute('type', 'button');
    const dirty = () =>
      (where.value === CUSTOM &&
        (stored !== CUSTOM ||
          baseUrl.value.trim() !== (voice.base_url ?? '') ||
          keyMode === 'clear' ||
          (keyMode === 'edit' && Boolean(keyIn.value)))) ||
      model.value.trim() !== (voice.model ?? '');
    const syncSave = () => {
      save.hidden = where.value === OFF;
      save.toggleAttribute('disabled', !c.writable || !dirty());
    };
    save.addEventListener('click', () => {
      /** @type {Record<string, string | null>} */
      const body = { model: model.value.trim() || null };
      if (where.value === CUSTOM) {
        body.connection = null;
        body.base_url = baseUrl.value.trim() || null;
        if (keyMode === 'clear') body.api_key = '';
        else if (keyMode === 'edit' && keyIn.value) body.api_key = keyIn.value;
      }
      save.toggleAttribute('disabled', true);
      void apply(body, t('cfg.saved'));
    });
    const saveRow = el('div', 'lv-cfg-inline-actions');
    saveRow.appendChild(save);
    modelField.after(saveRow);
    for (const node of [baseUrl, keyIn, model]) node.addEventListener('input', syncSave);

    const sync = () => {
      const mode = where.value;
      const custom = mode === CUSTOM;
      baseField.hidden = keyField.hidden = !custom;
      modelField.hidden = languageField.hidden = mode === OFF;
      const lender = c.models.find((m) => m.id === mode);
      const url = custom ? baseUrl.value.trim() : lender ? lender.base_url || c.defaults.openai_base_url : '';
      sentTo.hidden = mode === OFF;
      sentTo.textContent = url ? t('voice.sentTo', { host: hostOf(url) }) : t('voice.sentSomewhere');
      syncSave();
      void suggest();
    };
    where.addEventListener('change', () => {
      const mode = where.value;
      // A custom endpoint waits for its address: Save sends it.
      if (mode === CUSTOM) return sync();
      // Off keeps the model and language for the next time it is turned on;
      // a borrowed connection takes the model typed for it.
      void apply(
        mode === OFF
          ? { connection: null, base_url: null, api_key: '' }
          : { connection: mode, base_url: null, model: model.value.trim() || null },
      );
    });
    language.addEventListener('change', async () => {
      try {
        await api.setVoice({ language: language.value || null });
        await loadConfig();
      } catch (err) {
        toast(String(err.message || err), { type: 'error' });
        await render();
      }
    });
    baseUrl.addEventListener('input', sync);
    sync();

    // In the form, as the Models page's connection test is.
    if (voice.enabled) form.appendChild(trial());
    pane.append(form);
  }

  void render();
  return pane;
}

/**
 * "Try a sentence": record, then transcribe through POST /api/transcribe —
 * the microphone, the key, the model and the language in one go, which a
 * look at the endpoint's model list can't vouch for.
 */
function trial() {
  const box = el('div', 'lv-cfg-test-row');
  const btn = el('button', 'lv-btn lv-cfg-test-btn', t('voice.try'));
  btn.setAttribute('type', 'button');
  const out = el('span', 'lv-cfg-test-result', t('voice.tryHint'));
  out.setAttribute('role', 'status');
  /** @type {import('./voice-recorder.js').Capture | null} */
  let capture = null;
  let busy = false;
  const say = (text, bad = false) => {
    out.textContent = text;
    out.classList.toggle('lv-bad', bad);
  };

  async function stop() {
    const taken = capture;
    if (!taken) return;
    capture = null;
    busy = true;
    btn.disabled = true;
    btn.textContent = t('voice.try');
    say(t('voice.working'));
    try {
      const { wav, peakDb } = await toWav(await taken.stop());
      if (peakDb < SILENCE_DB) {
        say(t('voice.silent'), true);
        return;
      }
      const { text } = await api.transcribe(wav);
      say(String(text ?? '').trim() || t('voice.empty'));
    } catch (err) {
      say(t('voice.failed', { reason: err?.message || '' }), true);
    } finally {
      busy = false;
      btn.disabled = false;
    }
  }

  btn.addEventListener('click', async () => {
    if (busy) return;
    if (capture) return void stop();
    const blocked = captureBlocked();
    if (blocked) {
      say(t(blocked === 'insecure' ? 'voice.insecure' : 'voice.unsupported'), true);
      return;
    }
    busy = true;
    try {
      capture = await startCapture({ lost: () => void stop() });
    } catch (err) {
      say(t('voice.micFailed', { reason: err?.message || err?.name || '' }), true);
      return;
    } finally {
      busy = false;
    }
    // The pane was closed while the browser asked: nothing to show it in.
    if (!box.isConnected) {
      capture.discard();
      capture = null;
      return;
    }
    btn.textContent = t('voice.tryStop');
    say(t('voice.trying'));
    // Settings closed mid-take: no microphone stays open out of sight.
    const watch = setInterval(() => {
      if (!capture) return clearInterval(watch);
      if (box.isConnected) return;
      capture.discard();
      capture = null;
    }, 500);
  });
  box.append(btn, out);
  return box;
}
