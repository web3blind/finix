import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
import { readFile } from 'node:fs/promises';
const bundle = await readFile(new URL('../desktop/plugin.js', import.meta.url), 'utf8');
const atom = value => ({ get: () => value, set: x => { value = x; } });
const defer = () => { let resolve; return { promise: new Promise(r => { resolve = r; }), resolve: x => resolve(x) }; };
async function harness(o = {}) {
  let operation=null, selected=false, readyReceipt=false;
  const row=(patch={})=>({id:'a'.repeat(32),request_id:operation?.request_id??'11111111-1111-4111-8111-111111111111',profile_id:'bound',state:'running',phase:'awaiting_key',name:'restored',cancellable:true,cancel_requested:false,full_acceptance:false,...patch});
  const receipt=(patch={})=>({name:'restored',served:'restored',backend_target_ready:true,archive_verified:true,content_verified:true,integrations_paused:true,history_pending:false,full_acceptance:false,contract:'sdk-new-content-copy-v1',...patch});
  let epoch = 1, source = 'local'; const queries=new Map(),seen=new Set();
  const profile = atom('default'), connectionId = atom('local'), contributions = [], calls = [], disposers = [];
  const sdk = { useQuery:q=>{const k=JSON.stringify(q.queryKey);queries.set(k,q);if(q.enabled!==false&&!seen.has(k)){seen.add(k);queueMicrotask(()=>o.queryRunner?o.queryRunner(q):Promise.resolve(q.queryFn()).catch(()=>{}));}return {};}, atom, useValue: a => a.get(), Button: 'button', gatewayActivationEpoch: () => epoch, host: {
    state: { profile, connectionId, gateway: atom('open') }, activeConnectionId: () => source,
    connections: async () => o.registry ? o.registry() : [{ id: 'local', kind: 'local' }],
    profileRoutes: async () => o.routes ? o.routes() : [{ connectionId:'local', profile:profile.get(), targetProfile:profile.get(), mode:'local' }],
    requestProfile:async(route,method,params)=>{calls.push(['describe',route,method,params]);return o.describe?o.describe(route,method,params):{name:params.name};},
    ensureAgent: async (id, name) => { calls.push(['activate', id, name]); selected=name!=='default'; source = id; connectionId.set(id); profile.set(name); epoch++; await o.onActivate?.(); }, navigate() {} } };
  if(o.noDescribe)delete sdk.host.requestProfile;
  const jsx = (type, props) => ({ type, props });
  const modules = { '@hermes/plugin-sdk': sdk, 'react': { useEffect() {} }, 'react/jsx-runtime': { jsx, jsxs: jsx } };
  const context = vm.createContext({ crypto:webcrypto, TextEncoder, TextDecoder, Uint8Array, ArrayBuffer, setTimeout, clearTimeout, queueMicrotask });
  const entry = new vm.SourceTextModule(bundle, { context });
  await entry.link(s => { const m = modules[s]; assert.ok(m, s); return new vm.SyntheticModule(Object.keys(m), function () { for (const [k,v] of Object.entries(m)) this.setExport(k,v); }, { context }); });
  await entry.evaluate();
  const ctx = { register: x => contributions.push(x), onDispose: f => disposers.push(f),
    files: { requestFolder: async () => o.pick ? o.pick() : { ok:true, folder:{id:'grant',name:'folder',path:o.path ?? 'C:\\restore'} }, list: async () => ({ok:true,entries:o.entries ?? [{name:'backup.age',path:'backup.age',isDirectory:false}]}) },
    rest: (p, opts) => { calls.push([p,opts]); if(p.startsWith('/recovery'))assert.equal(selected,false,'ZERO SOURCE REST after ensureAgent'); if (o.rest) { const result = o.rest(p,opts,{row,receipt,current:()=>operation,set:v=>operation=v}); if (result !== undefined) return Promise.resolve(result).then(value=>{if(p.startsWith('/recovery/operations')&&value?.id)operation=value;return value;}); }
      // Locale is read from the currently bound profile, never a global shell default.
      if (p === '/ui-language') { assert.equal(opts?.method,undefined,'locale attestation is read-only'); return Promise.resolve({ui_language:o.locale??'ru'}); }
      if (p === '/status') return Promise.resolve({profile_id:profile.get(),recipient:null});
      if (p === '/recovery/capabilities') return Promise.resolve({code_version:'recovery-v1',operation_protocol:'recovery-operations-v1',profile_id:profile.get()==='default'?'bound':'bound-'+profile.get(),local_only:true});
      if (p === '/recovery/catalog') return Promise.resolve({profile_id:'bound',items:[{id:'b'.repeat(64),label:'Copy',source:'cloud',size:100,sha256:'a'.repeat(64),complete:o.catalogComplete===undefined?true:o.catalogComplete,created_at:'2026-11-08T00:00:00Z',omitted_count:2}]});
      if(p==='/recovery/operations/current')return Promise.resolve(operation);
      if(p==='/recovery/operations'){operation=row({request_id:opts.body.request_id});return Promise.resolve(operation);}
      if(p.includes('/operations/')&&p.includes('/key?')){operation=row({phase:'data_queued'});readyReceipt=true;return Promise.resolve(operation);}
      if(p.includes('/operations/')&&p.endsWith('/cancel')){operation={...operation,state:'cancelled',phase:'cancelled',cancellable:false,cancel_requested:true};return Promise.resolve(operation);}
      if(p==='/recovery/operations/'+('a'.repeat(32))){if(readyReceipt){readyReceipt=false;operation=row({state:'succeeded',phase:'backend_target_ready',cancellable:false,receipt:receipt()});}return Promise.resolve(operation);}
      if (/prepare-auto|\/jobs\//.test(p)) assert.fail('obsolete independent mutation protocol: '+p);
      return Promise.resolve({}); }
  };
  entry.namespace.default.register(ctx);
  for(let i=0;i<12;i++) await Promise.resolve();
  function flatten(n) { if(Array.isArray(n)) return n.flatMap(flatten); if(!n || typeof n !== 'object') return [n]; if(typeof n.type==='function') return flatten(n.type(n.props)); return [n,...flatten(n.props?.children)]; }
  const view = () => flatten(contributions.find(x=>x.id==='recovery-page')?.render());
  const nodes = () => view().filter(x=>x && typeof x==='object');
  const button = label => { const b=nodes().find(x=>x.type==='button'&&x.props.children===label); assert.ok(b,`Missing ${label}`); return b; };
  const settle=async()=>{for(let i=0;i<100;i++){view();await Promise.resolve();}};
  const click = async label => { const b=button(label); assert.equal(!!b.props.disabled,false,label); await b.props.onClick();await settle(); };
  const input = label => nodes().find(x=>x.type==='input'&&x.props['aria-label']===label);
  const change = async (label,value) => { const target={value,checked:value,files:Array.isArray(value)?value:undefined}; await input(label).props.onChange({currentTarget:target}); return target; };
  await settle();
  return {row,receipt,setOperation:v=>{operation=v;},settle,tick:async()=>{for(const q of queries.values())if(q.enabled&&q.refetchInterval)await q.queryFn();await settle();},choose:async id=>{nodes().find(x=>x.type==='select').props.onChange({currentTarget:{value:id}});await settle();},calls,contributions,nodes,button,click,input,change,text:()=>view().filter(x=>typeof x==='string').join('\n'),
    switchProfile:name=>{profile.set(name);epoch++;},
    switch: (id='remote')=>{source=id;connectionId.set(id);epoch++;}, dispose:()=>disposers.forEach(f=>f()),
    ready:settle,
    key:async(f={size:8,arrayBuffer:async()=>new TextEncoder().encode('SENTINEL').buffer})=>change('Файл ключа восстановления', [f])};
}

export { harness, defer };
