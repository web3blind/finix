import test from 'node:test';
import assert from 'node:assert/strict';
import {harness,preview} from './default-maintenance.test.mjs';
for(const bundle of [false,true])test(`acknowledged native maintenance closes its window without reviving the gateway (${bundle?'bundle':'source'})`,async()=>{
 const p=preview();p.apply_enabled=true;
 const h=await harness({bundle,preview:p,reviewGate:true});await h.start();await h.finish();
 const start=h.calls.length;await h.maintenance.actions.apply(h.maintenance.state.get().preview);
 assert.deepEqual(h.calls.slice(start).map(([path])=>path),['/recovery/default/apply','native:close']);
 assert.equal(h.maintenance.state.get().polling,false);
 await h.maintenance.actions.poll();assert.equal(h.calls.at(-1)[0],'native:close');
 assert.equal(h.calls.filter(([path])=>path==='/recovery/default/apply').length,1);
});
for(const response of [undefined,{id:'0'.repeat(32),state:'acknowledged',error:null},{id:'b'.repeat(32),state:'unknown',error:null}])test(`uncertain or invalid acknowledgement does not close or retry (${JSON.stringify(response)})`,async()=>{
 const p=preview();p.apply_enabled=true;const h=await harness({preview:p,reviewGate:true,rest:path=>path==='/recovery/default/apply'?(response??Promise.reject(Error('lost ACK'))):undefined});await h.start();await h.finish();const rendered=h.maintenance.state.get().preview;await h.maintenance.actions.apply(rendered);await h.maintenance.actions.apply(rendered);assert.equal(h.calls.some(([path])=>path==='native:close'),false);assert.equal(h.calls.filter(([path])=>path==='/recovery/default/apply').length,1);
});
