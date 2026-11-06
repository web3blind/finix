import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
import { webcrypto } from 'node:crypto';
import { generateKeyPair, recipientFromKeyFile } from '../web/keys.mjs';

// Executes the shipped ESM, not the source. Every external import must be public.
const bundle = await readFile(new URL('../desktop/plugin.js', import.meta.url), 'utf8');
const atom = value => ({ get: () => value, set: next => { value = next; } });
const file = text => ({ size: Buffer.byteLength(text), text: async () => text });
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
async function harness(options = {}) {
  let epoch = 1;
  let recipient = options.oldRecipient ?? null;
  const profile = atom('default');
  const contributions = [];
  const calls = [];
  const writes = [];
  const clipboard = [];
  const disposers = [];
  let afterPost = false;
  const context = vm.createContext({ crypto: webcrypto, TextEncoder, TextDecoder, Uint8Array, Uint32Array, ArrayBuffer, DataView, setTimeout, clearTimeout, console });
  const sdk = { atom, useValue: store => store.get(), Button: 'button', ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'sidebar.nav', PALETTE_AREA: 'palette',
    host: { state: { profile, gateway: atom('open') }, navigate: path => calls.push(['navigate', path]) } };
  if (!options.legacy) sdk.gatewayActivationEpoch = () => epoch;
  const jsx = (type, props) => ({ type, props });
  const modules = {
    '@hermes/plugin-sdk': sdk,
    'react/jsx-runtime': { jsx, jsxs: jsx }
  };
  const entry = new vm.SourceTextModule(bundle, { context });
  await entry.link(async specifier => {
    assert.ok(Object.hasOwn(modules, specifier), `Unexpected external dependency: ${specifier}`);
    const exports = modules[specifier];
    return new vm.SyntheticModule(Object.keys(exports), function () { for (const [name, value] of Object.entries(exports)) this.setExport(name, value); }, { context });
  });
  await entry.evaluate();
  const ctx = {
    register: c => contributions.push(c), onDispose: fn => disposers.push(fn),
    storage: { get() { assert.fail('No private storage reads'); }, set() { assert.fail('No private storage writes'); } },
    files: {
      requestFolder: async () => options.pick ? options.pick() : ({ ok: true, folder: { id: 'user-folder' } }),
      writeText: async (...args) => { writes.push(args); return options.write ? options.write(...args) : { ok: true }; }
    },
    os: { writeClipboard: async text => { clipboard.push(text); return true; } },
    rest: async (path, opts) => {
      calls.push([path, opts]);
      if (options.rest) return options.rest(path, opts);
      if (options.offline) throw new Error('offline');
      if (path === '/status') return options.status ? options.status({ recipient, afterPost }) : { profile_id: 'default', recipient };
      assert.equal(path, '/recipient');
      assert.equal(opts.method, 'POST');
      assert.equal(opts.body.profile_id, 'default');
      assert.equal(opts.body.expected_recipient, recipient);
      recipient = opts.body.recipient; afterPost = true;
      if (options.post) return options.post();
      return { ok: true };
    }
  };
  entry.namespace.default.register(ctx);
  for (let i = 0; i < 8; i++) await Promise.resolve();
  function expand(node) {
    if (Array.isArray(node)) return node.flatMap(expand);
    if (!node || typeof node !== 'object') return [node];
    if (typeof node.type === 'function') return expand(node.type(node.props));
    return [{ type: node.type, props: { ...node.props, children: undefined } }, ...expand(node.props?.children)];
  }
  const view = () => expand(contributions.find(c => c.id === 'pane').render());
  const text = () => view().filter(x => typeof x === 'string').join('\n');
  // Expand keeps text separately, so locate buttons from their unexpanded props.
  function controls() {
    const root = contributions.find(c => c.id === 'pane').render();
    const tree = root.type(root.props);
    return tree.props.children;
  }
  const click = async label => {
    const b = controls().find(n => n?.type === 'button' && n.props.children === label);
    assert.ok(b, `Missing ${label}`); assert.equal(!!b.props.disabled, false, `${label} disabled`);
    await b.props.onClick();
  };
  const invoke = async label => {
    const b = controls().find(n => n?.type === 'button' && n.props.children === label);
    await b.props.onClick();
  };
  const inputs = () => controls().filter(n => n?.type === 'label').flatMap(n => n.props.children).filter(n => n?.type === 'input');
  const verify = async f => { const target = { files: [f], value: 'selected' }; await inputs().find(n => n.props.type === 'file').props.onChange({ currentTarget: target }); assert.equal(target.value, ''); };
  const ready = async () => {
    await click('Generate new key'); await click('Save private key to local folder');
    await verify(file(writes.at(-1)[2]));
  };
  return { ctx, contributions, calls, writes, clipboard, text, controls, inputs, click, invoke, verify, ready,
    switchProfile() { profile.set('other'); epoch++; }, switchRoute() { epoch++; }, dispose() { disposers.forEach(fn => fn()); } };
}
const posts = h => h.calls.filter(([p]) => p === '/recipient');
function setupControls(h) {
  const node = h.controls().find(n => n?.props?.setup);
  const flatten = n => Array.isArray(n) ? n.flatMap(flatten) : n && typeof n === 'object' ? [n, ...flatten(n.props?.children)] : [];
  return flatten(node.type(node.props));
}
const setupButton = (h, label) => setupControls(h).find(n => n.type === 'button' && n.props.children === label);

