import test from 'node:test';
import assert from 'node:assert/strict';
import {harness} from './default-maintenance.test.mjs';
const HINT='finix.default-maintenance.read-hint.v1',old='9'.repeat(32),fresh='8'.repeat(32),candidate='c'.repeat(64),profileId='a'.repeat(64);
function setup(error='PREVIEW_DEADLINE',root='f'.repeat(64)){
 const saved={version:1,profile:'default',connection:'local',profileId,phase:'preview-terminal',job:old,maintenanceId:null,rootToken:root};
 const values=new Map([[HINT,saved]]);let reads=0;
 return {saved,values,storage:{get:(k,f)=>values.get(k)??f,set:(k,v)=>values.set(k,v)},rest:(p,o)=>{
  if(p.includes('/retir'))throw Error('No retirement protocol is required');
  if(p===`/recovery/default/preview/${old}`)return {contract:'default-preview-job-v1',id:old,state:error==='PREVIEW_INTERRUPTED'?'interrupted':'failed',error,result:null,cancellable:false};
  if(p==='/recovery/default/prepare-auto')return {id:fresh,profile_id:profileId,state:'awaiting_key'};
  if(p===`/recovery/jobs/${fresh}/key`)return {id:fresh,profile_id:profileId,state:'running'};
 },file:{size:8,arrayBuffer:async()=>{reads++;return new ArrayBuffer(8);}},reads:()=>reads};
}
for(const error of ['PREVIEW_DEADLINE','DEFAULT_RECOVERY_FAILED']) test(`known terminal ${error} permits fresh selection without automatic work`,async()=>{
 const f=setup(error),h=await harness(f);await h.maintenance.actions.load();
 assert.equal(h.maintenance.state.get().ready,true);
 assert.deepEqual(f.values.get(HINT),f.saved,'original hint remains until a fresh explicit request');
 assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);assert.equal(f.reads(),0);
 h.maintenance.actions.key(f.file);assert.equal(f.reads(),0);
 await h.maintenance.actions.prepare({candidate});
 assert.equal(h.calls.filter(([p])=>p==='/recovery/default/prepare-auto').length,1);
 assert.equal(f.values.get(HINT).job,fresh);assert.equal(f.reads(),1);
 assert.equal(h.calls.some(([p])=>p.includes('/retir')),false);
});
test('unknown interrupted or different-root preparations stay locked',async()=>{
 for(const f of [setup('PREVIEW_INTERRUPTED'),setup('PREVIEW_DEADLINE','1'.repeat(64))]){
  const h=await harness(f);await h.maintenance.actions.load();assert.equal(h.maintenance.state.get().ready,false);
  assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);assert.equal(f.reads(),0);
 }
});
