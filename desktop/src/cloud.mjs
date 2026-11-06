import * as sdk from '@hermes/plugin-sdk';
import { jsx, jsxs } from 'react/jsx-runtime';

const current = () => ({ profile: sdk.host.state.profile.get(), epoch: sdk.gatewayActivationEpoch?.() });
export function createCloud(ctx, enabled = () => true) {
  const state = sdk.atom({ data: null, provider: 'workspace', folder: 'Finix', consent: false,
    local: false, busy: false, message: 'Загрузите мастер для текущего backend.' });
  let owner = null, disposed = false;
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
    catch { set({ message: 'Подключение не подтверждено. Проверьте профиль, локальный браузер, rclone/Google OAuth client и зависимости. Текущее хранилище не заменено; обновите статус. Не присылайте коды или config в чат.' }); }
    finally { set({ busy: false }); }
  };
  const refresh = () => run(async () => {
    const now = current();
    if (!owner || owner.profile !== now.profile || owner.epoch !== now.epoch) {
      owner = now; set({ data: null, consent: false, local: false });
    }
    fence(); const data = await ctx.rest('/connection'); fence();
    if (typeof data.profile_id !== 'string' || typeof data.ticket !== 'string') throw new Error('contract');
    set({ data, message: `Состояние: ${data.session.status}. Настоящий доступ проверяется до применения; резервная копия ещё не загружалась.` });
  });
  const action = (action, extra = {}) => run(async () => {
    fence(); const s = state.get(); if (!s.data || !enabled()) throw new Error('key required');
    const before = s.data;
    const data = await ctx.rest('/connection', { method: 'POST', body: {
      profile_id: before.profile_id, ticket: before.ticket, action,
      session_id: before.session.id ?? '', provider: s.provider, folder: s.folder,
      confirmed: s.consent, local_browser: s.local, ...extra
    } });
    fence(); if (data.profile_id !== before.profile_id) throw new Error('binding');
    const readback = await ctx.rest('/connection'); fence();
    if (readback.session.id !== data.session.id) throw new Error('session');
    set({ data: readback, message: `Состояние: ${readback.session.status}. ${readback.session.status === 'applied' ? 'Хранилище выбрано и прочитано обратно. Перейдите к шагу 3: «Проверить и создать первую копию».' : 'Нажмите «Обновить подключение» после входа.'}` });
  });
  ctx.onDispose(() => { disposed = true; owner = null; state.set({ ...state.get(), data: null }); });
  return { state, set, refresh, action, open: () => run(async () => {
    fence(); const url = state.get().data?.session.url;
    if (!url || !await ctx.os?.openExternal(url)) throw new Error('browser'); fence();
  }) };
}

export function Cloud({ cloud, enabled = true }) {
  const s = sdk.useValue(cloud.state), a = cloud, session = s.data?.session;
  const button = (label, onClick, disabled = false) => jsx(sdk.Button, { type: 'button', onClick, disabled: s.busy || disabled || !enabled, children: label });
  const active = session && ['working', 'authorize', 'choice', 'verified'].includes(session.status);
  return jsxs('section', { 'aria-label': 'Подключение облака', children: [
    !enabled && jsx('p', { children: 'Сначала зарегистрируйте публичный ключ на шаге 1.' }),
    jsx('h2', { children: '2. Облачный сервис и авторизация' }),
    jsx('p', { children: 'Браузер должен достигать loopback backend: на той же машине либо через собственный SSH-туннель. Порты: Google Workspace 53683, остальные 53682. Для удалённого сервера используйте /finix connect в личном чате: режим tunnel даст точную SSH-команду, server-browser — ссылку для браузера на сервере. Публичный callback не нужен и не создаётся.' }),
    button('Обновить подключение', a.refresh),
    jsxs('label', { children: ['Облачный сервис', jsx('select', { value: s.provider, disabled: s.busy || active,
      onChange: e => a.set({ provider: e.currentTarget.value, consent: false }),
      children: Object.entries({ workspace: 'Google Workspace', yandex: 'Яндекс Диск', onedrive: 'OneDrive', dropbox: 'Dropbox' }).map(([value, label]) => jsx('option', { value, children: label }, value)) })] }),
    jsxs('label', { children: ['Папка rclone (Workspace создаёт свою профильную папку Finix)', jsx('input', { value: s.folder, disabled: s.busy || active || s.provider === 'workspace',
      onChange: e => a.set({ folder: e.currentTarget.value }) })] }),
    jsxs('label', { children: [jsx('input', { type: 'checkbox', checked: s.local, disabled: s.busy || active,
      onChange: e => a.set({ local: e.currentTarget.checked }) }), 'Браузер на машине backend или собственный SSH-туннель к его loopback уже настроен'] }),
    jsx('p', { children: 'Разрешения могут включать чтение, изменение и удаление файлов всего диска, не только папки Finix. Google использует существующий Workspace аккаунт; первый вход требует Desktop OAuth client JSON владельца (Google Cloud Console) в google_client_secret.json выбранного профиля и google-auth-oauthlib. Используйте локальный setup.py --client-secret FILE; не передавайте содержимое файла в чат. rclone использует встроенные OAuth приложения. Вход не загружает backup.' }),
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
    jsx('p', { role: 'status', 'aria-live': 'polite', children: s.message })
  ] });
}
