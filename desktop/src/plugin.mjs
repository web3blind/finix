import * as sdk from '@hermes/plugin-sdk';
import { jsx, jsxs } from 'react/jsx-runtime';
import { createKeySession, validateRecipient } from '../../web/keys.mjs';
import { createCloud, Cloud } from './cloud.mjs';

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
      const backend = await readStatus();
      publish({ backend, replacement: false, message: 'Backend status loaded for this key’s profile and route. Registration still requires your explicit click.' });
    }, 'Registration unavailable: backend missing, incompatible, or active profile/backend changed. Local key generation and saving do not require a backend.'),
    replacement: checked => {
      try { assertOwner(); publish({ replacement: checked === true }); }
      catch { publish({ message: 'Profile/backend changed. Generate a new key before registering.' }); }
    },
    register: () => run(async () => {
      assertOwner();
      const before = state.get();
      if (!before.saved || !before.verified || !key.verified || !before.backend) throw new Error('not ready');
      const current = await readStatus();
      assertOwner();
      if (current.profile_id !== before.backend.profile_id || current.recipient !== before.backend.recipient) {
        publish({ backend: current, replacement: false }); throw new Error('status changed');
      }
      if (current.recipient !== null && current.recipient !== key.recipient && !before.replacement) throw new Error('confirm replacement');
      // No await between the final scope fence and the route-scoped SDK call.
      assertOwner();
      await ctx.rest('/recipient', { method: 'POST', body: {
        profile_id: current.profile_id, recipient: key.recipient, expected_recipient: current.recipient
      } });
      assertOwner();
      const after = await readStatus();
      if (after.profile_id !== current.profile_id || after.recipient !== key.recipient) throw new Error('readback mismatch');
      publish({ backend: after, replacement: false, message: 'Public recipient registered and read back exactly. The private recovery key stayed on this device.' });
    }, 'Registration not confirmed. Check status and profile/backend, verify the saved key, and explicitly confirm any replacement before retrying.'),
    copy: () => run(async () => {
      assertOwner();
      if (!key.recipient || !await ctx.os?.writeClipboard(key.recipient)) throw new Error('clipboard unavailable');
      assertOwner(); publish({ message: 'Public recipient copied.' });
    }, 'Copy unavailable or profile/backend changed. Only the public recipient may be copied.'),
    clear: () => {
      if (state.get().busy) return;
      key.clear(); owner = null;
      publish({ recipient: null, saved: false, verified: false, backend: null, replacement: false, message: 'Key cleared from plugin memory. Saved files are unchanged.' });
    }
  };
  ctx.onDispose(() => { disposed = true; key.clear(); owner = null; });
  return { state, actions };
}

function Finix({ workflow }) {
  const s = sdk.useValue(workflow.state);
  const profile = sdk.useValue(sdk.host.state.profile);
  const a = workflow.actions;
  const button = (label, onClick, disabled = false) => jsx(sdk.Button, { type: 'button', disabled: s.busy || disabled, onClick, children: label });
  const replacing = s.backend?.recipient && s.backend.recipient !== s.recipient;
  return jsxs('section', {
    'aria-label': 'Finix recovery keys', style: { padding: '16px', overflow: 'auto', height: '100%', color: 'var(--ui-text-primary)' },
    children: [
      jsx('h1', { children: 'Finix' }),
      jsx('p', { children: `Gateway profile: ${profile}. Keys are generated on this device, including when the backend is remote.` }),
      jsx('p', { children: 'Store the private recovery file outside Hermes in a safe local folder. Never send it in chat. Registration sends only the public recipient. Clearing or generating again discards the in-memory key.' }),
      button('Generate new key', a.generate),
      button('Clear key', a.clear, !s.recipient),
      jsx('p', { children: 'Public age recipient' }),
      jsx('output', { 'aria-label': 'Public age recipient', style: { overflowWrap: 'anywhere', display: 'block' }, children: s.recipient ?? 'No key generated' }),
      button('Copy public recipient', a.copy, !s.recipient),
      button('Save private key to local folder', a.save, !s.recipient),
      jsxs('label', { children: ['Verify saved recovery file (maximum 4096 bytes)', jsx('input', {
        type: 'file', accept: '.txt,text/plain', disabled: s.busy || !s.saved,
        onChange: event => {
          const file = event.currentTarget.files?.[0];
          event.currentTarget.value = '';
          if (file) return a.verify(file);
        }
      })] }),
      jsx('p', { children: s.verified ? 'Saved key: verified' : 'Saved key: not verified' }),
      button('Refresh backend status', a.refresh, !s.recipient),
      jsx('p', { children: s.backend ? `Backend profile: ${s.backend.profile_id}; configured recipient: ${s.backend.recipient ?? 'none'}` : 'Backend registration unavailable until status is loaded.' }),
      replacing && jsxs('label', { children: [jsx('input', {
        type: 'checkbox', checked: s.replacement, disabled: s.busy,
        onChange: event => a.replacement(event.currentTarget.checked)
      }), 'I explicitly confirm replacing the configured recipient shown above. I have kept the old recovery key for existing backups.'] }),
      button('Register public recipient', a.register, !s.saved || !s.verified || !s.backend || (replacing && !s.replacement)),
      jsx('p', { role: 'status', 'aria-live': 'polite', children: s.message }),
      jsx(Cloud, { cloud: workflow.cloud })
    ]
  });
}

export default {
  id: 'finix', name: 'Finix',
  register(ctx) {
    const workflow = createWorkflow(ctx);
    workflow.cloud = createCloud(ctx);
    const render = () => jsx(Finix, { workflow });
    ctx.register({ id: 'pane', area: 'panes', title: 'Finix', data: { placement: 'right', width: '380px' }, render });
    ctx.register({ id: 'page', area: sdk.ROUTES_AREA, data: { path: '/finix' }, render });
    ctx.register({ id: 'nav', area: sdk.SIDEBAR_NAV_AREA, data: { path: '/finix', label: 'Finix', codicon: 'shield' } });
    ctx.register({ id: 'open', area: sdk.PALETTE_AREA, data: { id: 'finix.open', label: 'Open Finix', run: () => sdk.host.navigate('/finix') } });
  }
};
