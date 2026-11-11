import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {harness} from './recovery.test.mjs';
// Unchanged public terminal projection from real generated-age async HTTP/native READY.
const fixture=JSON.parse(await readFile(new URL('./fixtures/operation-http.json',import.meta.url),'utf8'));
test('shipped client consumes exact real generated-age async operation receipt',async()=>{
 const h=await harness({rest:p=>p==='/recovery/capabilities'?{code_version:'recovery-v1',operation_protocol:'recovery-operations-v1',profile_id:fixture.profile_id,local_only:true}:p==='/recovery/operations/current'?fixture:undefined,routes:()=>[{connectionId:'local',profile:fixture.name,targetProfile:fixture.name,mode:'local'}]});
 // Reopening attaches the actual existing receipt; it may not create another begin/key/import/start.
 assert.match(h.text(),/Восстановленный профиль открыт/);
 assert.equal(h.calls.filter(([p])=>p==='/recovery/operations/current').length,1);
 assert.equal(h.calls.filter(([p])=>p==='activate').length,1);
 assert.equal(h.calls.filter(([p])=>p==='describe').length,1);
 assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);
 assert.deepEqual(h.calls.find(([p])=>p==='activate').slice(1),['local',fixture.name]);
 assert.equal(h.calls.find(([p])=>p==='describe')[1].targetProfile,fixture.name);
 assert.equal(h.calls.some(([p])=>/prepare-auto|import-auto|\/start$/.test(p)),false);
});
