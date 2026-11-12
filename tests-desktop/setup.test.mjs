import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
import {webcrypto} from 'node:crypto';
import {existsSync} from 'node:fs';
import {launchOwnedElectron} from './owned-electron.mjs';
const bundle=await readFile(new URL('../desktop/plugin.js',import.meta.url),'utf8');
const start='Проверить и создать первую копию';
const atom=v=>({get:()=>v,set:n=>{v=n;},subscribe:()=>()=>{}});
const envelope=job=>({code_version:'backup-jobs-v1',profile_id:'bound',job});
const job=(patch={})=>({id:'same-job',state:'running',phase:'uploading',progress:{percent:37,completed_bytes:37,total_bytes:100,completed_files:3,total_files:10},started_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:00Z',verified:false,complete:null,omitted_count:0,...patch});
async function harness(options={}){
 let epoch=1,now=Date.parse('2026-01-01T00:00:00Z'),queries=[],current=options.current??null;const calls=[],contributions=[],disposers=[],profile=atom('default'),connectionId=atom('local');
 const sdk={atom,useValue:a=>a.get(),Button:'button',Input:'input',gatewayActivationEpoch:()=>epoch,useGatewayActivationEpoch:()=>epoch,host:{state:{profile,connectionId,gateway:atom('open')},activeConnectionId:()=>connectionId.get()},useQuery:q=>{queries.push(q);}};
 const jsx=(type,props)=>({type,props}),context=vm.createContext({crypto:webcrypto,TextEncoder,TextDecoder,Uint8Array,Uint32Array,ArrayBuffer,DataView,console,Date:class extends Date{static now(){return now;}},setTimeout(){assert.fail('Unmanaged timer');},clearTimeout(){}});
 const modules={'@hermes/plugin-sdk':sdk,'react':{useEffect(){}},'react/jsx-runtime':{jsx,jsxs:jsx}};
 const entry=new vm.SourceTextModule(bundle,{context});await entry.link(spec=>{const values=modules[spec];assert.ok(values,spec);return new vm.SyntheticModule(Object.keys(values),function(){for(const[k,v]of Object.entries(values))this.setExport(k,v);},{context});});await entry.evaluate();
 entry.namespace.default.register({register:c=>contributions.push(c),onDispose:f=>disposers.push(f),rest:async(p,o)=>{calls.push([p,o]);const supplied=await options.rest?.(p,o);if(supplied!==undefined)return supplied;if(p==='/ui-language'){assert.equal(o?.method,undefined,'locale attestation is read-only');return {ui_language:options.locale??'ru'};}if(p==='/status')return {profile_id:'bound',recipient:'age17s43yv3l8556x4m657nehv25sngyjjjfehgz87m2rmwc0k6nd5ksy43uvu'};if(p==='/setup')return {profile_id:'bound',verified:current?.verified===true,stage:'check',storage:'workspace',interval_hours:0};if(p==='/backup/jobs/current')return envelope(current);if(p==='/backup/jobs'){current=job();return envelope(current);}throw Error('Unexpected '+p);}});
 function expand(n){if(Array.isArray(n))return n.flatMap(expand);if(!n||typeof n!=='object')return[n];if(typeof n.type==='function')return expand(n.type(n.props));return[n,...expand(n.props?.children)];}
 const nodes=()=>{queries=[];return expand(contributions.find(c=>c.id==='pane').render());};
 const button=label=>nodes().find(n=>n?.type==='button'&&n.props.children===label);
 const text=()=>nodes().filter(x=>typeof x==='string').join('\n');
 const tick=async(ms=2000)=>{now+=ms;nodes();for(const q of queries.filter(q=>q.queryKey.includes('backup')&&q.enabled))await q.queryFn();for(let i=0;i<12;i++)await Promise.resolve();nodes();};
 const click=async()=>{const b=button(start);assert.ok(b);assert.equal(b.props.disabled,false);await b.props.onClick();};
 await tick(0);
 return {calls,nodes,button,text,tick,click,queries:()=>queries,setJob:j=>{current=j;},switch:()=>{epoch++;},source:()=>connectionId.set('other'),dispose:()=>disposers.forEach(f=>f())};
}
for(const scenario of ['missing field','backend error','explicit English']){
 test('shipped bundle English locale: '+scenario,async()=>{
  const h=await harness({rest:p=>{if(p==='/ui-language'){if(scenario==='backend error')throw Error('private locale sentinel');return scenario==='missing field'?{}:{ui_language:'en'};}}});
  assert.ok(h.nodes().some(n=>n?.type==='section'&&n.props.lang==='en'));
  assert.equal(h.button('Check and create first backup')?.props.disabled,false);
  assert.equal(h.button(start),undefined);assert.doesNotMatch(h.text(),/private locale sentinel/);
  const localeCalls=h.calls.filter(([p])=>p==='/ui-language');assert.ok(localeCalls.length>0);
  for(const [,options]of localeCalls){assert.equal(options?.method,undefined);assert.equal(options?.body,undefined);assert.equal(options.timeoutMs,10000);}
  assert.equal(h.calls.some(([,options])=>options?.method==='POST'),false);
 });
}
test('shipped backup jobs: quick Start, 20-minute worker, SDK polling, reopen attaches same job',async()=>{
 const h=await harness();assert.ok(h.calls.some(([p])=>p==='/backup/jobs/current'),'automatic mount GET');await h.click();
 assert.equal(h.calls.filter(([p])=>p==='/backup/jobs').length,1);const request=h.calls.find(([p])=>p==='/backup/jobs')[1];assert.match(request.body.request_id,/^[0-9a-f-]{36}$/);assert.equal(request.body.profile_id,'bound');assert.ok(request.timeoutMs<=30000);
 for(let i=0;i<601;i++)await h.tick();assert.equal(h.button(start).props.disabled,true);assert.match(h.text(),/Загрузка: 37%/);assert.equal(h.calls.some(([p])=>p==='/setup/check'),false);assert.ok(h.queries().some(q=>q.enabled&&q.refetchInterval===2000));
 const reopened=await harness({current:job()});assert.equal(reopened.button(start).props.disabled,true);assert.equal(reopened.calls.some(([,o])=>o?.method==='POST'),false);
});
test('no schedule without terminal completeness, absent route needs backend restart code',async()=>{
 const h=await harness({rest:p=>p==='/setup'?{profile_id:'bound',verified:true,stage:'schedule',storage:'workspace',interval_hours:0}:undefined});assert.equal(h.button('Сохранить и включить'),undefined);
 const q=await harness({rest:p=>{if(p==='/backup/jobs/current')throw {status:404,message:'private/path'};}});assert.match(q.text(),/BACKUP_BACKEND_UPDATE_REQUIRED/);assert.equal(q.button(start).props.disabled,true);
});
test('SDK plain Error route status uses only a closed leading HTTP status, never private body numbers',async()=>{
 const cases=[
  ['404: {"detail":"Not Found"}',true],
  ['405: {"detail":"Method Not Allowed"}',true],
  ['HTTP 404: Not Found',true],
  ['HTTP 405 Method Not Allowed',true],
  ['500: {"detail":"404: Not Found; HTTP 404; private/path token=secret"}',false],
  ['timeout: HTTP 404 private/path token=secret',false],
  ['Request failed: 404: private/path token=secret',false],
  ['4040: {"detail":"private/path token=secret"}',false],
  ['404private/path token=secret',false],
  ['404.5: private/path token=secret',false],
  ['HTTP 4040: private/path token=secret',false]
 ];
 for(const [message,missing] of cases){
  const error=new Error(message);assert.equal(error.status,undefined);assert.equal(error.statusCode,undefined);
  const h=await harness({rest:p=>{if(p==='/backup/jobs/current')throw error;}});
  const text=h.text();assert.equal(/BACKUP_BACKEND_UPDATE_REQUIRED/.test(text),missing,message);
  assert.equal(/Связь с сервером временно/.test(text),!missing,message);
  if(missing){assert.match(text,/перезапустите шлюз/);assert.ok(h.nodes().some(n=>n?.props?.role==='status'&&n.props.children.includes('BACKUP_BACKEND_UPDATE_REQUIRED')));}
  assert.doesNotMatch(text,/private\/path|token=secret|Not Found|Method Not Allowed/);
  assert.equal(h.button(start).props.disabled,true);await h.button(start).props.onClick();
  assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);
 }
});
test('numeric SDK statusCode recognizes missing routes without inspecting private detail',async()=>{
 for(const statusCode of [404,405,500,408,'404','405',4040]){
  const error=Object.assign(new Error('private/path token=secret body contains 404'),{statusCode});
  const h=await harness({rest:p=>{if(p==='/backup/jobs/current')throw error;}});
  const missing=statusCode===404||statusCode===405;
  assert.equal(/BACKUP_BACKEND_UPDATE_REQUIRED/.test(h.text()),missing,String(statusCode));
  assert.equal(/Связь с сервером временно/.test(h.text()),!missing,String(statusCode));
  assert.doesNotMatch(h.text(),/private\/path|token=secret|body contains/);
  assert.equal(h.button(start).props.disabled,true);await h.button(start).props.onClick();
  assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);
 }
});
test('running completeness is pending, setup prerequisites update automatically after cloud apply',async()=>{
 const h=await harness({current:job({complete:false})});assert.match(h.text(),/Загрузка: 37%/);assert.doesNotMatch(h.text(),/INCOMPLETE/);
 let storage='local';const q=await harness({rest:p=>p==='/setup'?{profile_id:'bound',verified:false,stage:'check',storage,interval_hours:0}:undefined});assert.equal(q.button(start).props.disabled,true);assert.ok(q.queries().some(x=>x.enabled&&x.refetchInterval===2000));storage='workspace';await q.tick();assert.equal(q.button(start).props.disabled,false);
});
test('native synthetic DOM: named progressbar and polite bounded status, indeterminate and incomplete semantics',async()=>{
 const browser=await launchOwnedElectron();
 try{const page=await browser.newPage();const failures=[];page.on('pageerror',e=>failures.push(e.message));await page.route('**/*',route=>route.abort());await page.setContent('<main id="app"></main>');
 await page.evaluate(({current})=>{
  window.current=current;window.queries=[];window.calls=[];window.contributions=[];let pending=false;const seen=new Set();
  const atom=value=>({get:()=>value,set:v=>{value=v;if(!pending){pending=true;queueMicrotask(()=>{pending=false;window.render?.();});}}});
  window.sdk={atom,useValue:a=>a.get(),Button:'button',Input:'input',gatewayActivationEpoch:()=>1,host:{state:{profile:atom('default'),connectionId:atom('local'),gateway:atom('open')},activeConnectionId:()=> 'local'},useQuery:q=>{window.queries=[q];const k=JSON.stringify(q.queryKey);if(q.enabled&&!seen.has(k)){seen.add(k);queueMicrotask(()=>q.queryFn());}}};
  window.jsx=(type,props)=>({type,props});window.ctx={register:c=>contributions.push(c),onDispose(){},rest:async(p,o)=>{calls.push([p,o]);if(p==='/ui-language'){if(o?.method)throw Error('locale attestation must be read-only');return {ui_language:'ru'};}if(p==='/status')return {profile_id:'bound',recipient:'age17s43yv3l8556x4m657nehv25sngyjjjfehgz87m2rmwc0k6nd5ksy43uvu'};if(p==='/setup')return {profile_id:'bound',verified:false,stage:'check',storage:'workspace',interval_hours:0};if(p==='/backup/jobs/current')return {code_version:'backup-jobs-v1',profile_id:'bound',job:window.current};throw Error('Unexpected route');}};
  function dom(n){if(n==null||typeof n==='boolean')return document.createTextNode('');if(Array.isArray(n)){const f=document.createDocumentFragment();n.forEach(x=>f.append(dom(x)));return f;}if(typeof n!=='object')return document.createTextNode(n);if(typeof n.type==='function')return dom(n.type(n.props));const el=document.createElement(n.type);for(const[k,v]of Object.entries(n.props??{})){if(k==='children')continue;if(k.startsWith('on'))el.addEventListener(k.slice(2).toLowerCase(),e=>v({currentTarget:el,target:e.target}));else if(k==='style')Object.assign(el.style,v);else if(['value','disabled','checked'].includes(k))el[k]=v;else if(v!=null)el.setAttribute(k,v);}el.append(dom(n.props?.children));return el;}
  window.render=()=>{const pane=contributions.find(c=>c.id==='pane');if(pane)document.querySelector('#app').replaceChildren(dom(pane.render()));};
 },{current:job()});
 const data=s=>'data:text/javascript;base64,'+Buffer.from(s).toString('base64');await page.addScriptTag({type:'importmap',content:JSON.stringify({imports:{'@hermes/plugin-sdk':data('const s=window.sdk;export const {atom,useValue,Button,Input,gatewayActivationEpoch,host,useQuery}=s;'),'react':data('export function useEffect(){}'),'react/jsx-runtime':data('export const jsx=window.jsx,jsxs=window.jsx;')}})});
 await page.addScriptTag({type:'module',content:bundle.replace(/export\s*\{\s*plugin_default as default\s*\};?/, 'plugin_default.register(window.ctx);window.render();')});
 const bar=page.getByRole('progressbar',{name:'Ход резервного копирования: текущий этап'});await bar.waitFor();assert.equal(await bar.getAttribute('aria-valuenow'),'37');assert.equal(await bar.getAttribute('value'),'37');assert.equal(await page.getByRole('button',{name:start,exact:true}).isDisabled(),true);
 const status=page.locator('[aria-label="Проверка и расписание"] [role="status"]');assert.equal(await status.getAttribute('aria-live'),'polite');assert.equal(await status.getAttribute('aria-atomic'),'true');const announcement=await status.textContent();
 await page.evaluate(async()=>{current={...current,progress:{percent:38,total_bytes:100}};await queries[0].queryFn();});assert.equal(await bar.getAttribute('aria-valuenow'),'38');assert.equal(await status.textContent(),announcement);
 await page.evaluate(async()=>{current={...current,progress:{percent:null,total_bytes:null,total_files:null}};await queries[0].queryFn();});assert.equal(await bar.getAttribute('value'),null);assert.equal(await bar.getAttribute('aria-valuenow'),null);
 await page.evaluate(async()=>{current={...current,state:'incomplete',complete:false,omitted_count:9};await queries[0].queryFn();});assert.match(await page.locator('body').innerText(),/Пропущено файлов: 9/);assert.equal(await page.getByRole('button',{name:'Сохранить и включить'}).count(),0);assert.equal(await page.evaluate(()=>calls.some(([,o])=>o?.method==='POST')),false);assert.deepEqual(failures,[]);
 await page.evaluate(async()=>{const rest=ctx.rest;ctx.rest=async(p,o)=>{if(p==='/backup/jobs/current')throw new Error('404: {"detail":"Not Found"}');return rest(p,o);};await queries[0].queryFn();});
 await page.getByText('BACKUP_BACKEND_UPDATE_REQUIRED: Обновите backend Finix и перезапустите шлюз. Запуск копии отключён; старый долгий запрос не используется.',{exact:true}).waitFor();
 assert.match(await status.textContent(),/BACKUP_BACKEND_UPDATE_REQUIRED:.*перезапустите шлюз/);assert.equal(await page.getByRole('button',{name:start,exact:true}).isDisabled(),true);
 assert.doesNotMatch(await page.locator('body').innerText(),/Связь с сервером временно|Not Found/);
 }finally{await browser.close();}
});
test('actual transport phases label local checksum separately from cloud readback',async()=>{
 const h=await harness({current:job({phase:'checksum'})});assert.match(h.text(),/Проверка локального шифротекста: 37%/);
 h.setJob(job({phase:'verify'}));await h.tick();assert.match(h.text(),/Проверка чтением: 37%/);
});
test('unknown legacy completeness is explicitly unknown, never zero omissions proof',async()=>{
 const h=await harness({current:job({state:'incomplete',complete:null,verified:true,omitted_count:null})});
 assert.match(h.text(),/UNKNOWN/);assert.match(h.text(),/неизвестн/);assert.doesNotMatch(h.text(),/Пропущено файлов: 0|Копия неполная/);assert.equal(h.button('Сохранить и включить'),undefined);
});
export {harness,job,envelope,start};
test('lost Start acknowledgement immediately GETs current, never duplicates; temporary polling timeout recovers',async()=>{
 let accepted=false,offline=false;
 const h=await harness({rest:(p)=>{if(p==='/backup/jobs'){accepted=true;throw Error('private/path token=secret');}if(p==='/backup/jobs/current'){if(offline)throw Error('timeout private/path');return envelope(accepted?job():null);}}});
 await h.click();assert.equal(h.calls.at(-1)[0],'/backup/jobs/current');const retained=h.button(start).props.onClick;await retained();assert.equal(h.calls.filter(([p])=>p==='/backup/jobs').length,1);
 offline=true;await h.tick();assert.match(h.text(),/Связь с сервером временно/);assert.match(h.text(),/Загрузка: 37%/);assert.equal(h.button(start).props.disabled,true);assert.doesNotMatch(h.text(),/private\/path|token=secret|Операция не завершена/);
 offline=false;await h.tick();assert.doesNotMatch(h.text(),/Связь с сервером временно/);assert.equal(h.button(start).props.disabled,true);
});
test('lost acknowledgement with absent current stays uncertain until server terminal',async()=>{
 let posts=0;
 const h=await harness({rest:p=>{if(p==='/backup/jobs'){posts++;throw Error('timeout');}}});await h.click();for(let i=0;i<4;i++)await h.tick();assert.equal(h.button(start).props.disabled,true);assert.equal(posts,1);
 h.setJob(job({state:'interrupted'}));await h.tick();assert.equal(h.button(start).props.disabled,false);
});
test('missing marker disables Start with restart code and never uses legacy POST',async()=>{
 for(const cap of [{profile_id:'bound',job:null},{code_version:'old',profile_id:'bound',job:null}]){
 const h=await harness({rest:p=>p==='/backup/jobs/current'?cap:undefined});assert.match(h.text(),/BACKUP_BACKEND_UPDATE_REQUIRED/);assert.equal(h.button(start).props.disabled,true);await h.button(start).props.onClick();assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);
 }
});
test('native progress semantics, truthful totals, readback never 100 early, incomplete omissions',async()=>{
 const h=await harness({current:job()});const bar=()=>h.nodes().find(x=>x?.type==='progress');assert.equal(bar().props['aria-valuenow'],37);assert.equal(bar().props.value,37);assert.match(bar().props['aria-label'],/резервного копирования/);assert.match(h.text(),/37 \/ 100 байт; 3 \/ 10 файлов/);
 h.setJob(job({phase:'readback',progress:{percent:100,completed_bytes:100,total_bytes:100}}));await h.tick();assert.equal(bar().props.value,99);assert.doesNotMatch(h.text(),/100%/);assert.equal(h.button('Сохранить и включить'),undefined);
 h.setJob(job({progress:{percent:55,completed_bytes:37,total_bytes:null,total_files:null}}));await h.tick();assert.equal(Object.hasOwn(bar().props,'value'),false);assert.equal(Object.hasOwn(bar().props,'aria-valuenow'),false);assert.match(h.text(),/объём пока неизвестен/);
 h.setJob(job({state:'incomplete',complete:false,verified:true,omitted_count:7,error_code:'PRIVATE /key'}));await h.tick();assert.match(h.text(),/Пропущено файлов: 7/);assert.doesNotMatch(h.text(),/PRIVATE|100%|создана, загружена и проверена/);assert.equal(h.button('Сохранить и включить'),undefined);
});
test('verified complete terminal enables schedule only after setup readback; schedule itself read back',async()=>{
 let interval=0,receipt=false;const h=await harness({rest:(p,o)=>{if(p==='/setup')return {profile_id:'bound',verified:receipt,stage:'schedule',storage:'workspace',interval_hours:interval};if(p==='/setup/schedule'){interval=Number(o.body.period.slice(0,-1))*(o.body.period.endsWith('d')?24:1);return {profile_id:'bound'};}}});
 await h.click();receipt=true;h.setJob(job({state:'succeeded',complete:true,verified:true,omitted_count:0}));await h.tick();assert.equal(h.nodes().find(x=>x?.type==='progress').props.value,100);const input=h.nodes().find(x=>x?.props?.['aria-describedby']==='finix-period-help');assert.ok(input);input.props.onChange({currentTarget:{value:'weekly'}});const retained=h.button('Сохранить и включить').props.onClick;await retained();assert.equal(h.calls.some(([p])=>p==='/setup/schedule'),false);input.props.onChange({currentTarget:{value:'7d'}});await h.button('Сохранить и включить').props.onClick();assert.match(h.text(),/Расписание включено: каждые 168 ч/);assert.equal(h.calls.at(-1)[0],'/setup');
});
test('polite NVDA status is bounded, while visible 1-percent updates remain accurate',async()=>{
 const h=await harness({current:job()});const live=()=>h.nodes().find(x=>x?.props?.role==='status'&&x.props.children?.includes?.('% этапа'));
 const initial=live()?.props.children;assert.ok(initial);for(let n=38;n<40;n++){h.setJob(job({progress:{percent:n,total_bytes:100}}));await h.tick();assert.equal(live().props.children,initial);}h.setJob(job({progress:{percent:40,total_bytes:100}}));await h.tick(10000);assert.notEqual(live().props.children,initial);assert.equal(live().props['aria-live'],'polite');assert.equal(live().props['aria-atomic'],true);
});
test('retained disabled, source/epoch/disposed actions and late polls cannot cross scopes',async()=>{
 for(const mutate of ['switch','source','dispose']){const h=await harness();const callback=h.button(start).props.onClick;h[mutate]();const n=h.calls.length;await callback();assert.equal(h.calls.length,n);}
 let resolve;const gate=new Promise(r=>resolve=r);let delayed=false;const h=await harness({current:job(),rest:p=>p==='/backup/jobs/current'&&delayed?gate:undefined});delayed=true;const pending=h.tick();await Promise.resolve();h.switch();h.nodes();resolve(envelope(job({state:'succeeded',verified:true,complete:true})));await pending;assert.doesNotMatch(h.text(),/создана, загружена и проверена/);
});
