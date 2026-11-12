import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {harness,preview} from './default-maintenance.test.mjs';
const actual=process.env.FINIX_DEFAULT_HTTP_PROJECTION?JSON.parse(await readFile(process.env.FINIX_DEFAULT_HTTP_PROJECTION,'utf8')):null;
const cap=actual?.default.body??{code_version:'default-recovery-v1',preview_contract:'default-preview-job-v1',supported:true,local_only:true,profile_id:'a'.repeat(64),serverRootToken:'f'.repeat(64),apply_enabled:true,active_shared_autoruns_supported:false};
const refusal=()=>{throw Error('409: '+JSON.stringify(actual?.ordinary.body??{detail:'HOST_IMPORT_CONTRACT_UNSUPPORTED'}));};
for(const bundle of [false,true])test(`default identity admits without ordinary import capability (${bundle?'bundle':'source'})`,async()=>{
 const h=await harness({bundle,rest:p=>p==='/recovery/default/capabilities'?cap:p==='/recovery/capabilities'?refusal():undefined});
 await h.maintenance.actions.load();
 assert.equal(h.maintenance.state.get().ready,true);
 assert.equal(h.maintenance.state.get().items.length,1);
 assert.deepEqual(h.calls.map(([p])=>p),['/recovery/default/capabilities','/recovery/catalog']);
 assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);
 h.dispose();
});
for(const bundle of [false,true]){
 test(`closed default identity rejects missing and foreign authority (${bundle?'bundle':'source'})`,async()=>{
  for(const patch of [{profile_id:undefined},{local_only:false},{supported:false},{code_version:'unknown'},{serverRootToken:'invalid'}]){
   const h=await harness({bundle,rest:path=>path==='/recovery/default/capabilities'?{...cap,...patch}:path==='/recovery/capabilities'?refusal():undefined});
   await h.start();assert.equal(h.maintenance.state.get().ready,false);assert.equal(h.calls.some(([p])=>p==='/recovery/catalog'),false);assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);h.dispose();
  }
 });
 test(`fresh reconnect refuses changed authority despite cached supported capability (${bundle?'bundle':'source'})`,async()=>{
  for(const patch of [{profile_id:'0'.repeat(64)},{local_only:false},{supported:false},{code_version:'unknown'},{serverRootToken:'0'.repeat(64)}]){
   let changed=false;const p=preview();p.apply_enabled=true;p.preview.root_binding=cap.serverRootToken;
   const h=await harness({bundle,preview:p,rest:path=>path==='/recovery/default/capabilities'?(changed?{...cap,...patch}:cap):path==='/recovery/capabilities'?refusal():undefined});
   await h.start();await h.finish();await h.maintenance.actions.apply(h.maintenance.state.get().preview);
   changed=true;h.epoch();const n=h.calls.length;await h.maintenance.actions.poll();
   assert.equal(h.calls.slice(n).some(([p])=>p.startsWith('/recovery/default/status/')),false);assert.equal(h.calls.slice(n).some(([,o])=>o?.method==='POST'),false);assert.equal(h.maintenance.state.get().busy,true);h.dispose();
  }
 });
 test(`late capability cannot outlive epoch or disposal (${bundle?'bundle':'source'})`,async()=>{
  for(const change of [h=>h.epoch(),h=>h.dispose(),h=>h.connectionId.set('remote')]){
   let resolve;const h=await harness({bundle,rest:path=>path==='/recovery/default/capabilities'?new Promise(r=>resolve=r):undefined});
   const pending=h.maintenance.actions.load();for(let i=0;i<20&&!resolve;i++)await Promise.resolve();assert.ok(resolve);change(h);resolve(cap);await pending;
   assert.equal(h.calls.some(([p])=>p==='/recovery/catalog'),false);assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);assert.equal(h.maintenance.state.get().ready,false);h.dispose();
  }
 });
 test(`exact existing job authenticates default identity despite ordinary 409 (${bundle?'bundle':'source'})`,async()=>{
  let outage=true;const p=preview();p.preview.root_binding=cap.serverRootToken;
  const job='9'.repeat(32);
  const h=await harness({bundle,preview:p,rest:path=>{
   if(path==='/recovery/default/capabilities')return cap;
   if(path==='/recovery/capabilities')return refusal();
   if(path===`/recovery/jobs/${job}`)return outage?Promise.reject(Error('read outage')):{id:job,profile_id:cap.profile_id,state:'succeeded',complete:true};
  }});
  await h.start();await h.maintenance.actions.poll();assert.equal(h.maintenance.state.get().checkExisting,true);
  outage=false;await h.maintenance.actions.checkExisting();assert.ok(h.maintenance.state.get().preview);
  assert.equal(h.calls.some(([p])=>p==='/recovery/capabilities'),false);
  assert.equal(h.calls.filter(([p])=>p.endsWith('/key')).length,1);
  assert.equal(h.calls.filter(([p])=>p==='/recovery/default/prepare-auto').length,1);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.find(([p])=>p==='/recovery/default/prepare')[1].body)),{profile_id:cap.profile_id,recovery_job:job,consent_prepare:true,preview_contract:'default-preview-job-v1'});
  h.dispose();
 });
 test(`expected STOP epoch retains custody and freshly authenticates default identity (${bundle?'bundle':'source'})`,async()=>{
  const p=preview();p.apply_enabled=true;p.preview.root_binding=cap.serverRootToken;
  const h=await harness({bundle,preview:p,rest:path=>path==='/recovery/default/capabilities'?cap:path==='/recovery/capabilities'?refusal():undefined});
  await h.start();await h.finish();assert.ok(h.maintenance.state.get().preview);
  await h.maintenance.actions.apply(h.maintenance.state.get().preview);
  const old=h.maintenance.view().actions,n=h.calls.length;h.epoch();
  await old.apply(p);await h.maintenance.actions.poll();
  assert.ok(h.calls.slice(n).some(([path])=>path===`/recovery/default/status/${p.id}`));
  assert.equal(h.calls.slice(n).some(([path])=>path==='/recovery/default/capabilities'),true);
  assert.equal(h.calls.slice(n).some(([,o])=>o?.method==='POST'),false);
  assert.equal(h.calls.some(([path])=>path==='/recovery/capabilities'),false);
  assert.equal(h.calls.filter(([path])=>path==='/recovery/default/apply').length,1);
  assert.equal(h.maintenance.view().value.confirmationConsumed,true);h.dispose();
 });
}
