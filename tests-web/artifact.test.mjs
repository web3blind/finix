import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';

test('single-file artifact has exact hash-only CSP, inline assets and bundled license notices', async () => {
  const html = await readFile(new URL('../dist/keygen.html', import.meta.url), 'utf8');
  const csp = html.match(/http-equiv="Content-Security-Policy" content="([^"]+)"/)[1];
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  const style = html.match(/<style>([\s\S]*?)<\/style>/)[1];
  const hash = text => `'sha256-${createHash('sha256').update(text).digest('base64')}'`;
  assert.equal(csp.includes(`script-src ${hash(script)};`), true);
  assert.equal(csp.includes(`style-src ${hash(style)};`), true);
  for (const directive of ['default-src', 'connect-src', 'img-src', 'font-src', 'media-src', 'object-src', 'base-uri', 'form-action', 'worker-src', 'frame-src']) {
    assert.equal(csp.includes(`${directive} 'none'`), true);
  }
  assert.equal(/unsafe-inline|unsafe-eval/.test(csp), false);
  assert.equal(/<(?:script|link|img|iframe)\b[^>]*(?:src|href)=/i.test(html), false);
  assert.equal(/__[A-Z]+__/.test(html), false);
  assert.equal(html.includes('Copyright 2023 The age Authors'), true);
  assert.equal(html.includes('Redistribution and use in source and binary forms'), true);
  assert.equal(html.includes('Permission is hereby granted'), true);
});
