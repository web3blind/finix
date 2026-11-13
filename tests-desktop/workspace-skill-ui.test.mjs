import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
import {webcrypto} from 'node:crypto';
import {generateKeyPair} from '../web/keys.mjs';
const publicKey = (await generateKeyPair()).recipient;

// Exercise the real shipped module through public SDK registration; no OAuth,
// cloud tools, credentials, profile files or native Desktop bootstrap.
const bundle = await readFile(new URL('../desktop/plugin.js', import.meta.url), 'utf8');
const atom = value => ({get:()=>value,set:next=>{value=next;}});
const deferred = () => {let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};};
async function harness({language='en', rest}={}) {
  let epoch=1, effects=[], status='idle';
  const profile=atom('default'), calls=[], opened=[], contributions=[], disposers=[];
  const sdk={atom,useValue:s=>s.get(),Button:'button',ROUTES_AREA:'routes',SIDEBAR_NAV_AREA:'sidebar.nav',PALETTE_AREA:'palette',
    gatewayActivationEpoch:()=>epoch,host:{state:{profile,gateway:atom('open')},navigate:()=>{}}};
  const jsx=(type,props)=>({type,props});
  const context=vm.createContext({crypto:webcrypto,TextEncoder,TextDecoder,Uint8Array,Uint32Array,ArrayBuffer,DataView,setTimeout,clearTimeout,console});
  const modules={'@hermes/plugin-sdk':sdk,'react':{useEffect:fn=>effects.push(fn)},'react/jsx-runtime':{jsx,jsxs:jsx}};
  const entry=new vm.SourceTextModule(bundle,{context});
  await entry.link(specifier=>{
    assert.ok(Object.hasOwn(modules,specifier),`Nonpublic import: ${specifier}`);
    const values=modules[specifier];
    return new vm.SyntheticModule(Object.keys(values),function(){for(const [k,v] of Object.entries(values))this.setExport(k,v);},{context});
  });
  await entry.evaluate();
  const envelope=()=>({profile_id:'bound',ticket:'public-ticket',session:{id:status==='idle'?'':'owned-session',status}});
  const ctx={register:c=>contributions.push(c),onDispose:fn=>disposers.push(fn),
    storage:{get:()=>assert.fail('private storage'),set:()=>assert.fail('private storage')},
    os:{openExternal:async url=>{opened.push(url);return true;}},
    rest:async(path,opts)=>{
      calls.push([path,opts]);
      if(path==='/ui-language')return {ui_language:language};
      if(path==='/status')return {profile_id:'bound',recipient:publicKey};
      if(path==='/connection'){
        if(rest){const result=await rest(path,opts,envelope);if(result!==undefined)return result;}
        if(opts?.method==='POST')status=opts.body.action==='apply'?'applied':opts.body.action==='cancel'?'cancelled':'verified';
        return envelope();
      }
      return {};
    }};
  entry.namespace.default.register(ctx);
  const settle=async()=>{for(let i=0;i<24;i++)await Promise.resolve();};
  await settle();
  const root=()=>{const n=contributions.find(c=>c.id==='pane').render();return n.type(n.props);};
  const cloud=()=>root().props.children.find(n=>n?.props?.cloud);
  const flatten=n=>Array.isArray(n)?n.flatMap(flatten):n&&typeof n==='object'?[n,...flatten(n.props?.children)]:[n];
  const nodes=()=>{const n=cloud();return flatten(n.type(n.props));};
  const text=()=>nodes().filter(n=>typeof n==='string').join('\n');
  const button=label=>nodes().find(n=>n?.type==='button'&&n.props.children===label);
  const select=()=>nodes().find(n=>n?.type==='select');
  const confirm=()=>nodes().filter(n=>n?.type==='input'&&n.props.type==='checkbox').forEach(n=>n.props.onChange({currentTarget:{checked:true}}));
  async function mount(){effects=[];nodes();const pending=effects;effects=[];pending.forEach(fn=>fn());await settle();}
  await mount();
  return {calls,opened,text,nodes,button,select,confirm,mount,settle,cloud:()=>cloud().props.cloud,
    choose:async()=>{await select().props.onChange({currentTarget:{value:'workspace-skill'}});},
    switchProfile:()=>{profile.set('other');epoch++;},switchEpoch:()=>{epoch++;},dispose:()=>disposers.forEach(fn=>fn())};
}
for (const language of ['en','ru']) for(const mode of ['session','exception']) test(`SKILL setup refusal is closed and localized (${language}, ${mode})`,async()=>{
  let hReady=false;
  const h=await harness({language,rest:async(path,opts,envelope)=>{
    if(opts?.method==='POST'){
      hReady=true;
      const dirty='GOOGLE_WORKSPACE_SKILL_SETUP_REQUIRED token=SECRET_PROVIDER_BODY C:/private/credentials';
      if(mode==='exception')throw new Error(dirty);
      return {...envelope(),session:{id:'owned-session',status:'failed',code:'GOOGLE_WORKSPACE_SKILL_SETUP_REQUIRED',message:dirty}};
    }
    if(mode==='session'&&hReady)return {profile_id:'bound',ticket:'public-ticket',session:{id:'owned-session',status:'failed',code:'GOOGLE_WORKSPACE_SKILL_SETUP_REQUIRED',message:'SECRET_PROVIDER_BODY'}};
  }});
  await h.choose();h.confirm();await h.button(language==='en'?'Start connection':'Начать подключение').props.onClick();
  assert.match(h.text(),/GOOGLE_WORKSPACE_SKILL_SETUP_REQUIRED/);
  assert.match(h.text(),language==='en'?/trusted Google Workspace SKILL/:/доверенный Google Workspace SKILL/);
  assert.doesNotMatch(h.text(),/SECRET_PROVIDER_BODY|credentials|token=|C:\/private/);
  assert.equal(h.button(language==='en'?'Apply connection':'Применить подключение').props.disabled,true);
  assert.equal(h.opened.length,0);
});

