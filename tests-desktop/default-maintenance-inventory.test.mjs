import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile,readdir} from 'node:fs/promises';
test('every shipped Desktop source is in native allowlist and wheel data declarations',async()=>{
 const allow=JSON.parse(await readFile(new URL('../scripts/release/allowlist.json',import.meta.url),'utf8'));
 const py=await readFile(new URL('../pyproject.toml',import.meta.url),'utf8');
 for(const n of await readdir(new URL('../desktop/src/',import.meta.url))){if(!n.endsWith('.mjs'))continue;const p=`desktop/src/${n}`;assert.ok(allow.includes(p),`native payload missing ${p}`);assert.ok(py.includes(`"${p}"`),`wheel data missing ${p}`);}
});
