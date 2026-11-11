import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {harness} from './recovery.test.mjs';
const actual=JSON.parse(await readFile(new URL('./fixtures/operation-http.json',import.meta.url),'utf8'));
test('shipped bundle consumes unchanged genuine generated-age async HTTP/native READY receipt',async()=>{
 const h=await harness({rest:p=>p==='/recovery/capabilities'?{code_version:'recovery-v1',operation_protocol:'recovery-operations-v1',profile_id:actual.profile_id,local_only:true}:p==='/recovery/operations/current'?actual:undefined,routes:()=>[{connectionId:'local',profile:actual.name,targetProfile:actual.name,mode:'local'}]});
 assert.match(h.text(),/Восстановленный профиль открыт/);assert.equal(h.calls.filter(([p])=>p==='activate').length,1);
 const index=h.calls.findIndex(([p])=>p==='activate');assert.equal(h.calls.slice(index+1).some(([p])=>p.startsWith('/recovery')),false);
 assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);assert.equal(actual.receipt.history.stored_logical,3);
});
test('shipped bundle initial three controls and one intent/key with no legacy mutation',async()=>{const h=await harness();assert.equal(h.nodes().filter(x=>x.type==='select').length,1);assert.equal(h.nodes().filter(x=>x.type==='input').length,1);assert.equal(h.nodes().filter(x=>x.type==='button').length,1);await h.key();await h.click('Восстановить');await h.tick();assert.match(h.text(),/Восстановленный профиль открыт/);assert.equal(h.calls.filter(([p])=>p==='/recovery/operations').length,1);assert.equal(h.calls.filter(([p])=>p.includes('/key?')).length,1);assert.equal(h.calls.some(([p])=>/prepare-auto|import-auto|\/start$/.test(p)),false);const upload=h.calls.find(([p])=>p.includes('/key?'))[1].upload;assert.ok(new Uint8Array(upload.bytes).every(x=>x===0));});