const posts=h=>h.calls.filter(([p,o])=>p==='/connection'&&o?.method==='POST');

for(const boundary of ['profile','epoch','dispose']) test(`pending SKILL verification and retained actions cannot cross ${boundary}`,async()=>{
  const pending=deferred();let started=false;
  const h=await harness({rest:async(path,opts,envelope)=>{
    if(opts?.method==='POST'){started=true;return pending.promise;}
    if(started)return {...envelope(),session:{id:'owned-session',status:'verified'}};
  }});
  await h.choose();h.confirm();
  const oldStart=h.button('Start connection').props.onClick;
  const oldApply=h.button('Apply connection').props.onClick;
  const oldCancel=h.button('Cancel connection').props.onClick;
  const work=oldStart();await h.settle();assert.equal(posts(h).length,1);
  if(boundary==='profile')h.switchProfile();else if(boundary==='epoch')h.switchEpoch();else h.dispose();
  assert.equal(h.cloud().isCurrent(),false);
  assert.equal(h.button('Apply connection').props.disabled,true);
  await oldCancel();assert.equal(posts(h).length,1,'pending work cannot dispatch cancellation through a stale callback');
  pending.resolve({profile_id:'bound',ticket:'public-ticket',session:{id:'owned-session',status:'verified',url:'https://accounts.example.test/old'}});
  await work;
  assert.equal(h.cloud().state.get().data,null,'stale response discarded');
  await oldApply();await oldStart();await oldCancel();
  assert.equal(posts(h).length,1);
  assert.equal(h.opened.length,0);
  assert.doesNotMatch(h.text(),/https:\/\/accounts.example.test\/old/);
});

