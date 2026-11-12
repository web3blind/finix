import test from 'node:test';
import assert from 'node:assert/strict';
import {harness,preview} from './default-maintenance.test.mjs';
const turn=async()=>{for(let i=0;i<40;i++)await Promise.resolve();};
test('owned pending Apply survives pure epoch, invalidates old callbacks and reads only exact status',async()=>{
 let ack;const p=preview();p.apply_enabled=true;
 const h=await harness({preview:p,reviewGate:true,rest:path=>path==='/recovery/default/apply'?new Promise(r=>ack=r):undefined});
 await h.start();await h.finish();const old=h.maintenance.view().actions;const run=old.apply(h.maintenance.state.get().preview);await turn();assert.ok(ack);
 h.epoch();assert.equal(h.maintenance.state.get().busy,true);assert.equal(h.maintenance.state.get().polling,true);assert.equal(h.maintenance.state.get().preview,null);
 await old.apply(p);await old.cancel();ack({id:p.id,state:'ready'});await run;
 await h.maintenance.actions.poll();assert.equal(h.calls.filter(([x])=>x==='/recovery/default/apply').length,1);
 assert.ok(h.calls.some(([x])=>x===`/recovery/default/status/${p.id}`));assert.equal(h.maintenance.view().value.confirmationConsumed,true);
});
const owned=async(opts={})=>{const p=preview();p.apply_enabled=true;const h=await harness({preview:p,reviewGate:true,...opts});await h.start();await h.finish();await h.maintenance.actions.apply(h.maintenance.state.get().preview);return h;};
const posts=h=>h.calls.filter(([,o])=>o?.method==='POST').length;
test('foreign physical root or logical source refuses before exact status; lock survives',async()=>{
 for(const field of ['root','source']){let changed=false;const h=await owned({rest:path=>{
  if(changed&&field==='root'&&path==='/recovery/default/capabilities')return {code_version:'default-recovery-v1',supported:true,serverRootToken:'1'.repeat(64)};
  if(changed&&field==='source'&&path==='/recovery/default/capabilities')return {code_version:'default-recovery-v1',supported:true,serverRootToken:'f'.repeat(64),local_only:true,profile_id:'1'.repeat(64)};
 }});const n=h.calls.length,post=posts(h);changed=true;h.epoch();await h.maintenance.actions.poll();await h.maintenance.actions.load();await h.maintenance.actions.apply(preview());
 assert.equal(h.calls.slice(n).some(([p])=>p.startsWith('/recovery/default/status/')),false);assert.equal(posts(h),post);assert.equal(h.maintenance.state.get().busy,true);assert.equal(h.maintenance.state.get().polling,false);}
});
test('foreign device registry, remote route and profile scope never reconnect owned operation',async()=>{
 for(const change of [h=>{h.sdk.host.connections=async()=>[{id:'local',kind:'local',device:'foreign'}];h.epoch();},h=>h.connectionId.set('remote'),h=>h.change()]){
  const h=await owned();const n=h.calls.length,post=posts(h);change(h);await h.maintenance.actions.poll();await h.maintenance.actions.load();assert.equal(h.calls.slice(n).some(([p])=>p.startsWith('/recovery/default/status/')),false);assert.equal(posts(h),post);assert.equal(h.maintenance.state.get().busy,true);
 }
});
test('unknown IPC or numeric HTTP error goes to locked truthful manual fallback, never retries',async()=>{
 for(const error of [Error('untyped IPC'),Object.assign(Error('temporarily unavailable'),{status:503})]){
  let down=false;const h=await owned({rest:path=>{if(down&&path==='/recovery/default/capabilities')throw error;}});down=true;h.epoch();const post=posts(h);await h.maintenance.actions.poll();const n=h.calls.length;for(let i=0;i<8;i++){await h.maintenance.actions.poll();await h.maintenance.actions.load();}assert.equal(h.calls.length,n);assert.equal(posts(h),post);assert.equal(h.maintenance.state.get().busy,true);assert.match(h.maintenance.state.get().message,/reopen Finix/i);
 }
});
test('dispose and late Apply ACK cannot resurrect verification, consent or requests',async()=>{
 let ack;const p=preview();p.apply_enabled=true;const h=await harness({preview:p,reviewGate:true,rest:path=>path==='/recovery/default/apply'?new Promise(r=>ack=r):undefined});await h.start();await h.finish();const old=h.maintenance.view().actions;const run=old.apply(h.maintenance.state.get().preview);await turn();h.dispose();const n=h.calls.length;ack({state:'ready'});await run;await old.apply(p);await old.poll();assert.equal(h.calls.length,n);assert.notEqual(h.maintenance.state.get().verified,true);
});
test('pending key ACK disables cancellation and never releases mutation owner',async()=>{
 let keyAck;const h=await harness({rest:path=>path.endsWith('/key')?new Promise(r=>keyAck=r):path.endsWith('/cancel')?Promise.reject(Error('lost cancel')):undefined});await h.maintenance.actions.load();h.maintenance.actions.key({size:8,arrayBuffer:async()=>new ArrayBuffer(8)});const run=h.maintenance.actions.prepare({candidate:'c'.repeat(64)});await turn();assert.ok(keyAck);await h.maintenance.actions.cancel();const n=posts(h);await h.maintenance.actions.prepare({candidate:'c'.repeat(64)});assert.equal(posts(h),n);assert.equal(h.maintenance.state.get().busy,true);keyAck({id:'9'.repeat(32),profile_id:'a'.repeat(64),state:'running'});await run;assert.equal(h.maintenance.state.get().busy,true);
});
test('failed cancel remains busy and cannot dispatch another prepare or key',async()=>{const h=await harness({rest:p=>p.endsWith('/cancel')?Promise.reject(Error('lost cancel')):undefined});await h.start();await h.maintenance.actions.cancel();assert.equal(h.calls.filter(([p])=>p.endsWith('/cancel')).length,1);const n=posts(h);await h.maintenance.actions.prepare({candidate:'c'.repeat(64)});await h.maintenance.actions.load();assert.equal(posts(h),n);assert.equal(h.maintenance.state.get().busy,true);assert.equal(h.maintenance.state.get().cancellable,false);});
test('transport disconnect without a new epoch also revokes callbacks and preserves one Apply',async()=>{const h=await owned();const old=h.maintenance.view().actions,n=posts(h);h.sdk.host.state.gateway.set('closed');assert.equal(h.maintenance.state.get().busy,true);assert.equal(h.maintenance.state.get().preview,null);await old.cancel();await old.apply(preview());assert.equal(posts(h),n);});
test('same physical root current READY at new epoch requires fresh stock description/history reads',async()=>{
 let ready=false;const h=await owned({sdkRoutes:[{mode:'local',connectionId:'local',profile:'default',targetProfile:'default'}],sdkRequest:async(_r,m)=>m==='profiles.describe'?{name:'default'}:{scope:'stored',sessions:[]},rest:path=>ready&&path.startsWith('/recovery/default/status/')?{id:'b'.repeat(32),state:'ready',error:null}:undefined});ready=true;h.epoch();const n=h.calls.length;await h.maintenance.actions.poll();assert.equal(h.maintenance.state.get().verified,true);assert.ok(h.calls.slice(n).some(([p])=>p==='sdk:session.list'));assert.equal(h.calls.slice(n).some(([,o])=>o?.method==='POST'),false);h.epoch();assert.equal(h.maintenance.state.get().verified,false,'epoch invalidates old READY/history proof immediately');
});
