import * as sdk from '@hermes/plugin-sdk';
import {useLocalized,createLocale,translate} from './i18n.mjs';
import { jsx, jsxs } from 'react/jsx-runtime';
import { useEffect } from 'react';
import { createKeySession, validateRecipient } from '../../web/keys.mjs';
import { createCloud, Cloud } from './cloud.mjs';
import { createSetup, SetupCheck } from './setup.mjs';
import { createRecovery, Recovery } from './recovery.mjs';
import { createDefaultMaintenance, DefaultMaintenance } from './default-maintenance.mjs';

// Authority is window-route scoped, not the currently focused chat's profile.
// Older hosts may generate/save keys, but cannot safely register them.
function scope() {
  return { profile: sdk.host.state.profile.get(), epoch: sdk.gatewayActivationEpoch?.() };
}
function sameScope(a, b) {
  return a.profile === b.profile && a.epoch === b.epoch;
}
function validStatus(value) {
  return value && typeof value.profile_id === 'string' && value.profile_id.length > 0 &&
    (value.recipient === null || validateRecipient(value.recipient));
}

function createWorkflow(ctx) {
  const key = createKeySession();
  const state = sdk.atom({ recipient: null, saved: false, verified: false, busy: false,
    replacement: false, backend: null, message: 'Generate a recovery key on this device. Only the public recipient can be sent to Finix.' });
  let owner = null;
  let disposed = false;
  const publish = patch => { if (!disposed) state.set({ ...state.get(), ...patch }); };
  function assertOwner() {
    if (disposed || !owner || !sameScope(owner, scope())) {
      key.clear();
      publish({ recipient: null, saved: false, verified: false, backend: null, replacement: false });
      throw new Error('scope');
    }
  }
  async function run(action, failure) {
    if (disposed || state.get().busy) return;
    publish({ busy: true });
    try { await action(); }
    catch { publish({ message: failure }); }
    finally { publish({ busy: false }); }
  }
  async function readStatus() {
    assertOwner();
    if (typeof ctx.rest !== 'function' || !Number.isSafeInteger(owner.epoch)) throw new Error('unavailable');
    const result = await ctx.rest('/status');
    assertOwner();
    if (!validStatus(result)) throw new Error('invalid status');
    return { profile_id: result.profile_id, recipient: result.recipient };
  }
  const actions = {
    generate: () => run(async () => {
      if (!state.get().backend || state.get().backend.recipient) return;
      assertOwner();
      key.clear(); owner = scope();
      publish({ recipient: null, saved: false, verified: false, backend: null, replacement: false });
      const recipient = await key.generate();
      assertOwner();
      publish({ recipient, message: 'Key generated locally. Choose a private local folder to save it, then select the saved file to verify.' });
    }, 'Key generation failed or the active profile/backend changed. Generate again.'),
    save: () => run(async () => {
      assertOwner();
      if (!key.recipient || !ctx.files?.requestFolder || !ctx.files?.writeText) throw new Error('unavailable');
      publish({ saved: false, verified: false });
      const picked = await ctx.files.requestFolder();
      assertOwner();
      if (!picked.ok || !picked.folder) {
        publish({ message: 'Save cancelled or folder access unavailable. Nothing registered.' }); return;
      }
      if (picked.folder.id === 'data') throw new Error('private plugin storage refused');
      // Unique filename plus exclusive creation; never overwrite an existing recovery key.
      const filename = `finix-recovery-${key.recipient}.txt`;
      const result = await ctx.files.writeText(picked.folder.id, filename, key.downloadText(), { exclusive: true });
      assertOwner();
      if (!result.ok) throw new Error('write failed');
      publish({ saved: true, message: `Saved ${filename}. Select that saved file below to verify it locally. Keep it private; Finix cannot recover it.` });
    }, 'Key was not confirmed saved (or profile/backend changed). Nothing registered. Choose another folder if the file already exists.'),
    verify: file => run(async () => {
      assertOwner();
      publish({ verified: false });
      if (!state.get().saved) throw new Error('save first');
      const matches = await key.verify(file);
      assertOwner();
      publish({ verified: matches, message: matches ? 'Saved key verified locally. Refresh backend status before registering the public recipient.' : 'This file belongs to a different key. Registration is blocked.' });
    }, 'Invalid key file (maximum 4096 bytes), key not saved, or profile/backend changed. Registration is blocked.'),
    refresh: () => run(async () => {
      if (!owner || !sameScope(owner, scope())) {
        key.clear(); owner = scope();
        publish({ recipient: null, saved: false, verified: false, backend: null });
      }
      const backend = await readStatus();
      publish({ backend, replacement: false, message: 'Backend status loaded for this key’s profile and route. Registration still requires your explicit click.' });
    }, 'Public recipient status unknown. Registration unavailable: backend missing, incompatible, or active profile/backend changed. Refresh backend status before generating a key; your registered public key and saved private files are not deleted.'),
    replacement: checked => {
      try { assertOwner(); publish({ replacement: checked === true }); }
      catch { publish({ message: 'Profile/backend changed. Generate a new key before registering.' }); }
    },
    register: () => run(async () => {
      assertOwner();
      const before = state.get();
      if (before.backend?.recipient) throw new Error('recipient locked');
      if (!before.saved || !before.verified || !key.verified || !before.backend) throw new Error('not ready');
      const current = await readStatus();
      assertOwner();
      if (current.profile_id !== before.backend.profile_id || current.recipient !== before.backend.recipient) {
        publish({ backend: current, replacement: false }); throw new Error('status changed');
      }
      if (current.recipient !== null) throw new Error('recipient locked');
      // No await between the final scope fence and the route-scoped SDK call.
      assertOwner();
      await ctx.rest('/recipient', { method: 'POST', body: {
        profile_id: current.profile_id, recipient: key.recipient, expected_recipient: null
      } });
      assertOwner();
      const after = await readStatus();
      if (after.profile_id !== current.profile_id || after.recipient !== key.recipient) throw new Error('readback mismatch');
      publish({ backend: after, replacement: false, message: 'Public recipient registered and read back exactly. The private recovery key stayed on this device.' });
    }, 'Registration not confirmed. Check status and profile/backend, verify the saved key, and an existing registered key cannot be replaced.'),
    copy: () => run(async () => {
      assertOwner();
      const recipient = key.recipient ?? state.get().backend?.recipient;
      if (!recipient || !await ctx.os?.writeClipboard(recipient)) throw new Error('clipboard unavailable');
      assertOwner(); publish({ message: 'Public recipient copied.' });
    }, 'Copy unavailable or profile/backend changed. Only the public recipient may be copied.'),
    clear: () => {
      if (state.get().busy) return;
      key.clear();
      publish({ recipient: null, saved: false, verified: false, replacement: false, message: 'Key cleared from plugin memory. Saved files and the registered public key are unchanged.' });
    }
  };
  ctx.onDispose(() => { disposed = true; key.clear(); owner = null; });
  return { state, actions, isCurrent: () => !disposed && owner !== null && sameScope(owner, scope()) };
}