test('setup checks first, reveals strict period only after verified readback, and schedules explicitly', async () => {
  const publicKey = (await generateKeyPair()).recipient;
  let verified = false, interval = 0;
  const view = () => ({ profile_id: 'bound', recipient_present: true, recipient_locked: true,
    stage: verified ? 'schedule' : 'check', verified, interval_hours: interval, storage: 'workspace' });
  const h = await harness({ rest: async (path, opts) => {
    if (path === '/status') return { profile_id: 'bound', recipient: publicKey };
    if (path === '/setup/check') { assert.equal(opts.body.profile_id, 'bound'); verified = true; }
    if (path === '/setup/schedule') { assert.equal(opts.body.period, '7d'); interval = 168; }
    return view();
  } });
  await setupButton(h, 'Обновить состояние настройки').props.onClick();
  assert.equal(setupButton(h, 'Сохранить и включить'), undefined);
  await setupButton(h, 'Проверить и создать первую копию').props.onClick();
  assert.match(h.text(), /Первая зашифрованная копия создана/);
  const input = setupControls(h).find(n => n.type === 'input');
  input.props.onChange({ currentTarget: { value: 'weekly' } });
  assert.equal(setupButton(h, 'Сохранить и включить').props.disabled, true);
  await setupButton(h, 'Сохранить и включить').props.onClick();
  assert.equal(h.calls.some(([path]) => path === '/setup/schedule'), false);
  input.props.onChange({ currentTarget: { value: '7d' } });
  await setupButton(h, 'Сохранить и включить').props.onClick();
  assert.match(h.text(), /Расписание включено: каждые 168 ч/);
});

test('check timeout never enables schedule; scope switches fence retained setup callbacks', async () => {
  const publicKey = (await generateKeyPair()).recipient;
  const h = await harness({ rest: async (path) => {
    if (path === '/status') return { profile_id: 'bound', recipient: publicKey };
    if (path === '/setup/check') throw new Error('timeout with private provider body');
    return { profile_id: 'bound', verified: false, stage: 'check', storage: 'workspace', interval_hours: 0 };
  } });
  await setupButton(h, 'Обновить состояние настройки').props.onClick();
  await setupButton(h, 'Проверить и создать первую копию').props.onClick();
  assert.equal(setupButton(h, 'Сохранить и включить'), undefined);
  assert.ok(!h.text().includes('private provider body'));
  const callback = setupButton(h, 'Проверить и создать первую копию').props.onClick;
  const before = h.calls.length; h.switchRoute(); await callback();
  assert.equal(h.calls.length, before);
});


test('shipped bundle contributes visible pane, page, navigation and public-only save/verify/register workflow', async () => {
  const h = await harness();
  assert.deepEqual(h.contributions.map(c => c.id), ['pane', 'page', 'nav', 'open']);
  await h.ready();
  const [folder, name, secret, opts] = h.writes[0];
  assert.equal(folder, 'user-folder'); assert.match(name, /^finix-recovery-age1.*\.txt$/); assert.equal(opts.exclusive, true);
  const publicKey = await recipientFromKeyFile(file(secret));
  assert.ok(h.text().includes(publicKey)); assert.ok(!h.text().includes('AGE-SECRET-KEY-1'));
  await h.click('Copy public recipient'); assert.deepEqual(h.clipboard, [publicKey]);
  await h.click('Refresh backend status'); await h.click('Register public recipient');
  assert.equal(posts(h).length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(posts(h)[0][1].body)), { profile_id: 'default', recipient: publicKey, expected_recipient: null });
  assert.match(h.text(), /registered and read back exactly/);
  assert.equal(h.calls.filter(([p]) => p === '/status').length, 4);
  assert.ok(!JSON.stringify(h.calls).includes('AGE-SECRET-KEY-1'));
  assert.ok(!JSON.stringify(h.controls()).includes('AGE-SECRET-KEY-1'));
});

