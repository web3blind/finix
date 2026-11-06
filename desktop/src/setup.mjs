import * as sdk from '@hermes/plugin-sdk';
import { jsx, jsxs } from 'react/jsx-runtime';

const scope = () => ({ profile: sdk.host.state.profile.get(), epoch: sdk.gatewayActivationEpoch?.() });
export function createSetup(ctx) {
  const state = sdk.atom({ data: null, busy: false, period: '24h', message: 'Обновите состояние настройки.' });
  let owner, disposed = false;
  const set = patch => { if (!disposed) state.set({ ...state.get(), ...patch }); };
  const fence = () => {
    const now = scope();
    if (disposed || !owner || now.profile !== owner.profile || now.epoch !== owner.epoch || !Number.isSafeInteger(now.epoch)) {
      set({ data: null }); throw new Error('scope');
    }
  };
  async function read() {
    fence(); const data = await ctx.rest('/setup'); fence();
    if (!data || typeof data.profile_id !== 'string' || typeof data.verified !== 'boolean') throw new Error('contract');
    return data;
  }
  const run = async fn => {
    if (disposed || state.get().busy) return;
    set({ busy: true });
    try { await fn(); }
    catch { set({ message: 'Операция не подтверждена. Обновите состояние: проверка на сервере может ещё выполняться. Не запускайте повторно до завершения. Проверьте профиль, подключение и формат периода 24h / 7d.' }); }
    finally { set({ busy: false }); }
  };
  const refresh = () => run(async () => {
    owner = scope(); const data = await read(); set({ data, message: data.check_running ? 'Проверка и первая копия ещё выполняются на сервере. Обновите состояние позже.' : data.verified ? 'Первая зашифрованная копия проверена. Укажите период.' : 'Сначала подключите облако, затем проверьте и создайте первую копию.' });
  });
  const action = name => run(async () => {
    fence(); const before = state.get();
    if (!before.data || (name === 'schedule' && (!before.data.verified || !/^[1-9][0-9]*[hd]$/.test(before.period)))) throw new Error('not ready');
    const data = await ctx.rest(`/setup/${name}`, { method: 'POST', timeoutMs: 300000, body: {
      profile_id: before.data.profile_id, ...(name === 'schedule' ? { period: before.period } : {})
    } });
    fence(); if (data.profile_id !== before.data.profile_id) throw new Error('binding');
    const after = await read();
    if (after.profile_id !== before.data.profile_id || !after.verified) throw new Error('verification');
    if (name === 'schedule') {
      const hours = Number(before.period.slice(0, -1)) * (before.period.endsWith('d') ? 24 : 1);
      if (after.interval_hours !== hours) throw new Error('readback');
    }
    set({ data: after, message: name === 'schedule' ? `Расписание включено: каждые ${after.interval_hours} ч. Настройки прочитаны обратно.` : 'Первая зашифрованная копия создана, загружена и проверена чтением из хранилища. Теперь выберите период.' });
  });
  ctx.onDispose(() => { disposed = true; owner = null; state.set({ ...state.get(), data: null }); });
  return { state, set, refresh, action };
}

export function SetupCheck({ setup, enabled }) {
  const s = sdk.useValue(setup.state);
  const button = (label, onClick, disabled = false) => jsx(sdk.Button, { type: 'button', onClick, disabled: s.busy || disabled, children: label });
  return jsxs('section', { 'aria-label': 'Проверка и расписание', 'aria-busy': s.busy, children: [
    jsx('h2', { children: '3. Проверка, первая копия и период' }),
    jsx('p', { children: 'Проверка создаёт полную копию выбранного профиля, шифрует её и проверяет загрузку чтением обратно. Большой профиль может потребовать много времени. Закрытие страницы не отменяет серверную операцию.' }),
    button('Обновить состояние настройки', setup.refresh),
    jsx('p', { children: s.data ? `Этап: ${s.data.stage}; хранилище: ${s.data.storage}; период: ${s.data.interval_hours} ч.` : 'Состояние не загружено' }),
    button('Проверить и создать первую копию', () => setup.action('check'), !enabled || !s.data || s.data.check_running || s.data.storage === 'local' || !['check', 'schedule', 'complete'].includes(s.data.stage)),
    s.busy && jsx('p', { role: 'status', children: 'Операция выполняется. Дождитесь результата; повторный запуск заблокирован.' }),
    s.data?.verified && jsxs('div', { children: [
      jsxs('label', { children: ['Период резервного копирования', jsx(sdk.Input ?? 'input', { value: s.period, disabled: s.busy, 'aria-describedby': 'finix-period-help', onChange: e => setup.set({ period: e.currentTarget.value }) })] }),
      jsx('p', { id: 'finix-period-help', children: 'Положительное целое число и h (часы) или d (дни), например 24h или 7d. Расписание изменится только после сохранения.' }),
      button('Сохранить и включить', () => setup.action('schedule'), !/^[1-9][0-9]*[hd]$/.test(s.period))
    ] }),
    jsx('p', { role: 'status', 'aria-live': 'polite', children: s.message })
  ] });
}
