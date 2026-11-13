import test from 'node:test';
import assert from 'node:assert/strict';
import {harness,preview} from './default-maintenance.test.mjs';
test('preview status GET failure keeps exact-id observation without replaying mutations',async()=>{
 let reads=0;const job='9'.repeat(32);
 const h=await harness({rest:(p,o)=>{
  if(p===`/recovery/jobs/${job}`)return {id:job,profile_id:'a'.repeat(64),state:'succeeded',complete:true};
  if(p==='/recovery/default/prepare')return {contract:'default-preview-job-v1',id:job,state:'running',error:null,result:null,cancellable:true};
  if(p===`/recovery/default/preview/${job}`){if(++reads===1)throw Error('Timed out connecting to backend');return {contract:'default-preview-job-v1',id:job,state:'succeeded',error:null,result:preview(),cancellable:false};}
 }});
 await h.start();await h.maintenance.actions.poll();
 assert.equal(h.maintenance.state.get().polling,true);
 await h.maintenance.actions.poll();
 assert.equal(h.maintenance.state.get().polling,true,'a failed GET must not strand the unchanged owner');
 await h.maintenance.actions.poll();assert.ok(h.maintenance.state.get().preview);
 for(const p of ['/recovery/default/prepare-auto',`/recovery/jobs/${job}/key`,'/recovery/default/prepare'])assert.equal(h.calls.filter(([path,o])=>path===p&&o?.method==='POST').length,1);
 assert.equal(h.calls.some(([p])=>p==='/recovery/default/apply'),false);
});
