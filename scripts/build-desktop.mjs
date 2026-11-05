import { build } from 'esbuild';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
const root = new URL('../', import.meta.url);
const result = await build({
  entryPoints: [new URL('desktop/src/plugin.mjs', root).pathname],
  bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022',
  external: ['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'],
  legalComments: 'inline', metafile: true
});
const dependencies = [...new Set(Object.keys(result.metafile.inputs).flatMap(path => {
  const match = path.match(/node_modules\/((?:@[^/]+\/)?[^/]+)/);
  return match ? [match[1]] : [];
}))].sort();
const notices = await Promise.all(dependencies.map(async name => `${name}\n${await readFile(new URL(`node_modules/${name}/LICENSE`, root), 'utf8')}`));
const output = `${result.outputFiles[0].text}\n/* Bundled dependency notices\n${notices.join('\n\n').replaceAll('*/', '* /')}\n*/\n`;
await mkdir(new URL('desktop/', root), { recursive: true });
await writeFile(new URL('desktop/plugin.js', root), output);
console.log(`Built desktop/plugin.js (${Buffer.byteLength(output)} bytes). Bundled: ${dependencies.join(', ')}; only SDK/React imports remain.`);
