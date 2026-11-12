import * as sdk from '@hermes/plugin-sdk';
import {useLocalized} from './i18n.mjs';
import { jsx, jsxs } from 'react/jsx-runtime';
import { useEffect } from 'react';

const remedies = Object.freeze({
  RCLONE_MISSING: 'rclone не найден в локальных инструментах Finix или PATH backend. Восстановите установку инструментов Finix и обновите подключение.',
  UNTRUSTED_BINARY: 'Инструмент или его каталог не прошёл проверку безопасности. Проверьте установку и права доступа; проверка не отключается.',
  RCLONE_PROVIDER_FAILED: 'rclone не завершил подключение к сервису. Проверьте сеть и доступность сервиса, затем начните заново.',
  CONNECTION_TIMEOUT: 'Время подключения истекло. Начните заново.',
  CONNECTION_UNSUPPORTED: 'Этот шаг авторизации не поддерживается мастером. Настройте именованный remote вручную.',
  GOOGLE_ACCOUNT_CHANGED: 'The Google account changed after verification. Cancel and explicitly check the intended profile account again. Existing storage is unchanged.',
  GOOGLE_AUTH_SETUP_REQUIRED: 'Google sign-in is not verified. Run hermes auth add finix:google in the selected profile, then check again. Legacy Workspace grants are not reused.',
  CONNECTION_BUSY: 'Другое подключение уже выполняется. Обновите его статус или отмените его.',
  OWNER_SESSION_CHANGED: 'Профиль или сессия изменились. Обновите подключение.',
  SETTINGS_CHANGED: 'Настройки изменились. Текущее хранилище сохранено; начните подключение заново.'
});
const diagnostic = code => Object.hasOwn(remedies, code) ? `${code}: ${remedies[code]}` : 'Подключение не подтверждено. Обновите статус и проверьте локальные инструменты, сеть и выбранный профиль. Текущее хранилище не заменено. Не присылайте коды или config в чат.';
const failure = error => {
  // The SDK may wrap HTTP detail in Error.message. Select only a closed code;
  // never echo any provider body, exception, callback URL or credential.
  const values = [error?.code, error?.detail, error?.message];
  const code = values.filter(v => typeof v === 'string').flatMap(v => v.match(/\b[A-Z][A-Z_]+\b/g) ?? []).find(v => Object.hasOwn(remedies, v));
  return diagnostic(code);
};
const sessionMessage = session => {
  switch (session.status) {
    case 'working': return 'Подготовка входа или проверка доступа. Нажмите «Обновить подключение» через несколько секунд: когда ссылка будет готова, появится кнопка «Открыть вход в браузере». Браузер автоматически не открывается. После входа обновите статус ещё раз.';
    case 'authorize': return 'Ссылка готова. Нажмите «Открыть вход в браузере» и подтвердите доступ на странице сервиса. После завершения входа нажмите «Обновить подключение», затем примените проверенное подключение. Мастер не определяет закрытие вкладки браузера автоматически. Текущее хранилище до применения не меняется.';
    case 'verified': return 'Доступ проверен. Нажмите «Применить подключение», чтобы выбрать это хранилище.';
    case 'applied': return 'Хранилище выбрано и прочитано обратно. Перейдите к шагу 3: «Проверить и создать первую копию».';
    case 'failed': return diagnostic(session.code);
    case 'expired': return diagnostic('CONNECTION_TIMEOUT');
    case 'choice': return 'Выберите диск ниже и обновите подключение для следующего шага.';
    case 'cancelled': return 'Подключение отменено. Текущее хранилище не заменено.';
    default: return 'Выберите сервис, подтвердите локальный браузер и разрешения, затем нажмите «Начать подключение». Вход сам по себе не загружает копию.';
  }
};
const current = () => ({ profile: sdk.host.state.profile.get(), epoch: sdk.gatewayActivationEpoch?.() });
export function createCloud(ctx, enabled = () => true) {
  const state = sdk.atom({ data: null, provider: 'workspace', consent: false,
    local: false, busy: false, loading: false, readiness: '', message: 'Выберите облачный сервис.' });
  let owner = null, disposed = false, revision = 0;
  const set = patch => { if (!disposed) state.set({ ...state.get(), ...patch }); };
  const fence = () => {
    const now = current();
    if (disposed || !owner || owner.profile !== now.profile || owner.epoch !== now.epoch || !Number.isSafeInteger(now.epoch)) {
      set({ data: null, consent: false, local: false }); throw new Error('scope');
    }
  };
  const run = async fn => {
    if (state.get().busy || disposed) return;
    set({ busy: true });
    try { await fn(); }
    catch (error) { set({ message: failure(error) }); }
    finally { set({ busy: false }); }
  };
  const refresh = async () => {
    if (disposed || (state.get().busy && !state.get().loading)) return;
    const request = ++revision;
    const now = current();
    if (!owner || owner.profile !== now.profile || owner.epoch !== now.epoch) {
      owner = now; set({ data: null, consent: false, local: false });
    }
    set({ busy: true, loading: true, readiness: '' });
    // Read the mounted handler through this plugin's authenticated SDK namespace.
    // Independent of connection state: an older router must not break the wizard.
    const probe = state.get().provider !== 'workspace' && Number.isSafeInteger(now.epoch) ? (async () => {
      let readiness;
      try {
        const data = await ctx.rest('/readiness');
        readiness = data?.code_version !== 'canonical-tools-v1' ? 'обработчик требует перезагрузки' :
          data.tools?.rclone?.available === true && data.tools.rclone.code === 'READY' ? 'rclone: готов' : 'rclone: недоступен';
      } catch {
        readiness = 'обработчик требует перезагрузки';
      }
      const latest = current();
      if (request === revision && now.profile === latest.profile && now.epoch === latest.epoch) set({ readiness });
    })() : Promise.resolve();
    try {
      fence(); const data = await ctx.rest('/connection');
      if (request !== revision) return;
      fence();
      if (typeof data.profile_id !== 'string' || typeof data.ticket !== 'string' || !data.session) throw new Error('contract');
      set({ data, message: sessionMessage(data.session) });
    } catch (error) {
      if (request === revision) set({ data: null, message: failure(error) });
    } finally {
      if (request === revision) set({ busy: false, loading: false });
      await probe;
    }
  };
  const action = (action, extra = {}) => run(async () => {
    fence(); const s = state.get(); if (!s.data || !enabled()) throw new Error('key required');
    if (action === 'start' && (!s.local || !s.consent)) throw new Error('consent required');
    if (action === 'apply' && s.data.session.status !== 'verified') throw new Error('verification required');
    const before = s.data;
    const data = await ctx.rest('/connection', { method: 'POST', body: {
      profile_id: before.profile_id, ticket: before.ticket, action,
      session_id: before.session.id ?? '', provider: s.provider, folder: 'Finix',
      confirmed: s.consent, local_browser: s.local, ...extra
    } });
    fence(); if (data.profile_id !== before.profile_id) throw new Error('binding');
    const readback = await ctx.rest('/connection'); fence();
    if (readback.profile_id !== before.profile_id || readback.session.id !== data.session.id) throw new Error('session');
    set({ data: readback, message: sessionMessage(readback.session) });
  });
  ctx.onDispose(() => { disposed = true; revision++; owner = null; state.set({ ...state.get(), data: null, consent: false, local: false }); });
  return { state, set, refresh, action,
    isCurrent: () => !disposed && owner !== null && owner.profile === current().profile && owner.epoch === current().epoch,
    select: provider => {
      if (state.get().busy && !state.get().loading) return;
      set({ provider, data: null, consent: false, local: false });
      return refresh();
    }, open: () => run(async () => {
      fence(); const url = state.get().data?.session.url;
      if (!url || !await ctx.os?.openExternal(url)) throw new Error('browser'); fence();
    }) };
}

