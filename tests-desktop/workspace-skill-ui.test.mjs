import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';

// Source-only regression: CI must separately rebuild/test desktop/plugin.js.
const source = await readFile(new URL('../desktop/src/cloud.mjs', import.meta.url), 'utf8');
const atom = value => ({get:()=>value,set:next=>{value=next;}});
async function harness() {
  const profile=atom('default'),calls=[];
  const sdk={atom,useValue:s=>s.get(),Button:'button',gatewayActivationEpoch:()=>1,
    host:{state:{profile,gateway:atom('open')}}};
  const jsx=(type,props)=>({type,props});
  const context=vm.createContext({console});
  const modules={'@hermes/plugin-sdk':sdk,'react':{useEffect:()=>{}},
    'react/jsx-runtime':{jsx,jsxs:jsx},'./i18n.mjs':{useLocalized:()=>({jsx,jsxs:jsx,language:'en'})}};
  const entry=new vm.SourceTextModule(source,{context});
  await entry.link(specifier=>{
    assert.ok(Object.hasOwn(modules,specifier));
    const values=modules[specifier];
    return new vm.SyntheticModule(Object.keys(values),function(){for(const [k,v] of Object.entries(values))this.setExport(k,v);},{context});
  });
  await entry.evaluate();
  const ctx={onDispose:()=>{},rest:async(path,opts)=>{
    calls.push([path,opts]);
    if(path==='/readiness')return {};
    return {profile_id:'bound',ticket:'ticket',session:{status:'idle'}};
  }};
  const cloud=entry.namespace.createCloud(ctx);
  const flatten=n=>Array.isArray(n)?n.flatMap(flatten):n&&typeof n==='object'?[n,...flatten(n.props?.children)]:[n];
  const nodes=()=>flatten(entry.namespace.Cloud({cloud,enabled:true}));
  await cloud.refresh();
  return {cloud,calls,nodes};
}

test('cloud source offers only own Google and supported rclone providers',async()=>{
  const h=await harness();
  const options=h.nodes().filter(n=>n?.type==='option').map(n=>n.props.value);
  assert.deepEqual(options,['workspace','yandex','onedrive','dropbox']);
  assert.equal(h.cloud.state.get().provider,'workspace');
  assert.equal(h.calls.filter(([,opts])=>opts?.method==='POST').length,0);
  assert.equal(h.calls.filter(([path])=>path==='/readiness').length,0);
  const text=h.nodes().filter(n=>typeof n==='string').join('\n');
  assert.match(text,/finix:google/);
  assert.match(text,/drive.file/);
  assert.doesNotMatch(text,/Google Workspace SKILL/);
});

test('rclone choices still query readiness and clear inherited consent',async()=>{
  const h=await harness();
  h.cloud.set({consent:true,local:true});
  await h.cloud.select('yandex');
  assert.equal(h.cloud.state.get().consent,false);
  assert.equal(h.cloud.state.get().local,false);
  assert.ok(h.calls.some(([path])=>path==='/readiness'));
  assert.equal(h.calls.filter(([,opts])=>opts?.method==='POST').length,0);
});

test('removed and unknown provider injections cannot select a backend',async()=>{
  const h=await harness();
  const reads=h.calls.length;
  for(const provider of ['workspace-skill','unknown']){
    await h.cloud.select(provider);
    assert.equal(h.cloud.state.get().provider,'workspace');
  }
  assert.equal(h.calls.length,reads);
});
