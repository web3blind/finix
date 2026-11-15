import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
import {english} from '../desktop/src/messages.mjs';

async function harness(rest, {sdkLocale = 'ru', hook = true} = {}) {
  const state = value => ({get: () => value, set: next => {value = next;}, subscribe: () => () => {}});
  const profile = state('default'), connectionId = state('local');
  const sdk = {atom: state, useValue: x => x.get(), host: {state: {profile, connectionId, gateway: state('open')}, activeConnectionId: () => connectionId.get()}, gatewayActivationEpoch: () => 1};
  if (hook) sdk.useI18n = () => ({locale: sdkLocale, isLoadingConfig: false});
  const jsx = (type, props) => ({type, props});
  const values = {'@hermes/plugin-sdk': sdk, 'react/jsx-runtime': {jsx, jsxs: jsx}, './messages.mjs': {english}};
  const context = vm.createContext({console});
  const source = new vm.SourceTextModule(await readFile(new URL('../desktop/src/i18n.mjs', import.meta.url), 'utf8'), {context});
  await source.link(name => new vm.SyntheticModule(Object.keys(values[name]), function () {for (const [key, value] of Object.entries(values[name])) this.setExport(key, value);}, {context}));
  await source.evaluate();
  const calls = [], disposers = [];
  const start = () => source.namespace.createLocale({rest: async (path, options) => {calls.push({path, options}); return rest(path);}, onDispose: fn => disposers.push(fn)});
  const modules = new Map();
  const component = async filename => {
    if (modules.has(filename)) return modules.get(filename);
    const module = new vm.SourceTextModule(await readFile(new URL('../desktop/src/' + filename, import.meta.url), 'utf8'), {context});
    modules.set(filename, module);
    await module.link(async name => {
      if (name === './i18n.mjs') return source;
      if (values[name]) return new vm.SyntheticModule(Object.keys(values[name]), function () {
        for (const [key, value] of Object.entries(values[name])) this.setExport(key, value);
      }, {context});
      return component(name.replace(/^\.\//, ''));
    });
    await module.evaluate();
    return module;
  };
  return {api: source.namespace, profile, start, calls, component: async name => (await component(name)).namespace, dispose: () => disposers.forEach(fn => fn()), tick: async () => {for (let n = 0; n < 12; n++) await Promise.resolve();}};
}

for (const value of [{}, {ui_language: 'de'}, {ui_language: null}, {ui_language: 'en'}]) {
  test('English is the closed default for ' + JSON.stringify(value), async () => {
    const h = await harness(() => value); const locale = h.start(); await h.tick();
    assert.equal(locale.view(), 'en');
    assert.equal(h.api.useLocalized().language, 'en', 'unattested Russian shell cannot override English');
    assert.ok(h.calls.every(c => c.path === '/ui-language' && !c.options?.method)); h.dispose();
  });
}
test('backend failure defaults to English, never another shell locale', async () => {
  const h = await harness(() => {throw Error('private sentinel');}); const locale = h.start(); await h.tick();
  assert.equal(locale.view(), 'en'); h.dispose();
});
test('Russian remains available explicitly', async () => {
  const h = await harness(() => ({ui_language: 'ru'}), {sdkLocale: 'en'}); const locale = h.start(); await h.tick();
  assert.equal(locale.view(), 'ru'); assert.equal(h.api.useLocalized().language, 'ru'); h.dispose();
});
test('older SDK without locale hook still renders English by default', async () => {
  const h = await harness(() => ({}), {hook: false}); const ui = h.api.useLocalized();
  assert.equal(ui.language, 'en');
  const node = ui.jsx('section', {children: 'Проверить и создать первую копию'});
  assert.equal(node.props.lang, 'en'); assert.equal(node.props.children, 'Check and create first backup');
});
test('scope changes remain fenced while English fallback resolves', async () => {
  let release, count = 0; const pending = new Promise(resolve => {release = resolve;});
  const h = await harness(() => ++count === 1 ? pending : {ui_language: 'en'});
  const locale = h.start(); h.profile.set('second'); assert.equal(locale.view(), null);
  await h.tick(); assert.equal(locale.view(), 'en'); release({ui_language: 'ru'}); await h.tick();
  assert.equal(locale.view(), 'en', 'late Russian from old profile must not overwrite current English'); h.dispose();
});

for (const language of ['en', 'ru']) {
  test('actual retired-recovery messages render in ' + language, async () => {
    const h = await harness(() => ({ui_language: language})); h.start(); await h.tick();
    const recoveryModule = await h.component('recovery.mjs');
    const calls = [];
    const recovery = recoveryModule.createRecovery({
      rest: async path => {calls.push(path); return {items: [
        {id: 'first', label: 'Encrypted backup', complete: true},
        {id: 'second', label: 'Encrypted backup', complete: false}
      ]};}, onDispose: () => {}
    });
    await recovery.actions.load();
    const recoveryTree = JSON.stringify(recoveryModule.Recovery({recovery}));
    const maintenanceModule = await h.component('default-maintenance.mjs');
    const maintenance = maintenanceModule.createDefaultMaintenance({});
    const maintenanceTree = JSON.stringify(maintenanceModule.DefaultMaintenance({maintenance}));
    assert.deepEqual(calls, ['/recovery/catalog']);
    if (language === 'en') {
      assert.doesNotMatch(recoveryTree + maintenanceTree, /[А-Яа-яЁё]/u);
      assert.match(recoveryTree, /No recovery key is requested/);
      assert.match(recoveryTree, /complete backup/);
      assert.match(maintenanceTree, /Physical default replacement/);
    } else {
      assert.match(recoveryTree, /Автоматический импорт/u);
      assert.match(maintenanceTree, /Замена физического default/u);
    }
    h.dispose();
  });
}
