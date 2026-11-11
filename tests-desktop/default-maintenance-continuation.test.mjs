import test from 'node:test';
import assert from 'node:assert/strict';
import {harness} from './default-maintenance.test.mjs';
const job='9'.repeat(32),profile='a'.repeat(64);
test('GET-only transient failure exposes same in-memory preparation check and reaches first preview once',async()=>{
 let fail=true;
 const h=await harness({rest:p=>p===`/recovery/jobs/${job}`?(fail?Promise.reject(Error('unavailable')):{id:job,profile_id:profile,state:'succeeded',complete:true,archive_verified:true}):undefined});
 await h.start();await h.maintenance.actions.poll();
 assert.equal(h.maintenance.state.get().checkExisting,true);
 fail=false;await h.maintenance.actions.checkExisting();await h.maintenance.actions.checkExisting();
 assert.ok(h.maintenance.state.get().preview);
 assert.equal(h.calls.filter(([p])=>p==='/recovery/default/prepare-auto').length,1);
 assert.equal(h.calls.filter(([p])=>p.endsWith('/key')).length,1);
 assert.equal(h.calls.filter(([p])=>p==='/recovery/default/prepare').length,1);
});
test('existing check refuses fresh foreign source or root before job read or preview',async()=>{
 for(const boundary of ['source','root']){
  let changed=false;
  const h=await harness({rest:p=>{
   if(p===`/recovery/jobs/${job}`)return Promise.reject(Error('unavailable'));
   if(changed&&p===(boundary==='source'?'/recovery/capabilities':'/recovery/default/capabilities'))return boundary==='source'?{code_version:'recovery-v1',local_only:true,profile_id:'0'.repeat(64)}:{code_version:'default-recovery-v1',supported:true,serverRootToken:'0'.repeat(64)};
  }});
  await h.start();await h.maintenance.actions.poll();changed=true;await h.maintenance.actions.checkExisting();
  assert.equal(h.calls.filter(([p])=>p===`/recovery/jobs/${job}`).length,1);
  assert.equal(h.calls.some(([p])=>p==='/recovery/default/prepare'),false);
 }
});
test('unknown first preview ACK never permits existing check or preview replay',async()=>{
 let fail=true;
 const h=await harness({rest:p=>p===`/recovery/jobs/${job}`?(fail?Promise.reject(Error('unavailable')):{id:job,profile_id:profile,state:'succeeded',complete:true}):p==='/recovery/default/prepare'?Promise.reject(Error('unknown')):undefined});
 await h.start();await h.maintenance.actions.poll();fail=false;await h.maintenance.actions.checkExisting();await h.maintenance.actions.checkExisting();await h.maintenance.actions.poll();
 assert.equal(h.calls.filter(([p])=>p==='/recovery/default/prepare').length,1);
 assert.equal(h.maintenance.state.get().checkExisting,false);
});
test('changed connection cannot continue existing preparation',async()=>{
 const h=await harness({rest:p=>p===`/recovery/jobs/${job}`?Promise.reject(Error('unavailable')):undefined});await h.start();await h.maintenance.actions.poll();const a=h.maintenance.view().actions;h.connectionId.set('foreign');await a.checkExisting();
 assert.equal(h.calls.some(([p])=>p==='/recovery/default/prepare'),false);
});
test('actual succeeded job field projection reaches first preview through original intent',async()=>{
 const actual={id:'be4401b6e2884c0abe66fc7eb4aa29e6',profile_id:'ecc03f1ca6cc3914307ce375a2f0392ead3f3858a6054f4c3fb523654c454e70',state:'succeeded',complete:true,archive_verified:true};
 let fail=true;
 const h=await harness({rest:p=>{
  if(p==='/recovery/capabilities')return {code_version:'recovery-v1',profile_id:actual.profile_id,local_only:true};
  if(p==='/recovery/catalog')return {profile_id:actual.profile_id,items:[{id:'c'.repeat(64),complete:true}]};
  if(p==='/recovery/default/prepare-auto')return {...actual,state:'awaiting_key'};
  if(p.endsWith('/key'))return {...actual,state:'running'};
  if(p===`/recovery/jobs/${actual.id}`)return fail?Promise.reject(Error('unavailable')):actual;
 }});
 await h.start();await h.maintenance.actions.poll();fail=false;await h.maintenance.actions.checkExisting();assert.ok(h.maintenance.state.get().preview);
 assert.equal(h.calls.find(([p])=>p==='/recovery/default/prepare')[1].body.recovery_job,actual.id);
});
test('changed registry routing authority refuses existing check',async()=>{
 const h=await harness({rest:p=>p===`/recovery/jobs/${job}`?Promise.reject(Error('unavailable')):undefined});await h.start();await h.maintenance.actions.poll();h.sdk.host.connections=async()=>[{id:'local',kind:'remote'}];await h.maintenance.actions.checkExisting();
 assert.equal(h.calls.some(([p])=>p==='/recovery/default/prepare'),false);
});
test('fresh authentication GET outage remains user-checkable without mutations',async()=>{
 let failJob=true,failCap=false;
 const h=await harness({rest:p=>{
  if(p===`/recovery/jobs/${job}`)return failJob?Promise.reject(Error('unavailable')):{id:job,profile_id:profile,state:'succeeded',complete:true};
  if(failCap&&p==='/recovery/default/capabilities')return Promise.reject(Error('unavailable'));
 }});
 await h.start();await h.maintenance.actions.poll();failCap=true;await h.maintenance.actions.checkExisting();assert.equal(h.maintenance.state.get().checkExisting,true);
 failCap=false;failJob=false;await h.maintenance.actions.checkExisting();assert.ok(h.maintenance.state.get().preview);
 assert.equal(h.calls.filter(([p])=>p==='/recovery/default/prepare').length,1);
});