test('save cancellation and failed exclusive write cannot verify or register', async () => {
  for (const options of [{ pick: () => ({ ok: true, folder: null }) }, { write: () => ({ ok: false, error: 'exists' }) }]) {
    const h = await harness(options); await h.click('Generate new key'); await h.click('Save private key to local folder');
    await h.invoke('Register public recipient'); assert.equal(posts(h).length, 0);
    assert.equal(h.inputs().find(n => n.props.type === 'file').props.disabled, true);
  }
});

test('offline or older SDK permits local generation/save/verify but never claims registration', async () => {
  for (const options of [{ offline: true }, { legacy: true }]) {
    const h = await harness(options); await h.ready(); await h.click('Refresh backend status');
    assert.match(h.text(), /Registration unavailable/);
    await h.invoke('Register public recipient'); assert.equal(posts(h).length, 0);
  }
});

test('wrong, oversized and malformed verification never authorizes registration', async () => {
  const other = await generateKeyPair();
  for (const f of [file(other.identity), { size: 4097, text() { assert.fail('Must reject size before reading'); } }, file('bad key')]) {
    const h = await harness(); await h.ready(); await h.verify(f); await h.click('Refresh backend status');
    await h.invoke('Register public recipient'); assert.equal(posts(h).length, 0);
  }
});

test('existing public recipient locks generation and registration with no replacement request', async () => {
  const oldRecipient = (await generateKeyPair()).recipient;
  const h = await harness({ oldRecipient });
  assert.match(h.text(), /Публичный ключ зарегистрирован. Приватный ключ хранится у вас/);
  assert.equal(h.controls().find(n => n?.props?.children === 'Generate new key').props.disabled, true);
  await h.invoke('Generate new key');
  await h.invoke('Register public recipient'); assert.equal(posts(h).length, 0);
  assert.equal(h.writes.length, 0);
  assert.equal(h.inputs().some(n => n.props.type === 'checkbox'), false);
});

test('profile and same-profile backend switches fence stale keys including switch-away/back', async () => {
  for (const kind of ['switchProfile', 'switchRoute']) {
    const h = await harness(); await h.ready(); await h.click('Refresh backend status'); h[kind]();
    await h.invoke('Register public recipient'); assert.equal(posts(h).length, 0);
    assert.ok(!h.text().includes('Saved key: verified'));
  }
});

test('profile switch during folder picker or file write cannot register or publish saved success', async () => {
  for (const point of ['pick', 'write']) {
    const pending = deferred(); const h = await harness({ [point]: () => pending.promise });
    await h.click('Generate new key'); const saving = h.click('Save private key to local folder');
    h.switchRoute(); pending.resolve(point === 'pick' ? { ok: true, folder: { id: 'user-folder' } } : { ok: true });
    await saving; await h.invoke('Register public recipient'); assert.equal(posts(h).length, 0);
    if (point === 'pick') assert.equal(h.writes.length, 0);
    assert.equal(h.inputs().find(n => n.props.type === 'file').props.disabled, true);
  }
});

test('registration readback mismatch never displays success', async () => {
  const h = await harness({ status: ({ recipient, afterPost }) => ({ profile_id: 'default', recipient: afterPost ? null : recipient }) });
  await h.ready(); await h.click('Refresh backend status'); await h.click('Register public recipient');
  assert.equal(posts(h).length, 1); assert.match(h.text(), /Registration not confirmed/);
  assert.ok(!h.text().includes('registered and read back exactly'));
});

test('status change between display and mutation aborts registration', async () => {
  let reads = 0; const other = (await generateKeyPair()).recipient;
  const h = await harness({ status: () => ({ profile_id: 'default', recipient: ++reads > 2 ? other : null }) });
  await h.ready(); await h.click('Refresh backend status'); await h.click('Register public recipient');
  assert.equal(posts(h).length, 0); assert.match(h.text(), /Registration not confirmed/);
});

test('profile switch during status preflight blocks POST and suppresses stale status', async () => {
  let reads = 0;
  const pending = deferred();
  const h = await harness({ status: () => ++reads <= 2 ? { profile_id: 'default', recipient: null } : pending.promise });
  await h.ready(); await h.click('Refresh backend status');
  const registering = h.click('Register public recipient');
  h.switchRoute(); pending.resolve({ profile_id: 'default', recipient: null });
  await registering;
  assert.equal(posts(h).length, 0); assert.match(h.text(), /Registration not confirmed/);
});

