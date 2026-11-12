import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
import {webcrypto,createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';
// Resolve the actual host dependency, never a modeled Query scheduler.
const queryModule=process.env.FINIX_SDK_QUERY_MODULE;
const runtime=queryModule?await import(pathToFileURL(queryModule).href):null;
const atom=value=>{const listeners=new Set();return {get:()=>value,set:v=>{value=v;for(const f of listeners)f();},subscribe:f=>{listeners.add(f);return()=>listeners.delete(f);}};};
const ID='b'.repeat(64);
const readyOperation={id:'a'.repeat(32),request_id:'11111111-1111-4111-8111-111111111111',profile_id:'synthetic',state:'succeeded',phase:'backend_target_ready',name:'restored',cancellable:false,cancel_requested:false,receipt:{name:'restored',served:'restored',backend_target_ready:true,archive_verified:true,content_verified:true,integrations_paused:true,history_pending:false,full_acceptance:false,contract:'sdk-new-content-copy-v1'}};
test('real Query pending activation owner disposal does not publish or replay SOURCE mutations', {skip:!runtime&&'Set FINIX_SDK_QUERY_MODULE'},async t=>{
 let release;const gate=new Promise(r=>release=r);const e=await environment({current:readyOperation,select:()=>gate});t.after(e.close);
 const h=await e.evaluate();await h.settle();assert.equal(e.calls.filter(c=>c.path==='select').length,1);const old=h.render().find(n=>n.type==='button'&&n.props.type==='button').props.onClick;
 h.dispose();release();await new Promise(r=>setImmediate(r));await old();assert.equal(h.scope().activated,false);assert.equal(e.calls.filter(c=>c.path==='select').length,1);assert.equal(e.calls.some(c=>c.method==='POST'),false);assert.equal(e.calls.filter(c=>c.path.startsWith('/recovery/')).length,2);assert.equal(e.calls.some(c=>c.path==='describe'),false);
});
test('real Query ignores forged SOURCE storage hint and performs fresh server admission', {skip:!runtime&&'Set FINIX_SDK_QUERY_MODULE'},async t=>{
 const e=await environment({storage:[['recovery.selection.v1',{version:1,connection:'local',source:'default',target:'foreign',id:'forged',phase:'connected'}]]});t.after(e.close);
 const h=await e.evaluate();await h.settle();assert.equal(h.scope().ready,true);assert.equal(h.scope().activated,false);assert.equal(h.scope().job,null);assert.equal(e.calls.filter(c=>c.path.startsWith('/recovery/')).length,3);assert.equal(e.calls.some(c=>c.method==='POST'||c.path==='select'),false);
});
test('real Query Back creates a fresh fenced admission without another Restore or key', {skip:!runtime&&'Set FINIX_SDK_QUERY_MODULE'},async t=>{
 const e=await environment({current:readyOperation,select:async()=>{}});t.after(e.close);const h=await e.evaluate();await h.settle();assert.equal(h.scope().activated,true);const before=e.calls.filter(c=>c.path.startsWith('/recovery/')).length;
 const back=h.render().find(n=>n.type==='button'&&n.props.children==='Back to previous profile');assert.ok(back);await back.props.onClick();await h.settle();assert.equal(h.scope().ready,true);assert.equal(h.scope().activated,false);assert.equal(h.scope().key,false);assert.equal(h.scope().candidate,ID);assert.equal(e.calls.filter(c=>c.path.startsWith('/recovery/')).length-before,3);assert.equal(e.calls.some(c=>c.method==='POST'),false);assert.equal(e.calls.filter(c=>c.path==='select').length,2);
});
async function environment(options={}){
 const client=new runtime.QueryClient({defaultOptions:{queries:{gcTime:Infinity}}});
 const profile=atom('default'),connectionId=atom('local'),gateway=atom('open');let epoch=1;
 const calls=[],controllers=[],storage=new Map(options.storage??[]);let selected=false;
 async function evaluate(){
  const contributions=[],disposers=[],queries=[],observers=new Map();
  const sdk={atom,useValue:a=>a.get(),Button:'button',gatewayActivationEpoch:()=>epoch,host:{state:{profile,connectionId,gateway},activeConnectionId:()=>connectionId.get(),connections:async()=>[{id:'local',kind:'local'}],navigate(){},profileRoutes:async()=>[{mode:'local',connectionId:'local',profile:profile.get(),targetProfile:profile.get()}],requestProfile:async(_,method,args)=>{calls.push({path:'describe',method});return {name:args.name};},ensureAgent:async(_,name)=>{assert.ok(options.select,'unexpected selection');calls.push({path:'select',method:'host'});selected=true;await options.select();profile.set(name);epoch++;if(name==='default')selected=false;}},useQuery:q=>{queries.push(q);const key=JSON.stringify(q.queryKey);let observer=observers.get(key);if(!observer){observer=new runtime.QueryObserver(client,q);observers.set(key,observer);observer.stop=observer.subscribe(()=>{});}else observer.setOptions(q);return observer.getCurrentResult();}};
  const jsx=(type,props)=>({type,props});
  const context=vm.createContext({crypto:webcrypto,TextEncoder,TextDecoder,Uint8Array,ArrayBuffer,setTimeout,clearTimeout,queueMicrotask});
  const modules={'@hermes/plugin-sdk':sdk,react:{useEffect(){}},'react/jsx-runtime':{jsx,jsxs:jsx},'./i18n.mjs':{scopedLanguage:()=> 'en',useLocalized:()=>({jsx,jsxs:jsx})}};
  const sourceMode=process.env.FINIX_LIFETIME_SOURCE==='1';
  const bytes=await readFile(sourceMode?new URL('../desktop/src/recovery.mjs',import.meta.url):process.env.FINIX_LIFETIME_BUNDLE??new URL('../desktop/plugin.js',import.meta.url));
  const entry=new vm.SourceTextModule(bytes.toString(),{context});
  const link=async name=>{if(name==='./recovery-operation.mjs'){const operation=new vm.SourceTextModule(await readFile(process.env.FINIX_LIFETIME_OPERATION??new URL('../desktop/src/recovery-operation.mjs',import.meta.url),'utf8'),{context});await operation.link(link);return operation;}const m=modules[name];assert.ok(m,name);return new vm.SyntheticModule(Object.keys(m),function(){for(const[k,v]of Object.entries(m))this.setExport(k,v);},{context});};
  await entry.link(link);await entry.evaluate();
  const ctx={register:c=>contributions.push(c),onDispose:f=>disposers.push(f),storage:{get:(k,f)=>storage.get(k)??f,set:(k,v)=>storage.set(k,v)},rest:async(path,request)=>{if(path.startsWith('/recovery/'))assert.equal(selected,false,'zero SOURCE REST after selection');calls.push({controller:controllers.length,path,method:request?.method??'GET'});if(path==='/status')return {profile_id:'synthetic',recipient:null};if(path==='/recovery/capabilities')return {code_version:'recovery-v1',operation_protocol:'recovery-operations-v1',local_only:true,profile_id:'synthetic'};if(path==='/recovery/operations/current')return options.current??null;if(path==='/recovery/catalog')return {profile_id:'synthetic',items:[{id:ID,label:'Synthetic copy',complete:true}]};return {};}};
  if(sourceMode){const recovery=entry.namespace.createRecovery(ctx);ctx.register({id:'recovery-page',render:()=>jsx(entry.namespace.Recovery,{recovery})});}else entry.namespace.default.register(ctx);
  const flatten=n=>Array.isArray(n)?n.flatMap(flatten):!n||typeof n!=='object'?[]:typeof n.type==='function'?flatten(n.type(n.props)):[n,...flatten(n.props?.children)];
  const root=()=>contributions.find(c=>c.id==='recovery-page').render();
  const render=()=>flatten(root());
  const settle=async()=>{for(let i=0;i<8;i++){render();await new Promise(r=>setImmediate(r));}};
  const h={queries,render,settle,scope:()=>root().props.recovery.view().value,dispose:()=>{disposers.forEach(f=>f());for(const o of observers.values()){o.stop();o.destroy();}},unmount:()=>{for(const o of observers.values()){o.stop();o.destroy();}observers.clear();},hash:createHash('sha256').update(bytes).digest('hex')};
  controllers.push(h);return h;
 }
 return {client,calls,evaluate,storage,epoch:()=>{epoch++;gateway.set('changed');},close:()=>{controllers.forEach(h=>h.dispose());client.clear();}};
}
test('cached successful null cannot strand a fresh byte-identical plugin evaluation controller', {skip:!runtime&&'Set FINIX_SDK_QUERY_MODULE to the pinned host react-query ESM entry'},async t=>{
 const e=await environment();t.after(e.close);
 const first=await e.evaluate();await first.settle();assert.equal(first.scope().ready,true);const key=first.queries.find(q=>q.queryKey[2]==='catalog').queryKey;
 assert.equal(e.client.getQueryState(key).status,'success');assert.equal(e.client.getQueryData(key),null);assert.equal(e.client.getQueryState(key).fetchStatus,'idle');
 const stale=first.render().find(n=>n.type==='input').props.onChange;first.dispose();stale({currentTarget:{files:[{size:8}],value:'synthetic'}});assert.equal(first.scope().key,false);const before=e.calls.filter(c=>c.path.startsWith('/recovery/')).length;
 const second=await e.evaluate();await second.settle();assert.equal(second.hash,first.hash,'separate evaluations of exactly the same shipped bytes');
 const after=e.calls.filter(c=>c.path.startsWith('/recovery/')).length;
 console.log(JSON.stringify({oldKey:key,newKey:second.queries.find(q=>q.queryKey[2]==='catalog').queryKey,firstStatus:e.client.getQueryState(key).status,cachedData:e.client.getQueryData(key),newReady:second.scope().ready,newRecoveryGets:after-before,artifactSha256:first.hash,sourceMode:process.env.FINIX_LIFETIME_SOURCE==='1'}));
 assert.equal(second.scope().ready,true,'fresh controller must execute its own admission despite prior successful null with infinite staleTime');
 assert.equal(second.scope().candidate,ID);assert.equal(after-before,3,'fresh capability/current/catalog; no restored authority from cache');
 assert.equal(e.calls.some(c=>c.method!=='GET'),false);
});
test('same controller reopen retains initialized state; source epoch reinitializes and rejects retained native callback', {skip:!runtime&&'Set FINIX_SDK_QUERY_MODULE'},async t=>{
 const e=await environment();t.after(e.close);const h=await e.evaluate();await h.settle();const stale=h.render().find(n=>n.type==='input').props.onChange;const before=e.calls.length;h.unmount();await h.settle();assert.equal(h.scope().ready,true);assert.equal(e.calls.length,before);
 e.epoch();assert.equal(h.scope().ready,false);stale({currentTarget:{files:[{size:8}],value:'synthetic'}});assert.equal(h.scope().key,false);await h.settle();assert.equal(h.scope().ready,true);assert.equal(h.scope().key,false);assert.equal(e.calls.filter(c=>c.path==='/recovery/catalog').length,2);assert.equal(e.calls.some(c=>c.method!=='GET'),false);
});
