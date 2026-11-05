import { generateX25519Identity, identityToRecipient, Encrypter } from 'age-encryption';

export const MAX_KEY_FILE_BYTES = 4096;

export function validateRecipient(recipient) {
  try {
    if (typeof recipient !== 'string' || !/^age1[0-9a-z]{58}$/.test(recipient)) return false;
    new Encrypter().addRecipient(recipient);
    return true;
  } catch { return false; }
}

export async function generateKeyPair() {
  if (!globalThis.crypto?.getRandomValues) throw new Error('Secure random source unavailable');
  const identity = await generateX25519Identity();
  const recipient = await identityToRecipient(identity);
  if (!validateRecipient(recipient)) throw new Error('Invalid recipient');
  return { identity, recipient };
}

export async function recipientFromKeyFile(file) {
  if (!file || file.size < 1 || file.size > MAX_KEY_FILE_BYTES) throw new Error('Invalid key file size');
  const text = await file.text();
  if (new TextEncoder().encode(text).length > MAX_KEY_FILE_BYTES) throw new Error('Invalid key file size');
  const lines = text.split(/\r?\n/).map(line => line.trim()).filter(line => line && !line.startsWith('#'));
  if (lines.length !== 1 || !/^AGE-SECRET-KEY-1[0-9A-Z]{58}$/.test(lines[0])) throw new Error('Invalid key file');
  try {
    const recipient = await identityToRecipient(lines[0]);
    if (!validateRecipient(recipient)) throw new Error();
    return recipient;
  } catch { throw new Error('Invalid key file'); }
}

export function createKeySession() {
  let pair = null;
  let verified = false;
  let revision = 0;
  return {
    clear() { revision++; pair = null; verified = false; },
    async generate() {
      const current = ++revision;
      pair = null; verified = false;
      const next = await generateKeyPair();
      if (current !== revision) return null;
      pair = next;
      return next.recipient;
    },
    get recipient() { return pair?.recipient ?? null; },
    get verified() { return verified; },
    downloadText() {
      if (!pair) throw new Error('Generate a key first');
      return `# Finix age recovery key — KEEP PRIVATE\n# public key: ${pair.recipient}\n${pair.identity}\n`;
    },
    async verify(file) {
      verified = false;
      if (!pair) throw new Error('Generate a key first');
      const current = revision;
      const recipient = await recipientFromKeyFile(file);
      if (current !== revision) return false;
      verified = recipient === pair.recipient;
      return verified;
    }
  };
}
