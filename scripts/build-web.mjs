import { build } from 'esbuild';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const root = new URL('../', import.meta.url);
const result = await build({
  entryPoints: [fileURLToPath(new URL('web/app.mjs', root))], bundle: true,
  write: false, format: 'iife', platform: 'browser', target: 'es2022',
  minify: true, legalComments: 'inline', metafile: true
});
const script = result.outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
const style = await readFile(new URL('web/style.css', root), 'utf8');
const hash = text => `'sha256-${createHash('sha256').update(text).digest('base64')}'`;
const csp = `default-src 'none'; script-src ${hash(script)}; style-src ${hash(style)}; connect-src 'none'; img-src 'none'; font-src 'none'; media-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; worker-src 'none'; frame-src 'none'`;
// Include complete notices for every bundled dependency, not just age itself.
const packages = [...new Set(Object.keys(result.metafile.inputs).flatMap(path => {
  const match = path.match(/node_modules\/((?:@[^/]+\/)?[^/]+)/);
  return match ? [match[1]] : [];
}))].sort();
const notices = [];
for (const name of packages) {
  notices.push(`${name}\n${await readFile(new URL(`node_modules/${name}/LICENSE`, root), 'utf8')}`);
}
const escape = text => text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
let html = await readFile(new URL('web/keygen.html', root), 'utf8');
html = html.replace('__CSP__', csp).replace('__STYLE__', style).replace('__LICENSES__', escape(notices.join('\n\n'))).replace('__SCRIPT__', () => script);
await mkdir(new URL('dist/', root), { recursive: true });
await writeFile(new URL('dist/keygen.html', root), html);
console.log(`Built dist/keygen.html (${Buffer.byteLength(html)} bytes); bundled notices: ${packages.join(', ')}`);
