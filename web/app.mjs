import { createKeySession } from './keys.mjs';

const session = createKeySession();
const $ = id => document.getElementById(id);
let busy = false;
let downloadURL = null;
const revoke = () => { if (downloadURL) URL.revokeObjectURL(downloadURL); downloadURL = null; };
const status = text => { $('status').textContent = text; };
function render() {
  $('public').value = session.recipient ?? '';
  $('generate').disabled = busy;
  for (const id of ['save', 'copy', 'import']) $(id).disabled = busy || !session.recipient;
  $('verified').textContent = session.verified ? 'Проверено: ключ совпадает. / Verified: key matches.' : 'Сохранённый ключ ещё не проверен. / Saved key not verified.';
}
$('generate').addEventListener('click', async () => {
  if (session.recipient && !window.confirm('Заменить ключ? Сначала сохраните старый. / Replace key? Save the old key first.')) return;
  revoke(); $('import').value = ''; busy = true;
  const pending = session.generate(); render(); status('Генерация… / Generating…');
  try { await pending; status('Сохраните приватный ключ, затем выберите сохранённый файл для проверки. / Save the private key, then select that saved file to verify.'); }
  catch { session.clear(); status('Не удалось создать ключ. Нужен современный браузер с Web Crypto. / Key generation failed. Use a modern browser with Web Crypto.'); }
  finally { busy = false; render(); }
});
$('save').addEventListener('click', () => {
  revoke();
  const blob = new Blob([session.downloadText()], { type: 'text/plain;charset=utf-8' });
  downloadURL = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = downloadURL; link.download = 'finix-recovery-key.txt'; link.click();
  // Revoke after the download has begun; never retain old key blobs indefinitely.
  const url = downloadURL;
  setTimeout(() => { URL.revokeObjectURL(url); if (downloadURL === url) downloadURL = null; }, 1000);
  status('Загрузка запрошена, но сохранение ещё не проверено. / Download requested; saving is not yet verified.');
});
$('copy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(session.recipient); status('Публичный ключ скопирован. / Public key copied.'); }
  catch { $('public').focus(); $('public').select(); status('Скопируйте выделенный публичный ключ вручную. / Copy the selected public key manually.'); }
});
$('import').addEventListener('change', async () => {
  const file = $('import').files[0];
  $('import').value = '';
  if (!file) return;
  busy = true; render();
  try { status(await session.verify(file) ? 'Ключ совпадает. Храните файл в безопасном месте. / Key matches. Keep the file safe.' : 'Другой ключ: файл не совпадает. / Different key: file does not match.'); }
  catch { status('Неверный файл ключа (максимум 4096 байт). / Invalid key file (maximum 4096 bytes).'); }
  finally { busy = false; render(); }
});
window.addEventListener('pagehide', () => { revoke(); session.clear(); $('import').value = ''; render(); status('Ключ удалён из состояния страницы. / Key cleared from page state.'); });
render();
