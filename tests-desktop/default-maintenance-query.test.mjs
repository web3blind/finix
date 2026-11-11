import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {harness,preview} from './default-maintenance.test.mjs';
test('pinned actual QueryClient shared cache cannot poison manual reopen after reconnect GET failure',{skip:!process.env.FINIX_QUERY_CORE},async()=>{
 const require=createRequire(import.meta.url),{QueryClient}=require(process.env.FINIX_QUERY_CORE);
 const client=new QueryClient({defaultOptions:{queries:{retry:false,gcTime:Infinity}}});let pending=[];
 const query=q=>{if(q.enabled)pending.push(client.fetchQuery(q));return {};};
 const drain=async()=>{await Promise.all(pending);pending=[];};
 const values=new Map(),storage={get:(k,f)=>values.get(k)??f,set:(k,v)=>values.set(k,v)};
 const p=preview();p.apply_enabled=true;let down=false;
 const h=await harness({storage,query,reviewGate:true,preview:p,rest:path=>{if(down&&path==='/recovery/default/capabilities')throw Object.assign(Error('transient'),{status:503});}});
 h.render();await drain();await h.start();await h.finish();await h.maintenance.actions.apply(h.maintenance.state.get().preview);
 down=true;h.epoch();h.render();await drain();assert.equal(h.maintenance.state.get().busy,true);assert.equal(h.maintenance.state.get().polling,false);const old=h.calls.length;h.render();await drain();assert.equal(h.calls.length,old);h.dispose();
 const reopened=await harness({storage,query,sdkRoutes:[{mode:'local',connectionId:'local',profile:'default',targetProfile:'default'}],sdkRequest:async(_r,m)=>m==='profiles.describe'?{name:'default'}:{scope:'stored',sessions:[]},rest:path=>path.startsWith('/recovery/default/status/')?{id:p.id,state:'ready',error:null}:undefined});
 reopened.render();await drain();assert.equal(reopened.maintenance.state.get().verified,true);assert.ok(reopened.calls.some(([path])=>path===`/recovery/default/status/${p.id}`));assert.equal(reopened.calls.some(([,o])=>o?.method==='POST'),false);assert.ok(client.getQueryCache().getAll().length>=3);client.clear();
});
