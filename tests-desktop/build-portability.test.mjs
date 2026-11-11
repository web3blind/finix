import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

for (const name of ['desktop', 'web']) {
  test(`${name} build accepts native file paths on the current platform`, () => {
    const result = spawnSync(process.execPath, [fileURLToPath(new URL(`../scripts/build-${name}.mjs`, import.meta.url))], {
      cwd: fileURLToPath(new URL('../', import.meta.url)), encoding: 'utf8'
    });
    assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  });
}