function Finix({ workflow }) {
 const {jsx,jsxs,language}=useLocalized();
  const value = sdk.useValue(workflow.state);
  const profile = sdk.useValue(sdk.host.state.profile);
  const gateway = sdk.useValue(sdk.host.state.gateway);
  const epoch = sdk.gatewayActivationEpoch?.();
  // Registration can precede gateway readiness. Retry on mount/activation,
  // never poll or infer that an unavailable backend has no registered key.
  useEffect(() => { void workflow.actions.refresh(); }, [workflow, profile, gateway, epoch]);
  const s = workflow.isCurrent() ? value : { ...value, recipient: null, backend: null, saved: false, verified: false };
  const a = workflow.actions;
  const button = (label, onClick, disabled = false) => jsx(sdk.Button, { type: 'button', disabled: s.busy || disabled, onClick, children: label });
  const locked = !!s.backend?.recipient;
  return jsxs('section', {
    'aria-label': 'Finix — настройка резервного копирования', style: { padding: '16px', overflow: 'auto', height: '100%', color: 'var(--ui-text-primary)' },
    children: [
      jsx('h1', { children: 'Finix' }),
      jsx('p', { children: `Gateway profile: ${profile}. Keys are generated on this device, including when the backend is remote.` }),
      jsx('p', { children: 'Store the private recovery file outside Hermes in a safe local folder. Never send it in chat. Registration sends only the public recipient. Clearing or generating again discards the in-memory key.' }),
      jsx('h2', { children: '1. Ключ восстановления' }),
      locked && jsx('p', { role: 'status', children: 'Публичный ключ зарегистрирован. Приватный ключ хранится у вас. Изменение ключа недоступно.' }),
      button('Generate new key', a.generate, locked || !s.backend),
      button('Clear key', a.clear, !s.recipient || locked),
      jsx('p', { children: 'Public age recipient' }),
      jsx('output', { 'aria-label': 'Public age recipient', style: { overflowWrap: 'anywhere', display: 'block' }, children: s.recipient ?? s.backend?.recipient ?? (s.backend ? 'No public recipient registered' : 'Public recipient status unknown. Refresh backend status; saved keys are unchanged.') }),
      button('Copy public recipient', a.copy, !s.recipient && !locked),
      button('Save private key to local folder', a.save, !s.recipient),
      jsxs('label', { children: ['Verify saved recovery file (maximum 4096 bytes)', jsx('input', {
        type: 'file', accept: '.txt,text/plain', disabled: s.busy || !s.saved,
        onChange: event => {
          const file = event.currentTarget.files?.[0];
          event.currentTarget.value = '';
          if (file) return a.verify(file);
        }
      })] }),
      jsx('p', { children: s.verified ? 'Saved key: verified' : locked && !s.recipient ? 'Private recovery file: not loaded in this session. This does not mean your saved file is missing or deleted. Cloud/setup uses the registered public recipient; no private file is required.' : 'Saved key: not verified' }),
      button('Refresh backend status', a.refresh),
      jsx('p', { children: s.backend ? `Backend profile: ${s.backend.profile_id}; configured recipient: ${s.backend.recipient ?? 'none'}` : 'Backend registration unavailable until status is loaded.' }),
      button('Register public recipient', a.register, !s.saved || !s.verified || !s.backend || locked),
      jsx('p', { role: 'status', 'aria-live': 'polite', children: s.message }),
      jsx(Cloud, { cloud: workflow.cloud, enabled: locked }),
      jsx(SetupCheck, { setup: workflow.setup, enabled: locked })
    ]
  });
}

