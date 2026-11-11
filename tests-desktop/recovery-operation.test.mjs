import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
import {webcrypto} from 'node:crypto';
export const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const ID='a'.repeat(32),RID='11111111-1111-4111-8111-111111111111';
export async function controller(o={}){
 const source=await readFile(new URL('../desktop/src/recovery-operation.mjs',import.meta.url),'utf8');
 const atom=v=>{const listeners=[];return {get:()=>v,set:x=>{v=x;listeners.forEach(f=>f());},subscribe:f=>{listeners.push(f);return ()=>listeners.splice(listeners.indexOf(f),1);}};};
 let epoch=3,selected=false;const profile=atom(o.profile??'source'),connectionId=atom('local');const calls=[],storage=o.storage??new Map(),disposers=[];
 let current=o.current??null;
 const row=(patch={})=>({id:ID,request_id:current?.request_id??RID,profile_id:o.profileId??'bound',state:'running',phase:'awaiting_key',name:'restored',cancellable:true,cancel_requested:false,full_acceptance:false,...patch});
 const sdk={atom,gatewayActivationEpoch:()=>epoch,host:{state:{profile,connectionId,gateway:atom('open')},activeConnectionId:()=>connectionId.get(),connections:async()=>[{id:'local',kind:'local'}],
 ensureAgent:async(_,name)=>{calls.push(['select',name]);selected=true;profile.set(name);epoch+=7;await o.select?.();},
 profileRoutes:async()=>o.routes??[{connectionId:'local',profile:o.target??'restored',targetProfile:o.target??'restored',mode:'local'}],
 requestProfile:async(route,method,args)=>{calls.push(['describe',route]);return {name:args.name};}}};
 const context=vm.createContext({Uint8Array,ArrayBuffer,Number,Date,crypto:webcrypto});
 const entry=new vm.SourceTextModule(source,{context});const mods={'@hermes/plugin-sdk':sdk,'./i18n.mjs':{useLocalized:()=>({}),scopedLanguage:()=> 'en'},'react/jsx-runtime':{jsx(){},jsxs(){}},react:{useEffect(){}}};
 await entry.link(n=>{const m=mods[n];assert.ok(m,n);return new vm.SyntheticModule(Object.keys(m),function(){for(const[k,v]of Object.entries(m))this.setExport(k,v);},{context});});await entry.evaluate();
 const recovery=entry.namespace.createRecovery({storage:{get:(k,f)=>storage.get(k)??f,set:(k,v)=>storage.set(k,v),remove:k=>storage.delete(k)},onDispose:f=>disposers.push(f),rest:async(p,opts)=>{
 calls.push([p,opts]);assert.equal(selected,false,'ZERO source REST after selection');const custom=o.rest?.(p,opts,row);if(custom!==undefined)return await custom;
 if(p.endsWith('/capabilities'))return {code_version:'recovery-v1',operation_protocol:'recovery-operations-v1',profile_id:o.profileId??'bound',local_only:true};
 if(p.endsWith('/current'))return current;
 if(p.endsWith('/catalog'))return {profile_id:o.profileId??'bound',items:[{id:'b'.repeat(64),label:'Copy',complete:true}]};
 if(p==='/recovery/operations'){current=row({request_id:opts.body.request_id});return current;}
 if(p.includes('/key?')){current=row({phase:'data_running'});return current;}
 if(p.endsWith('/cancel')){current=row({cancel_requested:true});return current;}
 return current;
 }});
 return {recovery,calls,row,storage,profile,connectionId,change:()=>{epoch++;profile.set('other');},dispose:()=>disposers.forEach(f=>f()),set:x=>{current=x;},start:async()=>{await recovery.actions.load();recovery.actions.key({size:8,arrayBuffer:async()=>new ArrayBuffer(8)});await recovery.actions.start({candidate:'b'.repeat(64),complete:true});}};
}
test('one durable begin/key, exact local selection only after whole backend receipt',async()=>{
 const h=await controller();await h.start();assert.equal(h.recovery.state.get().busy,true);
 h.set(h.row({phase:'importing',cancellable:false}));await h.recovery.actions.poll();await h.recovery.actions.cancel();assert.equal(h.calls.some(([p])=>p.endsWith('/cancel')),false);assert.equal(h.recovery.state.get().busy,true);
 h.set(h.row({state:'succeeded',phase:'backend_target_ready',cancellable:false,receipt:{name:'restored',served:'restored',backend_target_ready:true,archive_verified:true,content_verified:true,integrations_paused:true,history_pending:false,full_acceptance:false,contract:'sdk-new-content-copy-v1'}}));await h.recovery.actions.poll();
 assert.equal(h.recovery.state.get().activated,true);assert.equal(h.calls.filter(([p])=>p==='/recovery/operations').length,1);assert.equal(h.calls.filter(([p])=>p.includes('/key?')).length,1);assert.equal(h.calls.some(([p])=>/prepare-auto|import-auto|\/start$/.test(p)),false);
});
test('protocol admission before key selection and dispatch',async()=>{const h=await controller({rest:p=>p.endsWith('/capabilities')?{code_version:'recovery-v1',profile_id:'bound',local_only:true}:undefined});await h.start();assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);assert.equal(h.recovery.state.get().key,false);});
