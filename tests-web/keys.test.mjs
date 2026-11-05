import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { generateKeyPair, createKeySession, recipientFromKeyFile, validateRecipient } from '../web/keys.mjs';
import { Encrypter, Decrypter } from 'age-encryption';

const asFile = text => new File([text], 'key.txt');
const scratch = process.env.TMPDIR;
if (!scratch) throw new Error('TMPDIR is required for isolated secret fixtures');

// Never put secret-bearing values into assertion diagnostics or subprocess errors.
function age(args, input) {
  try { return execFileSync('/usr/bin/age', args, { input, stdio: ['pipe', 'pipe', 'pipe'] }); }
  catch { throw new Error('age interoperability command failed (details suppressed)'); }
}

test('generated X25519 key interoperates with /usr/bin/age in both directions', async () => {
  const dir = await mkdtemp(join(scratch, 'finix-node-'));
  try {
    const { identity, recipient } = await generateKeyPair();
    assert.equal(validateRecipient(recipient), true);
    const path = join(dir, 'identity.txt');
    await writeFile(path, identity + '\n', { mode: 0o600 });
    const plain = 'Finix real age fixture: русский текст\n';
    const encrypted = age(['-r', recipient], plain);
    const decrypt = new Decrypter(); decrypt.addIdentity(identity);
    assert.equal(await decrypt.decrypt(encrypted, 'text'), plain);
    const encrypt = new Encrypter(); encrypt.addRecipient(recipient);
    assert.equal(age(['-d', '-i', path], await encrypt.encrypt(plain)).toString(), plain);
    const derived = execFileSync('/usr/bin/age-keygen', ['-y', path], { stdio: ['ignore', 'pipe', 'pipe'] }).toString().trim();
    assert.equal(derived === recipient, true);
    const wrong = new Decrypter(); wrong.addIdentity((await generateKeyPair()).identity);
    await assert.rejects(() => wrong.decrypt(encrypted));
    const damaged = encrypted.slice(); damaged[damaged.length - 1] ^= 1;
    await assert.rejects(() => decrypt.decrypt(damaged));
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('recipient validation checks age encoding and checksum, not only prefix', async () => {
  const { recipient } = await generateKeyPair();
  assert.equal(validateRecipient(recipient), true);
  for (const value of [null, '', 'age1fake', recipient.toUpperCase(), recipient.slice(0, -1) + (recipient.endsWith('q') ? 'p' : 'q'), recipient + '\n']) {
    assert.equal(validateRecipient(value), false);
  }
});

test('bounded local file parsing accepts age comments and rejects invalid inputs without echoing them', async () => {
  const { identity, recipient } = await generateKeyPair();
  assert.equal(await recipientFromKeyFile(asFile(`# comment\r\n${identity}\r\n`)) === recipient, true);
  for (const input of ['', '# comment only', 'not a key', identity + '\n' + identity, identity.slice(0, -1) + (identity.endsWith('Q') ? 'P' : 'Q'), 'x'.repeat(4097)]) {
    let rejected = false;
    try { await recipientFromKeyFile(asFile(input)); } catch (error) {
      rejected = true;
      assert.equal(String(error).includes(identity), false);
    }
    assert.equal(rejected, true);
  }
  let read = false;
  await assert.rejects(() => recipientFromKeyFile({ size: 4097, text() { read = true; throw new Error(); } }));
  assert.equal(read, false);
});

test('saved-file comparison, mismatches, malformed retries, replacement and clearing reset state', async () => {
  const session = createKeySession();
  assert.throws(() => session.downloadText());
  await session.generate();
  const original = session.recipient;
  const saved = asFile(session.downloadText());
  assert.equal(await session.verify(saved), true);
  assert.equal(session.verified, true);
  await assert.rejects(() => session.verify(asFile('invalid')));
  assert.equal(session.verified, false);
  const other = await generateKeyPair();
  assert.equal(await session.verify(asFile(other.identity)), false);
  await session.generate();
  assert.notEqual(session.recipient, original);
  assert.equal(session.verified, false);
  assert.equal(await session.verify(saved), false);
  session.clear();
  assert.equal(session.recipient, null);
  assert.equal(session.verified, false);
  assert.throws(() => session.downloadText());
});

test('in-flight verification and generation cannot restore cleared state', async () => {
  const session = createKeySession();
  await session.generate();
  const text = session.downloadText();
  let finish;
  const pending = session.verify({ size: text.length, text: () => new Promise(resolve => { finish = resolve; }) });
  session.clear(); finish(text);
  assert.equal(await pending, false);
  assert.equal(session.verified, false);
  const generated = session.generate(); session.clear();
  assert.equal(await generated, null);
  assert.equal(session.recipient, null);
});
