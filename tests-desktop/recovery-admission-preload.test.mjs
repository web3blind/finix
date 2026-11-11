import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {harness,defer} from './recovery.test.mjs';
const captured=JSON.parse(await readFile(new URL('./fixtures/admission-preload.json',import.meta.url),'utf8'));
test('captured native preload capability/null-current/catalog admits the initial restore form without dispatch',async()=>{
 const h=await harness({registry:()=>captured.connections,rest:p=>{
  if(p==='/recovery/capabilities')return captured.readOnly.capabilities;
  if(p==='/recovery/operations/current')return captured.readOnly['operations/current'];
  if(p==='/recovery/catalog')return captured.readOnly.catalog;
 }});
 assert.equal(h.nodes().find(n=>n.type==='select').props.disabled,false,h.text());
 assert.equal(h.nodes().find(n=>n.type==='input').props.disabled,false);
 assert.equal(h.nodes().filter(n=>n.type==='option').length,captured.readOnly.catalog.items.length);
 assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);
 assert.equal(h.calls.some(([p])=>p==='activate'),false);
});
for(const [name,patch,registry] of [
 ['missing protocol',f=>{delete f.readOnly.capabilities.operation_protocol;}],
 ['foreign catalog',f=>{f.readOnly.catalog.profile_id='f'.repeat(64);}],
 ['remote registry',()=>{},()=>[{id:'local',kind:'remote'}]],
 ['duplicate local registry',()=>{},()=>[...captured.connections,...captured.connections]],
])test('captured admission refuses '+name+' before key or operation dispatch',async()=>{
 const f=structuredClone(captured);patch(f);
 const h=await harness({registry:registry??(()=>f.connections),rest:p=>p==='/recovery/capabilities'?f.readOnly.capabilities:p==='/recovery/operations/current'?f.readOnly['operations/current']:p==='/recovery/catalog'?f.readOnly.catalog:undefined});
 assert.equal(h.nodes().find(n=>n.type==='select').props.disabled,true);
 assert.equal(h.nodes().find(n=>n.type==='input').props.disabled,true);
 assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);
 assert.equal(h.calls.some(([p])=>p==='activate'),false);
});
test('captured delayed catalog cannot cross source or activation epoch fence',async()=>{
 const pending=defer();const h=await harness({registry:()=>captured.connections,rest:p=>p==='/recovery/capabilities'?captured.readOnly.capabilities:p==='/recovery/operations/current'?null:p==='/recovery/catalog'?pending.promise:undefined});
 assert.ok(h.calls.some(([p])=>p==='/recovery/catalog'));
 h.switch('remote');pending.resolve(captured.readOnly.catalog);await h.settle();
 assert.equal(h.nodes().find(n=>n.type==='select').props.disabled,true);
 assert.equal(h.nodes().find(n=>n.type==='input').props.disabled,true);
 assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);
});
