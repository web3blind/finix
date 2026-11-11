import test from 'node:test';
import assert from 'node:assert/strict';
import {harness} from './recovery.test.mjs';
const refusal="Error invoking remote method 'hermes:api': Error: 409: {\"detail\":\"PROFILE_UNAVAILABLE\"}";
// Model Query's rejection-only retry scheduling, not a second direct load().
// The old catch returns null => success/idle + staleTime Infinity => no retry.
function queryClock(){
 const pending=[];const outcomes=[];
 const run=async(q,count=0)=>{try{await q.queryFn();outcomes.push('success');}catch(error){outcomes.push('error');if(typeof q.retry==='function'&&q.retry(count,error)){assert.equal(q.retryDelay,1000);pending.push(()=>run(q,count+1));}}};
 return {run,outcomes,pending,advance:async()=>{const batch=pending.splice(0);for(const next of batch)await next();}};
}
const locked=h=>{assert.equal(h.nodes().find(n=>n.type==='input').props.disabled,true);assert.equal(h.button('Восстановить').props.disabled,true);};
const readOnly=h=>{assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);assert.equal(h.calls.some(([p])=>p==='activate'),false);};
test('cold capability IPC409 followed by same-scope readiness reattaches through bounded SDK Query, without intent',async()=>{
 const clock=queryClock();let calls=0;
 const h=await harness({queryRunner:clock.run,rest:p=>{if(p==='/recovery/capabilities'&&++calls===1)throw Error(refusal);}});
 locked(h);assert.deepEqual(clock.outcomes,['error'],'temporary admission refusal must not become Query success/idle');
 assert.equal(clock.pending.length,1);await clock.advance();await h.settle();
 assert.equal(calls,2);assert.equal(h.nodes().find(n=>n.type==='select').props.disabled,false);assert.equal(h.nodes().find(n=>n.type==='input').props.disabled,false);assert.equal(clock.pending.length,0);readOnly(h);
});
test('persistent exact capability refusal stops after three read-only reattachments and remains locked',async()=>{
 const clock=queryClock();let calls=0;const h=await harness({queryRunner:clock.run,rest:p=>{if(p==='/recovery/capabilities'){calls++;throw Error(refusal);}}});
 for(let i=0;i<8;i++){await clock.advance();await h.settle();}assert.equal(calls,4);assert.equal(clock.pending.length,0);locked(h);readOnly(h);
});
for(const [label,message]of [['server failure','500: {"detail":"PROFILE_UNAVAILABLE"}'],['foreign conflict','409: {"detail":"STALE_PROFILE"}'],['body-only status','500: private 409 PROFILE_UNAVAILABLE'],['suffix injection',refusal+' private'],['ticket bootstrap',"Error invoking remote method 'hermes:api': Error: gateway ticket bootstrap invalid_control_response"]])test('admission does not retry '+label,async()=>{
 const clock=queryClock();let calls=0;const h=await harness({queryRunner:clock.run,rest:p=>{if(p==='/recovery/capabilities'){calls++;throw Error(message);}}});await clock.advance();await h.settle();assert.equal(calls,1);assert.equal(clock.pending.length,0);locked(h);readOnly(h);
});
for(const boundary of ['/recovery/operations/current','/recovery/catalog'])test('known refusal outside capability does not broaden read retries: '+boundary,async()=>{
 const clock=queryClock();const h=await harness({queryRunner:clock.run,rest:p=>{if(p===boundary)throw Error(refusal);}});await clock.advance();await h.settle();assert.equal(clock.pending.length,0);locked(h);readOnly(h);
});
for(const change of ['source','profile','dispose'])test('scheduled readiness continuation cannot cross '+change+' fence',async()=>{
 const clock=queryClock();let calls=0;const h=await harness({queryRunner:clock.run,rest:p=>{if(p==='/recovery/capabilities'){calls++;throw Error(refusal);}}});
 assert.equal(clock.pending.length,1);if(change==='source')h.switch('remote');else if(change==='profile')h.switchProfile('other');else h.dispose();await clock.advance();assert.equal(calls,1);readOnly(h);
});
