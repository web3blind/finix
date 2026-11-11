import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
import {webcrypto} from 'node:crypto';
import {existsSync} from 'node:fs';
import {chromium} from 'playwright-core';
const bundle=await readFile(new URL('../desktop/plugin.js',import.meta.url),'utf8');
const start='Проверить и создать первую копию';
const atom=v=>({get:()=>v,set:n=>{v=n;},subscribe:()=>()=>{}});
const envelope=job=>({code_version:'backup-jobs-v1',profile_id:'bound',job});
const job=(patch={})=>({id:'same-job',state:'running',phase:'uploading',progress:{percent:37,completed_bytes:37,total_bytes:100,completed_files:3,total_files:10},started_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:00Z',verified:false,complete:null,omitted_count:0,...patch});
async function harness(options={}){
 let epoch=1,now=Date.parse('2026-01-01T00:00:00Z'),queries=[],current=options.current??null;const calls=[],contributions=[],disposers=[],profile=atom('default'),connectionId=atom('local');
 const sdk={useI18n:()=>({locale:options.locale??'ru',isLoadingConfig:false}),atom,useValue:a=>a.get(),Button:'button',Input:'input',gatewayActivationEpoch:()=>epoch,useGatewayActivationEpoch:()=>epoch,host:{state:{profile,connectionId,gateway:atom('open')},activeConnectionId:()=>connectionId.get()},useQuery:q=>{queries.push(q);}};
 const jsx=(type,props)=>({type,props}),context=vm.createContext({crypto:webcrypto,TextEncoder,TextDecoder,Uint8Array,Uint32Array,ArrayBuffer,DataView,console,Date:class extends Date{static now(){return now;}},setTimeout(){assert.fail('Unmanaged timer');},clearTimeout(){}});
 const modules={'@hermes/plugin-sdk':sdk,'react':{useEffect(){}},'react/jsx-runtime':{jsx,jsxs:jsx}};
 const entry=new vm.SourceTextModule(bundle,{context});await entry.link(spec=>{const values=modules[spec];assert.ok(values,spec);return new vm.SyntheticModule(Object.keys(values),function(){for(const[k,v]of Object.entries(values))this.setExport(k,v);},{context});});await entry.evaluate();
 entry.namespace.default.register({register:c=>contributions.push(c),onDispose:f=>disposers.push(f),rest:async(p,o)=>{calls.push([p,o]);const supplied=await options.rest?.(p,o);if(supplied!==undefined)return supplied;if(p==='/ui-language')return {ui_language:options.locale??'ru'};if(p==='/status')return {profile_id:'bound',recipient:'age17s43yv3l8556x4m657nehv25sngyjjjfehgz87m2rmwc0k6nd5ksy43uvu'};if(p==='/setup')return {profile_id:'bound',verified:current?.verified===true,stage:'check',storage:'workspace',interval_hours:0};if(p==='/backup/jobs/current')return envelope(current);if(p==='/backup/jobs'){current=job();return envelope(current);}throw Error('Unexpected '+p);}});
 function expand(n){if(Array.isArray(n))return n.flatMap(expand);if(!n||typeof n!=='object')return[n];if(typeof n.type==='function')return expand(n.type(n.props));return[n,...expand(n.props?.children)];}
 const nodes=()=>{queries=[];return expand(contributions.find(c=>c.id==='pane').render());};
 const button=label=>nodes().find(n=>n?.type==='button'&&n.props.children===label);
 const text=()=>nodes().filter(x=>typeof x==='string').join('\n');
 const tick=async(ms=2000)=>{now+=ms;nodes();for(const q of queries.filter(q=>q.queryKey.includes('backup')&&q.enabled))await q.queryFn();for(let i=0;i<12;i++)await Promise.resolve();nodes();};
 const click=async()=>{const b=button(start);assert.ok(b);assert.equal(b.props.disabled,false);await b.props.onClick();};
 await tick(0);
 return {setProfile:n=>profile.set(n),calls,nodes,button,text,tick,click,queries:()=>queries,setJob:j=>{current=j;},switch:()=>{epoch++;},source:()=>connectionId.set('other'),dispose:()=>disposers.forEach(f=>f())};
}
test('English setup uses real labels, progress, polite status and truthful pending result',async()=>{
 const h=await harness({locale:'en',current:job({complete:false})});await h.tick(0);
 assert.ok(h.button('Check and create first backup'),'English Start control');
 assert.match(h.text(),/Uploading: 37%/);
 assert.doesNotMatch(h.text(),/[А-Яа-яёЁ]|created, uploaded and verified|INCOMPLETE/);
 assert.ok(h.nodes().some(n=>n?.type==='progress'&&n.props['aria-label']==='Backup progress: current phase'));
 h.setJob(job({state:'succeeded',phase:'complete',complete:true,verified:true,omitted_count:0}));await h.tick();
 assert.match(h.text(),/created, uploaded and verified/);assert.ok(h.button('Save and enable'));
});
test('locale initialization is scope-fenced; late English cannot overwrite another profile Russian',async()=>{
 let read=0,release;const pending=new Promise(r=>{release=r;});
 const h=await harness({locale:'en',rest:p=>p==='/ui-language'?(++read===1?{ui_language:'en'}:read===2?pending:{ui_language:'ru'}):undefined});await h.tick(0);
 assert.ok(h.button('Check and create first backup'));
 h.setProfile('second');assert.equal(h.text(),'Finix…');
 h.setProfile('third');assert.equal(h.text(),'Finix…');
 await h.tick(0);assert.ok(h.button('Проверить и создать первую копию'));
 release({ui_language:'en'});await h.tick(0);
 assert.ok(h.button('Проверить и создать первую копию'));assert.equal(h.button('Check and create first backup'),undefined);
 assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);
});

test('running complete phase is pending in English and Russian, never a verified backup',async()=>{
 for(const locale of ['en','ru']){
  const h=await harness({locale,current:job({phase:'complete',complete:false})});await h.tick(0);
  assert.doesNotMatch(h.text(),/Backup verified|Копия проверена|created, uploaded and verified|INCOMPLETE/);
  assert.match(h.text(),locale==='en'?/not confirmed yet/:/ещё не подтверждена/);
 }
});

for(const name of ['backup-http.json','backup-http-legacy.json'])test('English shipped UI consumes actual offline HTTP backend contract: '+name,async()=>{
 const actual=JSON.parse(await readFile(new URL('./fixtures/'+name,import.meta.url),'utf8'));let current=actual.ack;
 const h=await harness({locale:'en',rest:p=>p==='/ui-language'?{ui_language:'en'}:p==='/status'?{profile_id:actual.ack.profile_id,recipient:actual.setup_before.recipient}:p==='/setup'?(current.job?.state==='succeeded'?actual.setup_terminal:actual.setup_before):p==='/backup/jobs/current'?current:undefined});await h.tick(0);
 for(const poll of actual.polls){current=poll;await h.tick();assert.doesNotMatch(h.text(),/[А-Яа-яёЁ]|created, uploaded and verified/);assert.equal(h.button('Save and enable'),undefined);}
 current=actual.terminal;await h.tick();assert.match(h.text(),/created, uploaded and verified/);assert.ok(h.button('Save and enable'));
});
test('English cloud controls and every public session status use the shared dictionary',async()=>{
 for(const status of ['idle','working','authorize','verified','applied','choice','cancelled','expired','failed']){
  const h=await harness({locale:'en',rest:p=>p==='/connection'?{profile_id:'bound',ticket:'csrf',session:{status,code:'RCLONE_MISSING',question:'config_driveid',choices:status==='choice'?[{id:'0',label:'Диск 1'}]:[]}}:undefined});await h.tick(0);
  await h.button('Refresh connection').props.onClick();
  assert.doesNotMatch(h.text(),/[А-Яа-яёЁ]/,status);
  assert.ok(h.button('Start connection'));assert.ok(h.button('Apply connection'));assert.ok(h.button('Cancel connection'));
  assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);
 }
});


