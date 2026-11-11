import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, existsSync } from 'node:fs';
import { promisify } from 'node:util';
import {launchOwnedElectron} from './owned-electron.mjs';
const read=promisify(readFile);
// Native Chromium DOM + keyboard + synthetic File; no host app/profile mutation.
test('English native DOM: three controls, HTTP-fixture metadata, terminal restoration and rollback', async()=>{
 const browser=await launchOwnedElectron();
 try {
 const page=await browser.newPage(); const failures=[];page.on('pageerror',e=>failures.push(e.message));
 await page.setContent('<main id="app"></main>');
 await page.goto('finix-fixture://dom/');
 const fixture=JSON.parse(await read(new URL('./fixtures/operation-http.json',import.meta.url),'utf8'));
 await page.evaluate(f=>{window.httpFixture=f;},fixture);
 await page.evaluate(()=>{
  window.calls=[];window.contributions=[];let operation=null;let pending=false;const seen=new Set();
  const atom=value=>({get:()=>value,set:v=>{value=v;if(!pending){pending=true;queueMicrotask(()=>{pending=false;window.render?.();});}}});
  window.sdk={useI18n:()=>({locale:'en',isLoadingConfig:false}),useQuery:q=>{if(q.enabled&&q.refetchInterval)window.jobPoll=q.queryFn;const k=JSON.stringify(q.queryKey);if(q.enabled!==false&&!seen.has(k)){seen.add(k);queueMicrotask(()=>Promise.resolve(q.queryFn()).catch(()=>{}));}return {};},atom,useValue:a=>a.get(),Button:'button',gatewayActivationEpoch:()=>1,host:{state:{profile:atom('default'),connectionId:atom('local'),gateway:atom('open')},activeConnectionId:()=> 'local',connections:async()=>[{id:'local',kind:'local'}],ensureAgent:async(id,name)=>{calls.push(['activate',id,name]);sdk.host.state.profile.set(name);},profileRoutes:async()=>[{connectionId:'local',profile:window.httpFixture.name,targetProfile:window.httpFixture.name,mode:'local'}],requestProfile:async(route,method,params)=>({name:params.name})}};
  window.jsx=(type,props)=>({type,props});
  window.ctx={register:c=>contributions.push(c),onDispose(){},files:{requestFolder:async()=>({ok:true,folder:{id:'target',path:'C:\\synthetic-target',name:'target'}}),list:async()=>({ok:true,entries:[],truncated:false})},rest:async(p,o)=>{calls.push([p,o]);const f=window.httpFixture;if(p==='/ui-language')return{ui_language:sdk.host.state.profile.get()==='default'?'en':'ru'};if(p==='/status')return{profile_id:'default',recipient:null};if(p==='/recovery/capabilities')return{code_version:'recovery-v1',operation_protocol:'recovery-operations-v1',local_only:true,profile_id:f.profile_id};if(p==='/recovery/catalog')return{profile_id:f.profile_id,items:[{id:'b'.repeat(64),label:'Encrypted backup',created_at:'2026-11-08T00:00:00Z',source:'local',complete:true}]};if(p==='/recovery/operations/current')return operation;if(p==='/recovery/operations'){operation={...f,state:'running',phase:'awaiting_key',cancellable:true,request_id:o.body.request_id,receipt:undefined};return operation;}if(p.includes('/key?')){operation={...f,request_id:operation.request_id};return operation;}if(p==='/recovery/operations/'+f.id)return operation;return {};}};
  function dom(n){if(n==null||n===false)return document.createTextNode('');if(Array.isArray(n)){const f=document.createDocumentFragment();n.forEach(x=>f.append(dom(x)));return f;}if(typeof n!=='object')return document.createTextNode(n);if(typeof n.type==='function')return dom(n.type(n.props));const el=document.createElement(n.type);for(const[k,v]of Object.entries(n.props??{})){if(k==='children')continue;if(k.startsWith('on'))el.addEventListener(k.slice(2).toLowerCase(),e=>Promise.resolve(v({currentTarget:el,target:e.target})));else if(k==='style')Object.assign(el.style,v);else if(['value','checked','disabled'].includes(k))el[k]=v;else if(v!=null)el.setAttribute(k,v);}el.append(dom(n.props?.children));return el;}
  window.render=()=>{const route=contributions.find(c=>c.id==='recovery-page');if(route)document.querySelector('#app').replaceChildren(dom(route.render()));};
 });
 const data=s=>'data:text/javascript;base64,'+Buffer.from(s).toString('base64');
 const mapping={'@hermes/plugin-sdk':data('const s=window.sdk;export const {atom,useValue,Button,gatewayActivationEpoch,host,useQuery,useI18n}=s;'), 'react':data('export function useEffect(){}'), 'react/jsx-runtime':data('export const jsx=window.jsx,jsxs=window.jsx;')};
 await page.addScriptTag({type:'importmap',content:JSON.stringify({imports:mapping})});
 const source=await read(new URL('../desktop/plugin.js',import.meta.url),'utf8');
 await page.addScriptTag({type:'module',content:source.replace(/export\s*\{\s*plugin_default as default\s*\};?/, 'plugin_default.register(window.ctx);window.render();')});
 await page.getByRole('heading',{name:'Finix restore',exact:true}).waitFor();
 assert.equal(await page.locator('select').count(),1);assert.equal(await page.locator('input').count(),1);assert.equal(await page.locator('button').count(),1);
 await page.waitForFunction(()=>calls.some(([p])=>p==='/recovery/catalog'));assert.equal(await page.evaluate(()=>calls.some(([,o])=>o?.method==='POST')),false);
 assert.match(await page.locator('select').innerText(),/Encrypted backup.*2026-11-08.*local.*complete/);
 assert.doesNotMatch(await page.locator('body').innerText(),/[А-Яа-яёЁ]/);
 await page.getByLabel('Recovery key file',{exact:true}).setInputFiles({name:'synthetic-recovery.txt',mimeType:'text/plain',buffer:Buffer.from('SYNTHETIC-KEY')});
 assert.equal(await page.evaluate(()=>calls.filter(([p])=>p.includes('/key?')).length),0);
 const start=page.getByRole('button',{name:'Restore',exact:true});await start.focus();await page.keyboard.press('Enter');await page.waitForFunction(()=>calls.some(([p])=>p.includes('/key?')));await page.evaluate(()=>window.jobPoll());
 await page.getByRole('status').filter({hasText:'Recovered profile connected'}).waitFor();
 assert.equal(await page.evaluate(()=>calls.filter(([p])=>p.includes('/key?')).length),1);
 assert.match(await page.locator('body').innerText(),/History verified/);assert.doesNotMatch(await page.locator('body').innerText(),/[А-Яа-яёЁ]/);await page.getByRole('button',{name:'Back to previous profile',exact:true}).waitFor();assert.equal(await page.evaluate(()=>sdk.host.state.profile.get()),fixture.name);assert.deepEqual(failures,[]);
 }finally{await browser.close();}
});

