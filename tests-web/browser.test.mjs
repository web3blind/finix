import test from 'node:test';
import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';
import { build } from 'esbuild';
import { execFileSync } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { generateKeyPair } from '../web/keys.mjs';
import { Decrypter } from 'age-encryption';

const htmlURL = new URL('../dist/keygen.html', import.meta.url).href;
const launch = () => chromium.launch({ executablePath: '/usr/bin/chromium', headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-background-networking'] });
const age = (args, input) => {
  try { return execFileSync('/usr/bin/age', args, { input, stdio: ['pipe', 'pipe', 'pipe'] }); }
  catch { throw new Error('age browser interoperability failed (details suppressed)'); }
};

async function waitStatus(page, pattern) {
  await page.waitForFunction(source => new RegExp(source).test(document.querySelector('#status').textContent), pattern);
}

test('standalone Chromium UI: save, copy, import, reset, no secret DOM/storage/logs, no network', async () => {
  const browser = await launch();
  const dir = await mkdtemp(join(process.env.TMPDIR, 'finix-browser-'));
  try {
    const context = await browser.newContext({ acceptDownloads: true });
    const page = await context.newPage();
    const outgoing = []; const logs = []; const errors = [];
    await context.route(/https?:\/\//, route => { outgoing.push(route.request().url()); return route.abort(); });
    page.on('console', message => logs.push(message.text()));
    page.on('pageerror', () => errors.push('page error'));
    await page.goto(htmlURL);
    assert.equal(await page.locator('#save').isDisabled(), true);
    await page.getByRole('button', { name: 'Создать новый ключ / Generate new key', exact: true }).click();
    await waitStatus(page, 'Save the private key');
    const recipient = await page.locator('#public').inputValue();
    assert.equal(recipient.startsWith('age1'), true);
    await page.locator('#copy').click();
    // Clipboard permissions vary for file URLs: success or accessible manual fallback are both valid.
    await waitStatus(page, 'Public key copied|Copy the selected public key manually');
    const downloadEvent = page.waitForEvent('download');
    await page.locator('#save').click();
    const download = await downloadEvent;
    const keyPath = join(dir, 'saved-key.txt'); await download.saveAs(keyPath);
    const saved = await readFile(keyPath, 'utf8');
    const secret = saved.split('\n').find(line => line.startsWith('AGE-SECRET-KEY-'));
    assert.equal(typeof secret === 'string', true);
    const derived = execFileSync('/usr/bin/age-keygen', ['-y', keyPath], { stdio: ['ignore', 'pipe', 'pipe'] }).toString().trim();
    assert.equal(derived === recipient, true);
    // A fixture encrypted by the actual age executable decrypts with the UI-generated key in Node.
    const fixture = age(['-r', recipient], 'browser generated key -> age -> Node');
    const decrypter = new Decrypter(); decrypter.addIdentity(secret);
    assert.equal(await decrypter.decrypt(fixture, 'text'), 'browser generated key -> age -> Node');
    await page.locator('#import').setInputFiles(keyPath);
    await waitStatus(page, 'Key matches');
    assert.equal((await page.locator('#verified').textContent()).includes('Verified: key matches'), true);
    await page.locator('#import').setInputFiles({ name: 'bad.txt', mimeType: 'text/plain', buffer: Buffer.from('invalid') });
    await waitStatus(page, 'Invalid key file');
    assert.equal((await page.locator('#verified').textContent()).includes('not verified'), true);
    const other = await generateKeyPair();
    await page.locator('#import').setInputFiles({ name: 'other.txt', mimeType: 'text/plain', buffer: Buffer.from(other.identity) });
    await waitStatus(page, 'Different key');
    await page.locator('#import').setInputFiles({ name: 'large.txt', mimeType: 'text/plain', buffer: Buffer.alloc(4097, 65) });
    await waitStatus(page, 'Invalid key file');
    await page.locator('#import').setInputFiles(keyPath); await waitStatus(page, 'Key matches');
    page.once('dialog', dialog => dialog.dismiss()); await page.locator('#generate').click();
    assert.equal((await page.locator('#public').inputValue()) === recipient, true);
    assert.equal((await page.locator('#verified').textContent()).includes('Verified: key matches'), true);
    page.once('dialog', dialog => dialog.accept()); await page.locator('#generate').click();
    await waitStatus(page, 'Save the private key');
    assert.equal((await page.locator('#public').inputValue()) === recipient, false);
    assert.equal((await page.locator('#verified').textContent()).includes('not verified'), true);
    await page.locator('#import').setInputFiles(keyPath); await waitStatus(page, 'Different key');
    const noLeak = await page.evaluate(secretValue => ({
      dom: !document.documentElement.outerHTML.includes(secretValue),
      fields: ![...document.querySelectorAll('input,textarea')].some(el => el.value.includes(secretValue)),
      storage: localStorage.length === 0 && sessionStorage.length === 0,
      resources: performance.getEntriesByType('resource').filter(entry => /^https?:/.test(entry.name)).length
    }), secret);
    assert.deepEqual(noLeak, { dom: true, fields: true, storage: true, resources: 0 });
    assert.equal(logs.some(line => line.includes(secret)), false);
    assert.equal(errors.length, 0);
    assert.equal(outgoing.length, 0);
    // Validate the CSP network barrier independently from the normal zero-request workflow.
    const blocked = await page.evaluate(async () => {
      try { await fetch('https://example.com/finix-csp-probe'); return false; } catch { return true; }
    });
    assert.equal(blocked, true);
    assert.equal(outgoing.length, 0);
    await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pagehide')));
    assert.equal(await page.locator('#public').inputValue(), '');
    assert.equal(await page.locator('#save').isDisabled(), true);
    await context.close();
  } finally { await browser.close(); await rm(dir, { recursive: true, force: true }); }
});

test('real Chromium crypto decrypts age CLI fixture, creates ciphertext age CLI decrypts', async () => {
  const browser = await launch();
  const dir = await mkdtemp(join(process.env.TMPDIR, 'finix-browser-interop-'));
  try {
    // QA harness is injected only in this isolated test page, never in the shipped HTML.
    const bundle = await build({ stdin: {
      contents: `import {generateKeyPair} from './web/keys.mjs'; import {Encrypter,Decrypter} from 'age-encryption';
      let pair; globalThis.qa = {
        async generate() { pair = await generateKeyPair(); return pair.recipient; },
        async decrypt(bytes) { const d = new Decrypter(); d.addIdentity(pair.identity); return d.decrypt(new Uint8Array(bytes), 'text'); },
        async encrypt(text) { const e = new Encrypter(); e.addRecipient(pair.recipient); return Array.from(await e.encrypt(text)); },
        download() { const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([pair.identity+'\\n'])); a.download='key.txt'; a.click(); }
      };`, resolveDir: new URL('../', import.meta.url).pathname
    }, bundle: true, write: false, platform: 'browser', format: 'iife' });
    const context = await browser.newContext({ acceptDownloads: true }); const page = await context.newPage();
    await page.goto(htmlURL);
    await page.evaluate(bundle.outputFiles[0].text);
    const recipient = await page.evaluate(() => qa.generate());
    const plain = 'real browser <-> age CLI: Finix';
    const encrypted = age(['-r', recipient], plain);
    assert.equal(await page.evaluate(bytes => qa.decrypt(bytes), Array.from(encrypted)), plain);
    const ciphertext = await page.evaluate(text => qa.encrypt(text), plain);
    const event = page.waitForEvent('download'); await page.evaluate(() => qa.download());
    const path = join(dir, 'key.txt'); await (await event).saveAs(path);
    assert.equal(age(['-d', '-i', path], Buffer.from(ciphertext)).toString(), plain);
    await context.close();
  } finally { await browser.close(); await rm(dir, { recursive: true, force: true }); }
});