test('profile switch during local file verification cannot authorize stale key', async () => {
  const h = await harness(); await h.click('Generate new key'); await h.click('Save private key to local folder');
  const pending = deferred();
  const verifying = h.verify({ size: Buffer.byteLength(h.writes[0][2]), text: () => pending.promise });
  h.switchRoute(); pending.resolve(h.writes[0][2]); await verifying;
  await h.invoke('Register public recipient');
  assert.equal(posts(h).length, 0); assert.ok(!h.text().includes('Saved key: verified'));
});

test('route switch after dispatch cannot revoke in-flight POST, but never publishes success in new scope', async () => {
  const pending = deferred(); const h = await harness({ post: () => pending.promise });
  await h.ready(); await h.click('Refresh backend status');
  const registering = h.click('Register public recipient');
  // Advance through status preflight to the synchronous ctx.rest dispatch.
  for (let i = 0; i < 12 && posts(h).length === 0; i++) await Promise.resolve();
  assert.equal(posts(h).length, 1);
  h.switchRoute(); pending.resolve({ ok: true }); await registering;
  assert.ok(!h.text().includes('registered and read back exactly'));
  assert.match(h.text(), /Registration not confirmed/);
});

test('unload clears key authority and blocks retained callbacks', async () => {
  const h = await harness(); await h.ready(); await h.click('Refresh backend status'); h.dispose();
  await h.invoke('Register public recipient'); assert.equal(posts(h).length, 0);
});

function cloudControls(h) {
  const node = h.controls().find(n => n?.props?.cloud);
  return node.type(node.props).props.children;
}
const cloudButton = (h, label) => cloudControls(h).find(n => n?.type === 'button' && n.props.children === label);
const cloudChecks = h => cloudControls(h).filter(n => n?.type === 'label').flatMap(n => n.props.children).filter(n => n?.type === 'input' && n.props.type === 'checkbox');

test('cloud wizard requires local-browser and explicit scope consent, applies only verified sessions, and reads back', async () => {
  const registeredKey = (await generateKeyPair()).recipient;
  let status = 'idle';
  const view = () => ({ profile_id: 'bound-backend', ticket: 'synthetic-csrf', session: { id: 'synthetic-session', status } });
  const h = await harness({ rest: async (path, opts) => {
    if (path === '/status') return { profile_id: 'bound-backend', recipient: registeredKey };
    assert.equal(path, '/connection');
    if (opts) {
      assert.equal(opts.body.profile_id, 'bound-backend'); assert.equal(opts.body.ticket, 'synthetic-csrf');
      assert.equal(opts.body.confirmed, true); assert.equal(opts.body.local_browser, true);
      status = opts.body.action === 'start' ? 'working' : 'applied';
    }
    return view();
  } });
  assert.equal(cloudButton(h, 'Начать подключение').props.disabled, true);
  await cloudButton(h, 'Обновить подключение').props.onClick();
  assert.equal(cloudButton(h, 'Начать подключение').props.disabled, true);
  cloudChecks(h).forEach(c => c.props.onChange({ currentTarget: { checked: true } }));
  assert.equal(cloudButton(h, 'Начать подключение').props.disabled, false);
  await cloudButton(h, 'Начать подключение').props.onClick();
  assert.equal(cloudButton(h, 'Применить подключение').props.disabled, true);
  status = 'verified'; await cloudButton(h, 'Обновить подключение').props.onClick();
  assert.equal(cloudButton(h, 'Применить подключение').props.disabled, false);
  await cloudButton(h, 'Применить подключение').props.onClick();
  assert.match(h.text(), /Хранилище выбрано и прочитано обратно/);
  assert.ok(h.calls.filter(([, o]) => !o).length >= 4);
  assert.ok(!h.text().includes('synthetic-csrf'));
});

test('cloud wizard fences backend switches and unload before mutation', async () => {
  const publicKey = (await generateKeyPair()).recipient;
  const h = await harness({ rest: async path => path === '/status' ? { profile_id: 'bound', recipient: publicKey } : ({ profile_id: 'bound', ticket: 'csrf', session: { id: 'session', status: 'verified' } }) });
  await cloudButton(h, 'Обновить подключение').props.onClick();
  cloudChecks(h).forEach(c => c.props.onChange({ currentTarget: { checked: true } }));
  const oldApply = cloudButton(h, 'Применить подключение').props.onClick;
  h.switchRoute(); await oldApply();
  assert.equal(h.calls.filter(([, o]) => o?.method === 'POST').length, 0);
  await cloudButton(h, 'Обновить подключение').props.onClick();
  h.dispose(); await oldApply();
  assert.equal(h.calls.filter(([, o]) => o?.method === 'POST').length, 0);
});