test('SKILL provider change supersedes an earlier read without inheriting consent or authorization URL',async()=>{
  const pending=deferred();let hold=false;
  const h=await harness({rest:async(path,opts)=>{if(hold&&!opts)return pending.promise;}});
  h.confirm();hold=true;
  const obsolete=h.cloud().refresh();await h.settle();
  hold=false;await h.choose();
  pending.resolve({profile_id:'bound',ticket:'old-ticket',session:{id:'old',status:'authorize',url:'https://accounts.example.test/obsolete'}});
  await obsolete;
  assert.equal(h.select().props.value,'workspace-skill');
  assert.equal(h.cloud().state.get().consent,false);
  assert.equal(h.cloud().state.get().local,false);
  assert.equal(h.button('Start connection').props.disabled,true);
  assert.doesNotMatch(h.text(),/obsolete|old-ticket/);
  assert.equal(posts(h).length,0);
});

test('SKILL verified cancellation is explicit and invalidates a retained Apply callback',async()=>{
  const h=await harness();await h.choose();h.confirm();await h.button('Start connection').props.onClick();
  const apply=h.button('Apply connection').props.onClick;
  await h.button('Cancel connection').props.onClick();
  assert.equal(posts(h).at(-1)[1].body.action,'cancel');
  await apply();assert.equal(posts(h).length,2);
  assert.equal(h.button('Apply connection').props.disabled,true);
});

for (const language of ['en','ru']) test(`Workspace SKILL permissions and setup dependency are accurate (${language})`,async()=>{
  const h=await harness({language});await h.choose();
  const visible=h.nodes().filter(n=>n?.type==='p').map(n=>n.props.children).join('\n');
  assert.match(visible,language==='en'?/Workspace SKILL.*broader permissions/:/Workspace SKILL.*широкие права/);
  assert.match(visible,language==='en'?/does not start OAuth/:/не запускает OAuth/);
  assert.match(h.text(),language==='en'?/configured and authorized.*selected profile/:/настроен и авторизован.*выбранном профиле/);
  assert.match(h.text(),language==='en'?/Finix still captures, encrypts and verifies/:/Finix.*снимок, шифрует и проверяет/);
  assert.doesNotMatch(visible,/rclone provider permissions|only the Finix drive.file grant/);
  assert.equal(h.button(language==='en'?'Start connection':'Начать подключение').props.disabled,true);
  assert.equal(h.nodes().find(n=>n?.type==='section').props.lang,language);
  const notice=h.nodes().findIndex(n=>n?.type==='p' && typeof n.props.children==='string' && n.props.children.startsWith('Google Workspace SKILL'));
  assert.ok(notice>=0 && notice<h.nodes().findIndex(n=>n?.type==='details'),'permission notice is outside collapsed Help');
});

test('shipped wizard exposes a separate Workspace SKILL choice, checks only on Start and explicitly applies',async()=>{
  const h=await harness();
  assert.equal(h.select().props.value,'workspace');
  const options=h.nodes().filter(n=>n?.type==='option');
  assert.equal(options.find(n=>n.props.value==='workspace-skill')?.props.children,'Google Drive (Google Workspace SKILL)');
  await h.choose();
  assert.equal(posts(h).length,0);
  assert.equal(h.calls.filter(([p])=>p==='/readiness').length,0,'neither Google provider needs rclone');
  const start=h.button('Start connection').props.onClick;
  await start();assert.equal(posts(h).length,0,'retained disabled Start needs both confirmations');
  h.confirm();await h.button('Start connection').props.onClick();
  assert.deepEqual(JSON.parse(JSON.stringify(posts(h)[0][1].body)),{profile_id:'bound',ticket:'public-ticket',action:'start',session_id:'',provider:'workspace-skill',folder:'Finix',confirmed:true,local_browser:true});
  assert.equal(posts(h).length,1,'verification cannot automatically apply');
  assert.equal(h.button('Apply connection').props.disabled,false);
  await h.button('Apply connection').props.onClick();
  assert.equal(posts(h)[1][1].body.action,'apply');
  assert.equal(h.opened.length,0);
});
