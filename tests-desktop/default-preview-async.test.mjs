import test from 'node:test';
import assert from 'node:assert/strict';
import {harness,preview} from './default-maintenance.test.mjs';
const job='9'.repeat(32);
const envelope=(state,result=null)=>({contract:'default-preview-job-v1',id:job,state,error:null,result,cancellable:state==='running'});
test('shipped consumer refuses legacy synchronous capability before key reading',async()=>{
 const h=await harness({bundle:true,rest:path=>path==='/recovery/default/capabilities'?{code_version:'default-recovery-v1',supported:true,apply_enabled:true,active_shared_autoruns_supported:false,serverRootToken:'f'.repeat(64)}:undefined});
 await h.start();assert.equal(h.calls.some(([,opts])=>opts?.method==='POST'),false);assert.equal(h.maintenance.state.get().key,false);
});
test('reopening async preview uses known GET only and cannot renew Apply consent',async()=>{
 const values=new Map(),storage={get:(k,f)=>values.get(k)??f,set:(k,v)=>values.set(k,v)};
 const first=await harness({bundle:true,storage,rest:path=>path==='/recovery/default/prepare'?envelope('running'):undefined});await first.start();await first.finish();first.dispose();
 const next=await harness({bundle:true,storage,rest:path=>path===`/recovery/default/preview/${job}`?envelope('succeeded',preview()):undefined});
 await next.maintenance.actions.load();await next.maintenance.actions.apply(preview());
 assert.equal(next.calls.some(([path])=>path===`/recovery/default/preview/${job}`),true);assert.equal(next.calls.some(([,opts])=>opts?.method==='POST'),false);assert.equal(next.maintenance.state.get().preview,null);
});
test('preview unknown ACK reconciles known job GET only and never resends prepare/key/Apply',async()=>{
 let completed=false;
 const h=await harness({bundle:true,rest:path=>path==='/recovery/default/prepare'?Promise.reject(Error('ACK lost')):path===`/recovery/default/preview/${job}`?envelope(completed?'succeeded':'running',completed?preview():null):undefined});
 await h.start();await h.finish();
 await h.maintenance.actions.poll();
 assert.equal(h.calls.filter(([path])=>path===`/recovery/default/preview/${job}`).length,1);
 assert.equal(h.maintenance.state.get().busy,true);
 completed=true;await h.maintenance.actions.poll();
 assert.equal(h.maintenance.state.get().preview?.state,'prepared');
 assert.equal(h.calls.filter(([path])=>path==='/recovery/default/prepare').length,1);
 assert.equal(h.calls.filter(([path])=>path.endsWith('/key')).length,1);
 assert.equal(h.calls.filter(([path])=>path==='/recovery/default/apply').length,0);
});
test('async preview ACK remains cancellable and status terminal never turns failed replay into fresh prepare',async()=>{
 let cancelled=false;
 const h=await harness({bundle:true,rest:path=>path==='/recovery/default/prepare'?envelope('running'):path===`/recovery/default/preview/${job}/cancel`?(cancelled=true,{...envelope('running'),cancellable:false}):path===`/recovery/default/preview/${job}`?{...envelope('cancelled'),error:'PREVIEW_CANCELLED'}:undefined});
 await h.start();await h.finish();assert.equal(h.maintenance.state.get().cancellable,true);
 await h.maintenance.actions.cancel();assert.equal(cancelled,true);await h.maintenance.actions.poll();
 assert.equal(h.maintenance.state.get().busy,false);assert.equal(h.maintenance.state.get().preview,null);
 await h.maintenance.actions.prepare();assert.equal(h.calls.filter(([path])=>path==='/recovery/default/prepare').length,1);
});