const bundle=await read(new URL('../desktop/plugin.js',import.meta.url),'utf8');
const start='Check and create first backup';
const job=(patch={})=>({id:'same-job',state:'running',phase:'uploading',progress:{percent:37,completed_bytes:37,total_bytes:100,completed_files:3,total_files:10},started_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:00Z',verified:false,complete:null,omitted_count:0,...patch});
test('English native setup: localized progress, pending status, omissions and closed restart remedy',async()=>{
 const browser=await launchOwnedElectron();
 try{const page=await browser.newPage();const failures=[];page.on('pageerror',e=>failures.push(e.message));await page.route('**/*',route=>route.abort());await page.setContent('<main id="app"></main>');
 await page.evaluate(({current})=>{
  window.current=current;window.queries=[];window.calls=[];window.contributions=[];let pending=false;const seen=new Set();
  const atom=value=>({get:()=>value,set:v=>{value=v;if(!pending){pending=true;queueMicrotask(()=>{pending=false;window.render?.();});}}});
  window.sdk={useI18n:()=>({locale:'en',isLoadingConfig:false}),atom,useValue:a=>a.get(),Button:'button',Input:'input',gatewayActivationEpoch:()=>1,host:{state:{profile:atom('default'),connectionId:atom('local'),gateway:atom('open')},activeConnectionId:()=> 'local'},useQuery:q=>{window.queries=[q];const k=JSON.stringify(q.queryKey);if(q.enabled&&!seen.has(k)){seen.add(k);queueMicrotask(()=>q.queryFn());}}};
  window.jsx=(type,props)=>({type,props});window.ctx={register:c=>contributions.push(c),onDispose(){},rest:async(p,o)=>{calls.push([p,o]);if(p==='/ui-language')return{ui_language:'en'};if(p==='/status')return {profile_id:'bound',recipient:'age17s43yv3l8556x4m657nehv25sngyjjjfehgz87m2rmwc0k6nd5ksy43uvu'};if(p==='/setup')return {profile_id:'bound',verified:false,stage:'check',storage:'workspace',interval_hours:0};if(p==='/backup/jobs/current')return {code_version:'backup-jobs-v1',profile_id:'bound',job:window.current};throw Error('Unexpected route');}};
  function dom(n){if(n==null||typeof n==='boolean')return document.createTextNode('');if(Array.isArray(n)){const f=document.createDocumentFragment();n.forEach(x=>f.append(dom(x)));return f;}if(typeof n!=='object')return document.createTextNode(n);if(typeof n.type==='function')return dom(n.type(n.props));const el=document.createElement(n.type);for(const[k,v]of Object.entries(n.props??{})){if(k==='children')continue;if(k.startsWith('on'))el.addEventListener(k.slice(2).toLowerCase(),e=>v({currentTarget:el,target:e.target}));else if(k==='style')Object.assign(el.style,v);else if(['value','disabled','checked'].includes(k))el[k]=v;else if(v!=null)el.setAttribute(k,v);}el.append(dom(n.props?.children));return el;}
  window.render=()=>{const pane=contributions.find(c=>c.id==='pane');if(pane)document.querySelector('#app').replaceChildren(dom(pane.render()));};
 },{current:job()});
 const data=s=>'data:text/javascript;base64,'+Buffer.from(s).toString('base64');await page.addScriptTag({type:'importmap',content:JSON.stringify({imports:{'@hermes/plugin-sdk':data('const s=window.sdk;export const {atom,useValue,Button,Input,gatewayActivationEpoch,host,useQuery,useI18n}=s;'),'react':data('export function useEffect(){}'),'react/jsx-runtime':data('export const jsx=window.jsx,jsxs=window.jsx;')}})});
 await page.addScriptTag({type:'module',content:bundle.replace(/export\s*\{\s*plugin_default as default\s*\};?/, 'plugin_default.register(window.ctx);window.render();')});
 const bar=page.getByRole('progressbar',{name:'Backup progress: current phase'});await bar.waitFor();assert.equal(await bar.getAttribute('aria-valuenow'),'37');assert.equal(await bar.getAttribute('value'),'37');assert.equal(await page.getByRole('button',{name:start,exact:true}).isDisabled(),true);
 assert.doesNotMatch(await page.locator('body').innerText(),/[А-Яа-яёЁ]|created, uploaded and verified/);
 const status=page.locator('[aria-label="Verification and schedule"] [role="status"]');assert.equal(await status.getAttribute('aria-live'),'polite');assert.equal(await status.getAttribute('aria-atomic'),'true');const announcement=await status.textContent();
 await page.evaluate(async()=>{current={...current,progress:{percent:38,total_bytes:100}};await queries[0].queryFn();});assert.equal(await bar.getAttribute('aria-valuenow'),'38');assert.equal(await status.textContent(),announcement);
 await page.evaluate(async()=>{current={...current,progress:{percent:null,total_bytes:null,total_files:null}};await queries[0].queryFn();});assert.equal(await bar.getAttribute('value'),null);assert.equal(await bar.getAttribute('aria-valuenow'),null);
 await page.evaluate(async()=>{current={...current,state:'incomplete',complete:false,omitted_count:9};await queries[0].queryFn();});assert.match(await page.locator('body').innerText(),/Omitted files: 9/);assert.equal(await page.getByRole('button',{name:'Save and enable'}).count(),0);assert.equal(await page.evaluate(()=>calls.some(([,o])=>o?.method==='POST')),false);assert.doesNotMatch(await page.locator('body').innerText(),/[А-Яа-яёЁ]/);assert.deepEqual(failures,[]);
 await page.evaluate(async()=>{const rest=ctx.rest;ctx.rest=async(p,o)=>{if(p==='/backup/jobs/current')throw new Error('404: {"detail":"Not Found"}');return rest(p,o);};await queries[0].queryFn();});
 await page.getByText('BACKUP_BACKEND_UPDATE_REQUIRED: Update the Finix backend and restart the gateway. Backup launch is disabled; the old long-running request is not used.',{exact:true}).waitFor();
 assert.match(await status.textContent(),/BACKUP_BACKEND_UPDATE_REQUIRED:.*restart the gateway/);assert.equal(await page.getByRole('button',{name:start,exact:true}).isDisabled(),true);
 assert.doesNotMatch(await page.locator('body').innerText(),/The server connection is temporarily|Not Found/);
 }finally{await browser.close();}
});
