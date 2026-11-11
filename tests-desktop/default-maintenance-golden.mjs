// Read a payload emitted by the actual generated-age HTTP test, unchanged.
import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
import {harness} from './default-maintenance.test.mjs';
const actual=JSON.parse(await readFile(process.argv[2],'utf8'));
import {dirname,join} from 'node:path';
const current=JSON.parse(await readFile(join(dirname(process.argv[2]),'actual-http-capabilities.json'),'utf8'));
const h=await harness({preview:actual,rest:path=>path==='/recovery/default/capabilities'?current:undefined});await h.start();await h.finish();
assert.equal(JSON.stringify(h.maintenance.state.get().preview),JSON.stringify(actual));
await h.maintenance.actions.apply(actual);
assert.equal(h.calls.some(([p])=>p==='/recovery/default/apply'),false);
console.log('ACTUAL_HTTP_PREVIEW_CONSUMED_WITH_PUBLIC_GATE_CLOSED');
