import test from 'node:test';
import assert from 'node:assert/strict';
import {harness} from './default-maintenance.test.mjs';
const HINT='finix.default-maintenance.read-hint.v1';

test('reopened applied result reauthenticates read-only after a same-root SDK activation epoch',async()=>{
 const values=new Map([[HINT,{version:1,profile:'default',connection:'local',profileId:'a'.repeat(64),phase:'apply-pending',job:'9'.repeat(32),maintenanceId:'b'.repeat(32),rootToken:'f'.repeat(64)}]]);
 const storage={get:(k,f)=>values.get(k)??f,set:(k,v)=>values.set(k,v)};
 let h,changeOnce=true;
 h=await harness({storage,
  sdkRoutes:[{mode:'local',connectionId:'local',profile:'default',targetProfile:'default'}],
  sdkRequest:async(_route,method)=>{if(method==='profiles.describe'){if(changeOnce){changeOnce=false;h.epoch();}return {name:'default'};}return {scope:'stored',sessions:[]};},
  rest:path=>path.startsWith('/recovery/default/status/')?{id:'b'.repeat(32),state:'ready',error:null}:undefined,
 });
 await h.maintenance.actions.load();
 await h.maintenance.actions.poll();
 assert.equal(h.maintenance.state.get().verified,true,'same-root activation must not permanently revoke read-only READY verification');
 assert.equal(h.calls.filter(([p])=>p==='/recovery/default/capabilities').length,2,'must authenticate the current physical root again');
 assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false,'a saved hint never restores creation, key or Apply authority');
 assert.equal(h.maintenance.view().value.applyEnabled,false);
 assert.equal(h.maintenance.state.get().preview,null);
});

test('read-only epoch reauthentication rejects a changed physical root before history or mutations',async()=>{
 const values=new Map([[HINT,{version:1,profile:'default',connection:'local',profileId:'a'.repeat(64),phase:'apply-pending',maintenanceId:'b'.repeat(32)}]]);
 let h,changed=false,caps=0,history=0;
 h=await harness({storage:{get:(k,f)=>values.get(k)??f,set:(k,v)=>values.set(k,v)},
  sdkRoutes:[{mode:'local',connectionId:'local',profile:'default',targetProfile:'default'}],
  sdkRequest:async(_route,method)=>{if(method==='profiles.describe'){if(!changed){changed=true;h.epoch();}return {name:'default'};}history++;return {scope:'stored',sessions:[]};},
  rest:path=>{if(path==='/recovery/default/capabilities')return {code_version:'default-recovery-v1',preview_contract:'default-preview-job-v1',supported:true,apply_enabled:true,active_shared_autoruns_supported:false,serverRootToken:(++caps===1?'f':'0').repeat(64),profile_id:'a'.repeat(64),local_only:true};if(path.startsWith('/recovery/default/status/'))return {id:'b'.repeat(32),state:'ready',error:null};},
 });
 await h.maintenance.actions.load();await h.maintenance.actions.poll();
 assert.equal(caps,2);assert.equal(history,0);assert.notEqual(h.maintenance.state.get().verified,true);
 assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);
});

test('fresh authenticated READY and stock history retire only the untrusted hint for a later explicit cycle',async()=>{
 const values=new Map([[HINT,{version:1,profile:'default',connection:'local',profileId:'a'.repeat(64),phase:'apply-pending',maintenanceId:'b'.repeat(32)}]]);
 const storage={get:(k,f)=>values.get(k)??f,set:(k,v)=>values.set(k,v)};
 const options={storage,sdkRoutes:[{mode:'local',connectionId:'local',profile:'default',targetProfile:'default'}],sdkRequest:async(_r,m)=>m==='profiles.describe'?{name:'default'}:{scope:'stored',sessions:[]},rest:p=>p.startsWith('/recovery/default/status/')?{id:'b'.repeat(32),state:'ready',error:null}:undefined};
 const first=await harness(options);await first.maintenance.actions.load();
 assert.equal(first.maintenance.state.get().verified,true);
 assert.equal(values.get(HINT).phase,'terminal','successful read-only verification must not permanently lock future independent maintenance');
 first.dispose();const next=await harness(options);await next.maintenance.actions.load();
 assert.equal(next.maintenance.state.get().ready,true);assert.equal(next.maintenance.state.get().key,false);
 assert.equal(next.maintenance.state.get().preview,null);assert.equal(next.calls.some(([,o])=>o?.method==='POST'),false);
});