export default {
  id: 'finix', name: 'Finix',
  register(ctx) {
    const locale = createLocale(ctx);
    const workflow = createWorkflow(ctx);
    workflow.cloud = createCloud(ctx, () => workflow.isCurrent() && !!workflow.state.get().backend?.recipient);
    workflow.setup = createSetup(ctx);
    void workflow.actions.refresh();
    const render = () => jsx(Finix, { workflow });
    ctx.register({ id: 'pane', area: 'panes', title: 'Finix', data: { placement: 'right', width: '380px' }, render });
    ctx.register({ id: 'page', area: sdk.ROUTES_AREA, data: { path: '/finix' }, render });
    ctx.register({ id: 'nav', area: sdk.SIDEBAR_NAV_AREA, data: { path: '/finix', label: 'Finix', codicon: 'shield' } });
    ctx.register({ id: 'open', area: sdk.PALETTE_AREA, data: { id: 'finix.open', label: 'Open Finix', run: () => sdk.host.navigate('/finix') } });
    const recovery = createRecovery(ctx);
    const maintenance = createDefaultMaintenance(ctx);
    ctx.register({ id: 'default-maintenance-page', area: sdk.ROUTES_AREA, data: { path: '/finix/default-maintenance' }, render: () => jsx(DefaultMaintenance, { maintenance }) });
    ctx.register({ id: 'default-maintenance-nav', area: sdk.SIDEBAR_NAV_AREA, data: { path: '/finix/default-maintenance', label: 'Finix — physical-default maintenance', codicon: 'tools' } });
    ctx.register({ id: 'default-maintenance-open', area: sdk.PALETTE_AREA, data: { id: 'finix.default-maintenance', label: 'Finix: physical-default maintenance', run: () => sdk.host.navigate('/finix/default-maintenance') } });
    ctx.register({ id: 'recovery-page', area: sdk.ROUTES_AREA, data: { path: '/finix/restore' }, render: () => jsx(Recovery, { recovery }) });
    ctx.register({ id: 'recovery-nav', area: sdk.SIDEBAR_NAV_AREA, data: { path: '/finix/restore', label: 'Finix — восстановление', codicon: 'history' } });
    ctx.register({ id: 'recovery-open', area: sdk.PALETTE_AREA, data: { id: 'finix.restore', label: 'Finix: восстановить копию', run: () => sdk.host.navigate('/finix/restore') } });
    let navLanguage = 'ru';
    const localizedNav = language => {
      if(language===navLanguage)return;
      navLanguage=language;
      ctx.register({ id: 'recovery-nav', area: sdk.SIDEBAR_NAV_AREA, data: { path: '/finix/restore', label: translate('Finix — восстановление',language), codicon: 'history' } });
      ctx.register({ id: 'recovery-open', area: sdk.PALETTE_AREA, data: { id: 'finix.restore', label: translate('Finix: восстановить копию',language), run: () => sdk.host.navigate('/finix/restore') } });
    };
    ctx.onDispose(locale.subscribe(localizedNav));
  }
};
