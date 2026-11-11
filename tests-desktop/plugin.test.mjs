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
  const opened = [];
  const disposers = [];
  let effects = [];
  let afterPost = false;
  const context = vm.createContext({ crypto: webcrypto, TextEncoder, TextDecoder, Uint8Array, Uint32Array, ArrayBuffer, DataView, setTimeout, clearTimeout, console });
  const sdk = { atom, useValue: store => store.get(), Button: 'button', ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'sidebar.nav', PALETTE_AREA: 'palette',
    host: { state: { profile, gateway: atom('open') }, navigate: path => calls.push(['navigate', path]) } };
  if (!options.legacy) sdk.gatewayActivationEpoch = () => epoch;
  const jsx = (type, props) => ({ type, props });
  const modules = {
    '@hermes/plugin-sdk': sdk,
    'react': { useEffect: fn => { effects.push(fn); } },
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
    os: { writeClipboard: async text => { clipboard.push(text); return true; },
      openExternal: async url => { opened.push(url); return options.openExternal ? options.openExternal(url) : true; } },
    rest: async (path, opts) => {
      calls.push([path, opts]);
      if (path === '/readiness') {
        assert.equal(opts?.method ?? 'GET', 'GET');
        return options.readiness ? options.readiness() : { platform: 'win32', code_version: 'canonical-tools-v1', tools: { rclone: { available: true, code: 'READY' } } };
      }
      if (path === '/ui-language') return { ui_language: 'ru' };
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
  return { ctx, contributions, calls, writes, clipboard, opened, text, controls, inputs, click, invoke, verify, ready,
    async mount() { effects = []; view(); const mounted = effects; effects = []; mounted.forEach(fn => fn()); for (let i = 0; i < 12; i++) await Promise.resolve(); },
    switchProfile() { profile.set('other'); epoch++; }, switchRoute() { epoch++; }, dispose() { disposers.forEach(fn => fn()); } };
}
const posts = h => h.calls.filter(([p]) => p === '/recipient');
function setupControls(h) {
  const node = h.controls().find(n => n?.props?.setup);
  const flatten = n => Array.isArray(n) ? n.flatMap(flatten) : n && typeof n === 'object' ? [n, ...flatten(n.props?.children)] : [];
  return flatten(node.type(node.props));
}
const setupButton = (h, label) => setupControls(h).find(n => n.type === 'button' && n.props.children === label);

for (const fixture of ['backup-http.json','backup-http-legacy.json']) test('shipped backup UI consumes actual composed HTTP runner responses and lost ACK: '+fixture, async () => {
  const actual = JSON.parse(await readFile(new URL('./fixtures/'+fixture, import.meta.url), 'utf8'));
  const publicKey = actual.setup_before.recipient;
  let current = actual.initial;
  const h = await harness({ rest: async (path, opts) => {
    if (path === '/status') return { profile_id: actual.ack.profile_id, recipient: publicKey };
    if (path === '/setup') return current.job?.state === 'succeeded' ? actual.setup_terminal : actual.setup_before;
    if (path === '/backup/jobs') {
      assert.equal(opts.body.profile_id, actual.ack.profile_id);
      current = actual.ack;
      throw new Error('synthetic lost HTTP 202 acknowledgement');
    }
    if (path === '/backup/jobs/current') return current;
    throw new Error(`Unexpected route ${path}`);
  } });
  await setupButton(h, 'Обновить состояние настройки').props.onClick();
  await setupButton(h, 'Проверить и создать первую копию').props.onClick();
  assert.equal(setupButton(h, 'Проверить и создать первую копию').props.disabled, true);
  for (const envelope of actual.polls) {
    current = envelope;
    await setupButton(h, 'Обновить состояние настройки').props.onClick();
    assert.equal(setupButton(h, 'Проверить и создать первую копию').props.disabled, true);
    assert.equal(setupButton(h, 'Сохранить и включить'), undefined);
    assert.doesNotMatch(h.text(), /Первая зашифрованная копия создана/);
  }
  current = actual.terminal;
  await setupButton(h, 'Обновить состояние настройки').props.onClick();
  assert.match(h.text(), /Первая зашифрованная копия создана/);
  assert.ok(setupButton(h, 'Сохранить и включить'));
  assert.equal(h.calls.filter(([path]) => path === '/backup/jobs').length, 1);
});

test('explicit incomplete capture warns on check and refresh without claiming verification', async () => {
  const publicKey = (await generateKeyPair()).recipient;
  let incomplete = false;
  const h = await harness({ rest: async path => {
    if (path === '/status') return { profile_id: 'bound', recipient: publicKey };
    if (path === '/backup/jobs') incomplete = true;
    if (path.startsWith('/backup/jobs')) return { code_version:'backup-jobs-v1', profile_id:'bound', job: incomplete ? { id:'fixture', state:'incomplete', phase:'complete', progress:{percent:null}, complete:false, verified:true, omitted_count:2 } : null };
    return { profile_id: 'bound', verified: false, stage: 'check', storage: 'workspace', interval_hours: 0,
      capture: incomplete ? { complete: false, omitted_count: 2, omitted: ['private/path'] } : null };
  } });
  await setupButton(h, 'Обновить состояние настройки').props.onClick();
  await setupButton(h, 'Проверить и создать первую копию').props.onClick();
  assert.match(h.text(), /INCOMPLETE/);
  assert.match(h.text(), /2/);
  assert.doesNotMatch(h.text(), /private\/path|создана, загружена и проверена/);
  assert.equal(setupButton(h, 'Сохранить и включить'), undefined);
  await setupButton(h, 'Обновить состояние настройки').props.onClick();
  assert.match(h.text(), /INCOMPLETE/);
});

test('setup checks first, reveals strict period only after verified readback, and schedules explicitly', async () => {
  const publicKey = (await generateKeyPair()).recipient;
  let verified = false, interval = 0;
  const view = () => ({ profile_id: 'bound', recipient_present: true, recipient_locked: true,
    stage: verified ? 'schedule' : 'check', verified, interval_hours: interval, storage: 'workspace' });
  const h = await harness({ rest: async (path, opts) => {
    if (path === '/status') return { profile_id: 'bound', recipient: publicKey };
    if (path === '/backup/jobs') { assert.equal(opts.body.profile_id, 'bound'); verified = true; }
    if (path.startsWith('/backup/jobs')) return { code_version:'backup-jobs-v1', profile_id:'bound', job: verified ? { id:'fixture', state:'succeeded', phase:'complete', progress:{percent:100}, complete:true, verified:true, omitted_count:0 } : null };
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
  assert.match(h.text(), /PERIOD_FORMAT_24h_OR_7d/);
  input.props.onChange({ currentTarget: { value: '7d' } });
  await setupButton(h, 'Сохранить и включить').props.onClick();
  assert.match(h.text(), /Расписание включено: каждые 168 ч/);
});

test('setup failures have actionable closed codes, including later status readback, without private text', async () => {
  const publicKey = (await generateKeyPair()).recipient;
  const cases = [
    ['SETUP_SOURCE_CHANGED', /изменились|изменился/],
    ['SETUP_READBACK_FAILED', /чтени|чтением/],
    ['SETUP_DEPENDENCY_UNAVAILABLE', /age|инструмент/],
    ['SETUP_CHECK_FAILED', /не завершена|не выполнена/],
    ['SETUP_STATUS_FAILED', /состояни/],
    ['SETUP_ACCESS_DENIED', /доступ/],
    ['SETUP_SQLITE_FAILED', /SQLite/],
  ];
  for (const [code, expected] of cases) {
    let failed = false;
    const h = await harness({ rest: async path => {
      if (path === '/status') return { profile_id: 'bound', recipient: publicKey };
      if (path === '/backup/jobs') { failed = true; throw new Error(`HTTP 409: {"detail":"${code}"} private/path token=secret`); }
      if (path === '/backup/jobs/current') return { code_version:'backup-jobs-v1', profile_id:'bound', job: failed ? { id:'fixture', state:'failed', phase:'capturing', progress:{percent:null}, complete:null, verified:false, omitted_count:0, error_code:code } : null };
      return { profile_id: 'bound', verified: false, stage: 'check', storage: 'rclone', interval_hours: 0, last_error_code: failed ? code : null };
    } });
    await setupButton(h, 'Обновить состояние настройки').props.onClick();
    await setupButton(h, 'Проверить и создать первую копию').props.onClick();
    assert.match(h.text(), new RegExp(code));
    assert.match(h.text(), expected);
    assert.doesNotMatch(h.text(), /private\/path|token=secret/);
    await setupButton(h, 'Обновить состояние настройки').props.onClick();
    assert.match(h.text(), new RegExp(code));
    assert.equal(setupButton(h, 'Сохранить и включить'), undefined);
    h.dispose();
  }
});

test('check timeout never enables schedule; scope switches fence retained setup callbacks', async () => {
  const publicKey = (await generateKeyPair()).recipient;
  const h = await harness({ rest: async (path) => {
    if (path === '/status') return { profile_id: 'bound', recipient: publicKey };
    if (path === '/backup/jobs') throw new Error('timeout with private provider body');
    if (path === '/backup/jobs/current') return { code_version:'backup-jobs-v1', profile_id:'bound', job:null };
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
  assert.deepEqual(h.contributions.map(c => c.id), ['pane', 'page', 'nav', 'open', 'default-maintenance-page', 'default-maintenance-nav', 'default-maintenance-open', 'recovery-page', 'recovery-nav', 'recovery-open']);
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

test('failed status or older SDK blocks generation until absence of a registered recipient is known', async () => {
  for (const options of [{ offline: true }, { legacy: true }, { status: () => ({ profile_id: '', recipient: null }) }]) {
    const h = await harness(options);
    assert.equal(h.controls().find(n => n?.props?.children === 'Generate new key').props.disabled, true);
    await h.invoke('Generate new key');
    assert.equal(h.controls().find(n => n?.props?.children === 'Save private key to local folder').props.disabled, true);
    assert.match(h.text(), /Public recipient status unknown/);
    assert.ok(!h.text().includes('No key generated'));
    await h.click('Refresh backend status');
    await h.invoke('Register public recipient'); assert.equal(posts(h).length, 0);
    assert.equal(h.writes.length, 0);
  }
});

test('wrong, oversized and malformed verification never authorizes registration', async () => {
  const other = await generateKeyPair();
  for (const f of [file(other.identity), { size: 4097, text() { assert.fail('Must reject size before reading'); } }, file('bad key')]) {
    const h = await harness(); await h.ready(); await h.verify(f); await h.click('Refresh backend status');
    await h.invoke('Register public recipient'); assert.equal(posts(h).length, 0);
  }
});

test('restart loads persisted public recipient without private state and permits public copy', async () => {
  const oldRecipient = 'age17s43yv3l8556x4m657nehv25sngyjjjfehgz87m2rmwc0k6nd5ksy43uvu';
  const h = await harness({ oldRecipient });
  assert.equal(h.calls.filter(([p]) => p === '/status').length, 1);
  assert.ok(h.text().includes(oldRecipient));
  assert.match(h.text(), /Private recovery file: not loaded in this session/);
  assert.ok(!h.text().includes('Saved key: not verified'));
  await h.click('Copy public recipient');
  assert.deepEqual(h.clipboard, [oldRecipient]);
  assert.equal(h.writes.length, 0);
  assert.equal(posts(h).length, 0);
  assert.equal(cloudButton(h, 'Обновить подключение').props.disabled, false);
  assert.equal(setupButton(h, 'Обновить состояние настройки').props.disabled, false);
});

test('page mount retries early unavailable backend and profile activation loads its own public status', async () => {
  const oldRecipient = 'age17s43yv3l8556x4m657nehv25sngyjjjfehgz87m2rmwc0k6nd5ksy43uvu';
  let reads = 0;
  const h = await harness({ status: () => {
    reads++;
    if (reads === 1) throw new Error('gateway not ready');
    return { profile_id: reads === 2 ? 'default' : 'other', recipient: reads === 2 ? oldRecipient : null };
  } });
  assert.match(h.text(), /Public recipient status unknown/);
  await h.mount();
  assert.ok(h.text().includes(oldRecipient));
  assert.equal(reads, 2);
  h.switchProfile(); await h.mount();
  assert.match(h.text(), /Backend profile: other/);
  assert.ok(!h.text().includes(oldRecipient));
  assert.equal(posts(h).length, 0);
  assert.equal(h.writes.length, 0);
});

test('initial hydration blocks generation until empty backend is confirmed', async () => {
  const pending = deferred();
  const h = await harness({ status: () => pending.promise });
  assert.equal(h.calls.filter(([p]) => p === '/status').length, 1);
  assert.equal(h.controls().find(n => n?.props?.children === 'Generate new key').props.disabled, true);
  assert.match(h.text(), /Public recipient status unknown/);
  await h.invoke('Generate new key');
  pending.resolve({ profile_id: 'default', recipient: null });
  for (let i = 0; i < 12; i++) await Promise.resolve();
  assert.match(h.text(), /No public recipient registered/);
  await h.click('Generate new key');
  assert.match(h.text(), /Key generated locally/);
});

test('scope changes immediately hide old recipient and disable stale empty-backend generation', async () => {
  for (const kind of ['switchProfile', 'switchRoute']) {
    for (const oldRecipient of [null, 'age17s43yv3l8556x4m657nehv25sngyjjjfehgz87m2rmwc0k6nd5ksy43uvu']) {
      const h = await harness({ oldRecipient });
      h[kind]();
      assert.equal(h.controls().find(n => n?.props?.children === 'Generate new key').props.disabled, true);
      assert.match(h.text(), /Public recipient status unknown/);
      if (oldRecipient) assert.ok(!h.text().includes(oldRecipient));
      await h.invoke('Copy public recipient');
      await h.invoke('Generate new key');
      assert.equal(h.clipboard.length, 0);
      assert.equal(h.writes.length, 0);
      assert.equal(posts(h).length, 0);
    }
  }
});

test('initial status resolving after a scope change cannot publish the previous recipient', async () => {
  for (const kind of ['switchProfile', 'switchRoute', 'dispose']) {
    const pending = deferred();
    const oldRecipient = 'age17s43yv3l8556x4m657nehv25sngyjjjfehgz87m2rmwc0k6nd5ksy43uvu';
    const h = await harness({ status: () => pending.promise });
    h[kind](); pending.resolve({ profile_id: 'default', recipient: oldRecipient });
    for (let i = 0; i < 12; i++) await Promise.resolve();
    assert.ok(!h.text().includes(oldRecipient));
    assert.equal(h.controls().find(n => n?.props?.children === 'Generate new key').props.disabled, true);
    assert.equal(posts(h).length, 0);
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
const cloudProvider = h => cloudControls(h).filter(n => n?.type === 'label').flatMap(n => n.props.children).find(n => n?.type === 'select');
const selectCloud = (h, value) => cloudProvider(h).props.onChange({ currentTarget: { value } });

test('cloud wizard keeps setup internals collapsed while broad-access consent stays visible', async () => {
  const h = await harness();
  const nodes = cloudControls(h);
  const help = nodes.find(n => n?.type === 'details');
  assert.ok(help, 'technical setup help is optional');
  assert.notEqual(help.props.open, true);
  const visible = nodes.filter(n => n?.type === 'p').map(n => n.props.children).join('\n');
  assert.ok(!visible.includes('Google Cloud Console'));
  assert.ok(!visible.includes('Порты:'));
  assert.match(visible, /чтение, изменение и удаление файлов всего диска/);
  assert.match(visible, /Текущее хранилище/);
});

test('cloud folder is hidden and new connections always use Finix', async () => {
  const publicKey = (await generateKeyPair()).recipient;
  const h = await harness({ rest: async (path, opts) => {
    if (path === '/status') return { profile_id: 'bound', recipient: publicKey };
    if (opts) assert.equal(opts.body.folder, 'Finix');
    return { profile_id: 'bound', ticket: 'csrf', session: { status: 'idle' } };
  } });
  assert.equal(cloudControls(h).filter(n => n?.type === 'label').flatMap(n => n.props.children).some(n => n?.type === 'input' && n.props.type !== 'checkbox'), false);
  assert.ok(!h.text().includes('Папка rclone'));
  await cloudButton(h, 'Обновить подключение').props.onClick();
  cloudChecks(h).forEach(c => c.props.onChange({ currentTarget: { checked: true } }));
  await cloudButton(h, 'Начать подключение').props.onClick();
  assert.equal(h.calls.filter(([, o]) => o?.method === 'POST').length, 1);
});

test('mounted handler readiness is read through the same SDK namespace only for rclone services', async () => {
  const h = await harness({ oldRecipient: 'age17s43yv3l8556x4m657nehv25sngyjjjfehgz87m2rmwc0k6nd5ksy43uvu',
    rest: async path => path === '/status' ? { profile_id: 'bound', recipient: 'age17s43yv3l8556x4m657nehv25sngyjjjfehgz87m2rmwc0k6nd5ksy43uvu' } : { profile_id: 'bound', ticket: 'csrf', session: { status: 'idle' } } });
  await h.mount();
  assert.equal(h.calls.filter(([p]) => p === '/readiness').length, 0, 'Workspace never depends on rclone');
  await selectCloud(h, 'yandex');
  assert.equal(h.calls.filter(([p]) => p === '/readiness').length, 1);
  assert.match(h.text(), /rclone: готов/);
  assert.equal(h.calls.filter(([p]) => p === '/connection').length, 2);
  const count = h.calls.length;
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(h.calls.length, count, 'no readiness polling');
  await h.mount();
  assert.equal(h.calls.filter(([p]) => p === '/readiness').length, 2, 'rclone mount rechecks mounted handler');
  assert.equal(h.opened.length, 0);
  assert.equal(h.calls.filter(([, o]) => o?.method === 'POST').length, 0);
});

test('readiness failures and stale code stay separate from successful connection and Workspace', async () => {
  for (const readiness of [
    () => { throw Object.assign(new Error('404 SECRET_PATH'), { status: 404 }); },
    () => { throw new Error('offline SECRET_ENV'); },
    () => ({ code_version: 'stale', tools: { rclone: { available: true, code: 'READY' } }, path: 'SECRET_PATH' })
  ]) {
    const h = await harness({ readiness, rest: async path => path === '/status' ?
      { profile_id: 'bound', recipient: 'age17s43yv3l8556x4m657nehv25sngyjjjfehgz87m2rmwc0k6nd5ksy43uvu' } :
      { profile_id: 'bound', ticket: 'csrf', session: { status: 'idle' } } });
    await selectCloud(h, 'yandex');
    assert.match(h.text(), /обработчик требует перезагрузки/);
    assert.doesNotMatch(h.text(), /SECRET_|rclone: готов|Подключение не подтверждено/);
    cloudChecks(h).forEach(c => c.props.onChange({ currentTarget: { checked: true } }));
    assert.equal(cloudButton(h, 'Начать подключение').props.disabled, false, 'diagnostic failure does not erase connection state');
    await selectCloud(h, 'workspace');
    assert.doesNotMatch(h.text(), /обработчик требует перезагрузки|rclone: /);
    assert.equal(h.calls.filter(([p]) => p === '/readiness').length, 1);
    cloudChecks(h).forEach(c => c.props.onChange({ currentTarget: { checked: true } }));
    assert.equal(cloudButton(h, 'Начать подключение').props.disabled, false);
    assert.equal(h.calls.filter(([, o]) => o?.method === 'POST').length, 0);
    assert.equal(h.opened.length, 0);
  }
});

test('pending readiness does not hold connection state and late reads cannot cross provider or scope', async () => {
  for (const change of ['provider', 'switchProfile', 'switchRoute', 'dispose']) {
    const pending = deferred();
    const h = await harness({ readiness: () => pending.promise, rest: async path => path === '/status' ?
      { profile_id: 'bound', recipient: 'age17s43yv3l8556x4m657nehv25sngyjjjfehgz87m2rmwc0k6nd5ksy43uvu' } :
      { profile_id: 'bound', ticket: 'csrf', session: { status: 'idle' } } });
    const loading = selectCloud(h, 'yandex');
    for (let i = 0; i < 12; i++) await Promise.resolve();
    cloudChecks(h).forEach(c => c.props.onChange({ currentTarget: { checked: true } }));
    assert.equal(cloudButton(h, 'Начать подключение').props.disabled, false);
    if (change === 'provider') await selectCloud(h, 'workspace'); else h[change]();
    pending.resolve({ code_version: 'canonical-tools-v1', tools: { rclone: { available: true, code: 'READY' } } });
    await loading;
    assert.doesNotMatch(h.text(), /rclone: готов/);
  }
});

test('current handler with missing rclone reports only the concise safe status', async () => {
  const h = await harness({ readiness: () => ({ code_version: 'canonical-tools-v1', tools: {
    rclone: { available: false, code: 'TOOL_MISSING', path: 'SECRET_PATH' } } }) });
  await selectCloud(h, 'yandex');
  assert.match(h.text(), /rclone: недоступен/);
  assert.doesNotMatch(h.text(), /SECRET_PATH|обработчик требует перезагрузки/);
});

test('cloud mount loads default readiness without initial refresh or authorization', async () => {
  const publicKey = (await generateKeyPair()).recipient;
  const h = await harness({ rest: async path => path === '/status' ? { profile_id: 'bound', recipient: publicKey } :
    { profile_id: 'bound', ticket: 'csrf', session: { status: 'idle' } } });
  await h.mount();
  assert.equal(h.calls.filter(([p]) => p === '/connection').length, 1);
  assert.equal(h.opened.length, 0);
  cloudChecks(h).forEach(c => c.props.onChange({ currentTarget: { checked: true } }));
  assert.equal(cloudButton(h, 'Начать подключение').props.disabled, false);
  assert.equal(h.calls.filter(([, o]) => o?.method === 'POST').length, 0);
});

test('provider selection loads public readiness, resets both confirmations, and never logs in or applies', async () => {
  const publicKey = (await generateKeyPair()).recipient;
  const h = await harness({ rest: async path => path === '/status' ? { profile_id: 'bound', recipient: publicKey } :
    { profile_id: 'bound', ticket: 'csrf', session: { status: 'idle' } } });
  cloudChecks(h).forEach(c => c.props.onChange({ currentTarget: { checked: true } }));
  await selectCloud(h, 'yandex');
  assert.equal(h.calls.filter(([path]) => path === '/connection').length, 1);
  assert.equal(cloudProvider(h).props.value, 'yandex');
  assert.equal(h.opened.length, 0);
  assert.ok(cloudChecks(h).every(c => !c.props.checked));
  assert.equal(cloudButton(h, 'Начать подключение').props.disabled, true);
  await cloudButton(h, 'Начать подключение').props.onClick();
  assert.equal(h.calls.filter(([, o]) => o?.method === 'POST').length, 0, 'retained callbacks cannot bypass consent');
  cloudChecks(h)[0].props.onChange({ currentTarget: { checked: true } });
  await cloudButton(h, 'Начать подключение').props.onClick();
  assert.equal(h.calls.filter(([, o]) => o?.method === 'POST').length, 0);
  cloudChecks(h)[1].props.onChange({ currentTarget: { checked: true } });
  h.controls().find(n => n?.props?.cloud).props.cloud.set({ folder: 'DO-NOT-USE' });
  await cloudButton(h, 'Начать подключение').props.onClick();
  const requests = h.calls.filter(([, o]) => o?.method === 'POST');
  assert.equal(requests.length, 1);
  assert.equal(requests[0][1].body.provider, 'yandex');
  assert.equal(requests[0][1].body.folder, 'Finix');
});

test('provider initialization fences stale reads, failures, scope switches and disposal', async () => {
  const publicKey = (await generateKeyPair()).recipient;
  for (const staleError of [false, true]) {
    const pending = deferred(); let reads = 0;
    const h = await harness({ rest: async path => {
      if (path === '/status') return { profile_id: 'bound', recipient: publicKey };
      const read = ++reads;
      if (read === 1) { await pending.promise; if (staleError) throw new Error('RCLONE_MISSING'); }
      return { profile_id: 'bound', ticket: 'csrf', session: { status: read === 1 ? 'failed' : 'idle', code: 'UNTRUSTED_BINARY' } };
    } });
    const first = selectCloud(h, 'yandex');
    await selectCloud(h, 'dropbox');
    assert.equal(cloudProvider(h).props.value, 'dropbox');
    assert.equal(reads, 2);
    pending.resolve(); await first;
    assert.ok(!h.text().includes('RCLONE_MISSING'));
    assert.ok(!h.text().includes('UNTRUSTED_BINARY'));
    cloudChecks(h).forEach(c => c.props.onChange({ currentTarget: { checked: true } }));
    assert.equal(cloudButton(h, 'Начать подключение').props.disabled, false);
  }
  for (const kind of ['switchProfile', 'switchRoute', 'dispose']) {
    const pending = deferred();
    const h = await harness({ rest: async path => path === '/status' ? { profile_id: 'bound', recipient: publicKey } : pending.promise });
    const loading = selectCloud(h, 'yandex'); h[kind]();
    pending.resolve({ profile_id: 'bound', ticket: 'csrf', session: { status: 'authorize', url: 'http://127.0.0.1/STALE' } });
    await loading;
    assert.equal(cloudButton(h, 'Открыть вход в браузере'), undefined);
    assert.equal(cloudButton(h, 'Начать подключение').props.disabled, true);
    assert.equal(h.calls.filter(([, o]) => o?.method === 'POST').length, 0);
  }
});

test('public initialization never applies existing verified sessions; retained apply requires verification', async () => {
  const publicKey = (await generateKeyPair()).recipient;
  let status = 'verified';
  const h = await harness({ rest: async path => path === '/status' ? { profile_id: 'bound', recipient: publicKey } :
    { profile_id: 'bound', ticket: 'csrf', session: { id: 'session', status } } });
  await h.mount();
  assert.equal(h.calls.filter(([, o]) => o?.method === 'POST').length, 0);
  const apply = cloudButton(h, 'Применить подключение').props.onClick;
  status = 'working'; await cloudButton(h, 'Обновить подключение').props.onClick();
  await apply();
  assert.equal(h.calls.filter(([, o]) => o?.method === 'POST').length, 0);
});

test('cloud selection errors remain visible and scope changes hide prior authorization immediately', async () => {
  const publicKey = (await generateKeyPair()).recipient;
  let fail = true;
  const h = await harness({ rest: async path => {
    if (path === '/status') return { profile_id: 'bound', recipient: publicKey };
    if (fail) throw new Error('RCLONE_MISSING SECRET_PROVIDER_BODY');
    return { profile_id: 'bound', ticket: 'csrf', session: { id: 'session', status: 'authorize', url: 'http://127.0.0.1/STALE' } };
  } });
  await selectCloud(h, 'yandex');
  assert.match(h.text(), /RCLONE_MISSING/);
  assert.ok(!h.text().includes('SECRET_PROVIDER_BODY'));
  fail = false; await selectCloud(h, 'dropbox');
  assert.ok(cloudButton(h, 'Открыть вход в браузере'));
  assert.match(h.text(), /после.*входа.*Обновить подключение/i);
  h.switchRoute();
  assert.equal(cloudButton(h, 'Открыть вход в браузере'), undefined);
  assert.ok(!h.text().includes('/STALE'));
  assert.equal(cloudButton(h, 'Начать подключение').props.disabled, true);
});

test('cloud apply readback must belong to the exact bound profile and session', async () => {
  const publicKey = (await generateKeyPair()).recipient;
  for (const mismatch of ['profile', 'session']) {
    let applied = false;
    const h = await harness({ rest: async (path, opts) => {
      if (path === '/status') return { profile_id: 'bound', recipient: publicKey };
      if (opts) { applied = true; return { profile_id: 'bound', ticket: 'csrf', session: { id: 'session', status: 'applied' } }; }
      return { profile_id: applied && mismatch === 'profile' ? 'other' : 'bound', ticket: 'csrf',
        session: { id: applied && mismatch === 'session' ? 'other' : 'session', status: applied ? 'applied' : 'verified' } };
    } });
    await selectCloud(h, 'yandex');
    await cloudButton(h, 'Применить подключение').props.onClick();
    assert.ok(!h.text().includes('Хранилище выбрано и прочитано обратно'));
    assert.match(h.text(), /Подключение не подтверждено/);
  }
});

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

test('cloud start explains refresh BEFORE login and refresh reveals explicit browser button without polling', async () => {
  const publicKey = (await generateKeyPair()).recipient;
  let status = 'idle';
  const h = await harness({ rest: async (path, opts) => {
    if (path === '/status') return { profile_id: 'bound', recipient: publicKey };
    if (opts) status = 'working';
    return { profile_id: 'bound', ticket: 'csrf', session: { id: 'session', status,
      ...(status === 'authorize' ? { url: 'http://127.0.0.1:53682/auth?state=synthetic' } : {}) } };
  } });
  await cloudButton(h, 'Обновить подключение').props.onClick();
  cloudChecks(h).forEach(c => c.props.onChange({ currentTarget: { checked: true } }));
  await cloudButton(h, 'Начать подключение').props.onClick();
  assert.match(h.text(), /Подготовка входа/);
  assert.match(h.text(), /Обновить подключение.*появится.*Открыть вход в браузере/);
  assert.equal(cloudButton(h, 'Открыть вход в браузере'), undefined);
  const count = h.calls.length;
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(h.calls.length, count, 'no hand polling');
  status = 'authorize';
  await cloudButton(h, 'Обновить подключение').props.onClick();
  assert.equal(cloudButton(h, 'Открыть вход в браузере').props.disabled, false);
  assert.match(h.text(), /Ссылка готова/);
});

test('cloud errors display only closed remediation codes, including asynchronous worker failures', async () => {
  const publicKey = (await generateKeyPair()).recipient;
  for (const code of ['RCLONE_MISSING', 'UNTRUSTED_BINARY', 'RCLONE_PROVIDER_FAILED']) {
    for (const asyncFailure of [false, true]) {
      const h = await harness({ rest: async (path, opts) => {
        if (path === '/status') return { profile_id: 'bound', recipient: publicKey };
        if (opts) throw new Error(`HTTP 409: Finix connection: ${code}; SECRET_PROVIDER_BODY`);
        return { profile_id: 'bound', ticket: 'csrf', session: { status: asyncFailure ? 'failed' : 'idle', code } };
      } });
      await cloudButton(h, 'Обновить подключение').props.onClick();
      if (!asyncFailure) {
        cloudChecks(h).forEach(c => c.props.onChange({ currentTarget: { checked: true } }));
        await cloudButton(h, 'Начать подключение').props.onClick();
      }
      assert.ok(h.text().includes(code), code);
      assert.ok(!h.text().includes('SECRET_PROVIDER_BODY'));
    }
  }
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