export function Cloud({ cloud, enabled = true }) {
 const {jsx,jsxs,language}=useLocalized();
  const value = sdk.useValue(cloud.state), a = cloud;
  const s = a.isCurrent() ? value : { ...value, data: null, consent: false, local: false, message: 'Выберите сервис. Состояние подключения загружается для текущего профиля.' };
  const session = s.data?.session;
  const profile = sdk.useValue(sdk.host.state.profile);
  const gateway = sdk.useValue(sdk.host.state.gateway);
  const epoch = sdk.gatewayActivationEpoch?.();
  // Only public local connection state: no worker, OAuth or browser action.
  useEffect(() => { if (enabled) void a.refresh(); }, [a, enabled, profile, gateway, epoch]);
  const button = (label, onClick, disabled = false) => jsx(sdk.Button, { type: 'button', onClick, disabled: s.busy || disabled || !enabled, children: label });
  const active = session && ['working', 'authorize', 'choice', 'verified'].includes(session.status);
  return jsxs('section', { 'aria-label': 'Подключение облака', children: [
    !enabled && jsx('p', { children: 'Сначала зарегистрируйте публичный ключ на шаге 1.' }),
    jsx('h2', { children: '2. Облачный сервис и авторизация' }),
    jsx('p', { children: 'Выберите сервис, отметьте два подтверждения и нажмите «Начать подключение». Новые подключения используют папку Finix. Текущее хранилище не меняется до проверки доступа и явного применения.' }),
    s.provider !== 'workspace' && a.isCurrent() && s.readiness && jsx('p', { role: 'status', children: s.readiness }),
    button('Обновить подключение', a.refresh),
    jsxs('label', { children: ['Облачный сервис', jsx('select', { value: s.provider, disabled: (s.busy && !s.loading) || active,
      onChange: e => a.select(e.currentTarget.value),
      children: Object.entries({ workspace: 'Google Drive', yandex: 'Яндекс Диск', onedrive: 'OneDrive', dropbox: 'Dropbox' }).map(([value, label]) => jsx('option', { value, children: label }, value)) })] }),

    jsxs('label', { children: [jsx('input', { type: 'checkbox', checked: s.local, disabled: s.busy || active,
      onChange: e => a.set({ local: e.currentTarget.checked }) }), 'Браузер на машине backend или собственный SSH-туннель к его loopback уже настроен'] }),
    jsx('p', { children: s.provider === 'workspace' ? 'Google uses only the Finix drive.file grant: files created or explicitly opened by this app, not full-Drive access. This check never starts OAuth or uploads a backup.' : 'rclone provider permissions may include reading, changing and deleting files across the drive. Signing in does not upload a backup.' }),
    jsxs('label', { children: [jsx('input', { type: 'checkbox', checked: s.consent, disabled: s.busy || active,
      onChange: e => a.set({ consent: e.currentTarget.checked }) }), 'Подтверждаю подключение выбранного сервиса и возможные широкие права доступа'] }),
    button('Начать подключение', () => a.action('start'), !s.data || !s.local || !s.consent || active),
    session?.url && jsx('p', { children: 'Ссылка авторизации выбранного сервиса (откройте в доверенном браузере backend или через SSH-туннель):' }),
    session?.url && jsx('output', { style: { overflowWrap: 'anywhere' }, children: session.url }),
    session?.url && button('Открыть вход в браузере', a.open),
    session?.status === 'choice' && jsx('p', { children: `OneDrive: ${session.question}. Выберите вариант диска/типа в порядке rclone; если нужный вариант неясен, отмените и настройте именованный remote вручную.` }),
    ...(session?.choices ?? []).map(c => button(c.label, () => a.action('answer', { choice: c.id }))),
    button('Применить подключение', () => a.action('apply'), session?.status !== 'verified'),
    button('Отменить подключение', () => a.action('cancel'), !active),
    jsxs('details', { children: [
      jsx('summary', { children: 'Помощь: удалённый backend и первый вход Google' }),
      jsx('p', { children: 'For rclone, the browser must reach backend loopback port 53682 on the same machine or through your own SSH tunnel. No public callback is created.' }),
      jsx('p', { children: 'Google uses only Finix-declared OAuth with drive.file scope. Run hermes auth add finix:google in the selected profile before checking this connection. Hermes owns credentials; no Workspace-skill grant is reused. Legacy backups and settings are preserved and require explicit migration.' })
    ] }),
    jsx('p', { role: 'status', 'aria-live': 'polite', children: s.message })
  ] });
}
