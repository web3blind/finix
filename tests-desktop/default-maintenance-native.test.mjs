import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {launchOwnedElectron} from './owned-electron.mjs';
// Native Windows Electron DOM, synthetic SDK/transport, real handler-emitted
// preview. Not installed Desktop or a physical Apply acceptance test.
test('native maintenance DOM consumes actual HTTP preview, discloses custody and refuses public Apply',async()=>{
 assert.ok(process.env.FINIX_MAINTENANCE_PREVIEW_PATH,'Supply the generated-identity HTTP preview, never a user plan');
 const actual=JSON.parse(await readFile(process.env.FINIX_MAINTENANCE_PREVIEW_PATH,'utf8'));
 const browser=await launchOwnedElectron();try{
 const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto('finix-fixture://dom/');
 await page.evaluate(actual=>{
  window.actual=actual;window.calls=[];window.contributions=[];let pending=false,jobState='awaiting_key';const seen=new Set();
  const atom=value=>({get:()=>value,set:v=>{value=v;if(!pending){pending=true;queueMicrotask(()=>{pending=false;window.render?.();});}}});
  const profileId='a'.repeat(64),job='9'.repeat(32);const jobView=()=>({id:job,profile_id:profileId,state:jobState,complete:jobState==='succeeded'});
  window.sdk={atom,useValue:a=>a.get(),Button:'button',gatewayActivationEpoch:()=>1,useQuery:q=>{if(q.enabled&&q.refetchInterval)window.jobPoll=q.queryFn;const k=JSON.stringify(q.queryKey);if(q.enabled!==false&&!seen.has(k)){seen.add(k);queueMicrotask(()=>Promise.resolve(q.queryFn()).catch(()=>{}));}return {};},host:{state:{profile:atom('default'),connectionId:atom('local'),gateway:atom('open')},activeConnectionId:()=> 'local',connections:async()=>[{id:'local',kind:'local'}]}};
  window.jsx=(type,props)=>({type,props});
  window.ctx={register:c=>contributions.push(c),onDispose(){},rest:async(p,o)=>{calls.push([p,o]);if(p==='/status')return{profile_id:profileId,recipient:null};if(p==='/recovery/default/capabilities')return{code_version:'default-recovery-v1',supported:true,apply_enabled:false,active_shared_autoruns_supported:false,serverRootToken:window.actual.preview.root_binding};if(p==='/recovery/capabilities')return{code_version:'recovery-v1',profile_id:profileId,local_only:true};if(p==='/recovery/catalog')return{profile_id:profileId,items:[{id:'c'.repeat(64),complete:true,label:'/private/not-displayed'}]};if(p==='/recovery/default/prepare-auto')return jobView();if(p.endsWith('/key')){jobState='succeeded';return jobView();}if(p==='/recovery/jobs/'+job)return jobView();if(p==='/recovery/default/prepare')return window.actual;throw Error('UNEXPECTED_ROUTE');}};
  function dom(n){if(n==null||n===false)return document.createTextNode('');if(Array.isArray(n)){const f=document.createDocumentFragment();n.forEach(x=>f.append(dom(x)));return f;}if(typeof n!=='object')return document.createTextNode(n);if(typeof n.type==='function')return dom(n.type(n.props));const el=document.createElement(n.type);for(const[k,v]of Object.entries(n.props??{})){if(k==='children')continue;if(k.startsWith('on'))el.addEventListener(k.slice(2).toLowerCase(),e=>Promise.resolve(v({currentTarget:el,target:e.target})));else if(k==='style')Object.assign(el.style,v);else if(['value','checked','disabled'].includes(k))el[k]=v;else if(v!=null)el.setAttribute(k,v);}el.append(dom(n.props?.children));return el;}
  window.render=()=>{const page=contributions.find(c=>c.id==='default-maintenance-page');if(page)document.querySelector('#app').replaceChildren(dom(page.render()));};
 },actual);
 const data=s=>'data:text/javascript;base64,'+Buffer.from(s).toString('base64');
 await page.addScriptTag({type:'importmap',content:JSON.stringify({imports:{'@hermes/plugin-sdk':data('export const {atom,useValue,Button,gatewayActivationEpoch,host,useQuery}=window.sdk;'),'react':data('export function useEffect(){}'),'react/jsx-runtime':data('export const jsx=window.jsx,jsxs=window.jsx;')}})});
 const source=await readFile(new URL('../desktop/plugin.js',import.meta.url),'utf8');await page.addScriptTag({type:'module',content:source.replace(/export\s*\{\s*plugin_default as default\s*\};?/,'plugin_default.register(window.ctx);window.render();')});
 await page.getByRole('heading',{name:'Physical-default maintenance',exact:true}).waitFor();await page.waitForFunction(()=>calls.some(([p])=>p==='/recovery/catalog'));
 assert.equal(await page.locator('select').count(),1);assert.equal(await page.locator('input[type=checkbox]').count(),0);
 await page.getByLabel('Maintenance recovery key',{exact:true}).setInputFiles({name:'synthetic.txt',mimeType:'text/plain',buffer:Buffer.from('SYNTHETIC-NOT-A-USER-KEY')});
 await page.getByRole('button',{name:'Prepare physical-default maintenance preview',exact:true}).click();
 await page.getByRole('heading',{name:'Review this exact maintenance plan',exact:true}).waitFor();
 for(const text of ['memories and skills','old-only','shared host resources','Downtime','late-write','retained indefinitely','inert scope','archived plugin code'])assert.ok((await page.locator('body').innerText()).includes(text),text);
 const apply=page.getByRole('button',{name:'Physical-default Apply unavailable — independent review required',exact:true});assert.equal(await apply.isDisabled(),true);
 assert.equal(await page.evaluate(()=>calls.some(([p])=>p==='/recovery/default/apply')),false);assert.equal(await page.evaluate(()=>calls.some(([p])=>p.includes('/operations')||p.endsWith('/start'))),false);
 assert.equal((await page.locator('body').innerText()).includes('/private/not-displayed'),false);assert.ok(await page.evaluate(()=>new Uint8Array(calls.find(([p])=>p.endsWith('/key'))[1].upload.bytes).every(v=>v===0)));assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});
