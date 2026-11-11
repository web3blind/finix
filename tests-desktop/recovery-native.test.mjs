import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, existsSync } from 'node:fs';
import { promisify } from 'node:util';
import {launchOwnedElectron} from './owned-electron.mjs';
const read=promisify(readFile);
// Native Chromium DOM + keyboard + synthetic File; no host app/profile mutation.
test('native DOM: three controls, auto catalog, native File and keyboard single-click restore', async()=>{
 const browser=await launchOwnedElectron();
 try {
 const page=await browser.newPage(); const failures=[];page.on('pageerror',e=>failures.push(e.message));
 await page.goto('finix-fixture://dom/');
 const fixture=JSON.parse(await read(new URL('./fixtures/operation-http.json',import.meta.url),'utf8'));
 await page.evaluate(f=>{window.httpFixture=f;},fixture);
 await page.evaluate(()=>{
  window.calls=[];window.contributions=[];let operation=null;let pending=false;const seen=new Set();
  const atom=value=>({get:()=>value,set:v=>{value=v;if(!pending){pending=true;queueMicrotask(()=>{pending=false;window.render?.();});}}});
  window.sdk={useQuery:q=>{if(q.enabled&&q.refetchInterval)window.jobPoll=q.queryFn;const k=JSON.stringify(q.queryKey);if(q.enabled!==false&&!seen.has(k)){seen.add(k);queueMicrotask(()=>Promise.resolve(q.queryFn()).catch(()=>{}));}return {};},atom,useValue:a=>a.get(),Button:'button',gatewayActivationEpoch:()=>1,host:{state:{profile:atom('default'),connectionId:atom('local'),gateway:atom('open')},activeConnectionId:()=> 'local',connections:async()=>[{id:'local',kind:'local'}],ensureAgent:async(id,name)=>{calls.push(['activate',id,name]);sdk.host.state.profile.set(name);},profileRoutes:async()=>[{connectionId:'local',profile:window.httpFixture.name,targetProfile:window.httpFixture.name,mode:'local'}],requestProfile:async(route,method,params)=>({name:params.name})}};
  window.jsx=(type,props)=>({type,props});
  window.ctx={register:c=>contributions.push(c),onDispose(){},files:{requestFolder:async()=>({ok:true,folder:{id:'target',path:'C:\\synthetic-target',name:'target'}}),list:async()=>({ok:true,entries:[],truncated:false})},rest:async(p,o)=>{calls.push([p,o]);const f=window.httpFixture;if(p==='/status')return{profile_id:'default',recipient:null};if(p==='/recovery/capabilities')return{code_version:'recovery-v1',operation_protocol:'recovery-operations-v1',local_only:true,profile_id:f.profile_id};if(p==='/recovery/catalog')return{profile_id:f.profile_id,items:[{id:'b'.repeat(64),label:'Copy',complete:true}]};if(p==='/recovery/operations/current')return operation;if(p==='/recovery/operations'){operation={...f,state:'running',phase:'awaiting_key',cancellable:true,request_id:o.body.request_id,receipt:undefined};return operation;}if(p.includes('/key?')){operation={...f,request_id:operation.request_id};return operation;}if(p==='/recovery/operations/'+f.id)return operation;return {};}};
  function dom(n){if(n==null||n===false)return document.createTextNode('');if(Array.isArray(n)){const f=document.createDocumentFragment();n.forEach(x=>f.append(dom(x)));return f;}if(typeof n!=='object')return document.createTextNode(n);if(typeof n.type==='function')return dom(n.type(n.props));const el=document.createElement(n.type);for(const[k,v]of Object.entries(n.props??{})){if(k==='children')continue;if(k.startsWith('on'))el.addEventListener(k.slice(2).toLowerCase(),e=>Promise.resolve(v({currentTarget:el,target:e.target})));else if(k==='style')Object.assign(el.style,v);else if(['value','checked','disabled'].includes(k))el[k]=v;else if(v!=null)el.setAttribute(k,v);}el.append(dom(n.props?.children));return el;}
  window.render=()=>{const route=contributions.find(c=>c.id==='recovery-page');if(route)document.querySelector('#app').replaceChildren(dom(route.render()));};
 });
 const data=s=>'data:text/javascript;base64,'+Buffer.from(s).toString('base64');
 const mapping={'@hermes/plugin-sdk':data('const s=window.sdk;export const {atom,useValue,Button,gatewayActivationEpoch,host,useQuery}=s;'), 'react':data('export function useEffect(){}'), 'react/jsx-runtime':data('export const jsx=window.jsx,jsxs=window.jsx;')};
 await page.addScriptTag({type:'importmap',content:JSON.stringify({imports:mapping})});
 const source=await read(new URL('../desktop/plugin.js',import.meta.url),'utf8');
 await page.addScriptTag({type:'module',content:source.replace(/export\s*\{\s*plugin_default as default\s*\};?/, 'plugin_default.register(window.ctx);window.render();')});
 await page.getByRole('heading',{name:'Восстановление Finix',exact:true}).waitFor();
 assert.equal(await page.locator('select').count(),1);assert.equal(await page.locator('input').count(),1);assert.equal(await page.locator('button').count(),1);
 await page.waitForFunction(()=>calls.some(([p])=>p==='/recovery/catalog'));assert.equal(await page.evaluate(()=>calls.some(([,o])=>o?.method==='POST')),false);
 await page.getByLabel('Файл ключа восстановления',{exact:true}).setInputFiles({name:'synthetic-recovery.txt',mimeType:'text/plain',buffer:Buffer.from('SYNTHETIC-KEY')});
 assert.equal(await page.evaluate(()=>calls.filter(([p])=>p.includes('/key?')).length),0);
 const start=page.getByRole('button',{name:'Восстановить',exact:true});await start.focus();await page.keyboard.press('Enter');await page.waitForFunction(()=>calls.some(([p])=>p.includes('/key?')));await page.evaluate(()=>window.jobPoll());
 await page.getByRole('status').filter({hasText:'Восстановленный профиль открыт'}).waitFor({timeout:5000}).catch(async e=>{throw Error(JSON.stringify({failures,text:await page.locator('body').innerText(),paths:await page.evaluate(()=>calls.map(x=>x[0]))}));});
 assert.equal(await page.evaluate(()=>calls.filter(([p])=>p.includes('/key?')).length),1);
 assert.match(await page.locator('body').innerText(),/История проверена/);assert.deepEqual(failures,[]);
 }finally{await browser.close();}
});
