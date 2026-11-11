import test from 'node:test';
import assert from 'node:assert/strict';
import {controller} from './recovery-operation.test.mjs';
import {harness} from './recovery.test.mjs';
test('shipped warning precedes key input with one Restore and no extra fields',async()=>{
 const h=await harness({rest:p=>p==='/recovery/capabilities'?{code_version:'recovery-v1',operation_protocol:'recovery-operations-v1',profile_id:'bound',local_only:true,bootstrap_condition:condition}:undefined});
 const nodes=h.nodes();
 assert.match(h.text(),/другой процесс вашей учётной записи ОС не должен одновременно заменять/);
 assert.equal(nodes.filter(n=>n.type==='select').length,1);
 assert.equal(nodes.filter(n=>n.type==='input').length,1);
 assert.equal(nodes.filter(n=>n.type==='button').length,1);
 const warning=nodes.findIndex(n=>n.props?.role==='note');
 const key=nodes.findIndex(n=>n.type==='input');
 assert.ok(warning>=0&&warning<key);
 assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);
});
// Guidance is conditional prose, not backend authority or a cached mode flag.
const RU='При восстановлении через общий backend другой процесс вашей учётной записи ОС не должен одновременно заменять создаваемый профиль.';
const EN='When restoring through a shared backend, another process of your OS user must not concurrently replace the new recovered profile.';
function check(nodes,text=RU){
 const note=nodes.findIndex(n=>n.type==='p'&&n.props.children===text);
 const picker=nodes.findIndex(n=>n.type==='input'&&n.props.type==='file');
 assert.ok(note>=0&&note<picker,'conditional guidance visible BEFORE native picker, independently of geometry');
 assert.equal(nodes[note].props.hidden,undefined);
 assert.equal(nodes[note].props.style?.display,undefined);
}
async function sourceView(recovery){
 const vm=await import('node:vm'),{readFile}=await import('node:fs/promises');
 const jsx=(type,props)=>({type,props});
 const sdk={useValue:a=>a.get(),host:{state:{profile:{get:()=> 'source'}}},Button:'button'};
 const modules={'@hermes/plugin-sdk':sdk,'./i18n.mjs':{useLocalized:()=>({jsx,jsxs:jsx}),scopedLanguage:()=> 'ru'},'react/jsx-runtime':{jsx,jsxs:jsx},react:{useEffect(){}},'./recovery-operation.mjs':{createRecovery(){}}};
 const entry=new vm.SourceTextModule(await readFile(new URL('../desktop/src/recovery.mjs',import.meta.url),'utf8'));
 await entry.link(n=>{const m=modules[n];return new vm.SyntheticModule(Object.keys(m),function(){for(const[k,v]of Object.entries(m))this.setExport(k,v);});});await entry.evaluate();
 const flatten=n=>Array.isArray(n)?n.flatMap(flatten):n&&typeof n==='object'?[n,...flatten(n.props?.children)]:[];
 return ()=>flatten(entry.namespace.Recovery({recovery}));
}
for(const mode of ['source','bundle'])test(`${mode}: invariant guidance covers geometry changes at every fresh sensitive boundary`,async()=>{
 let root=false,reads=0,prepare;
 const cap=()=>({code_version:'recovery-v1',operation_protocol:'recovery-operations-v1',profile_id:'bound',local_only:true,bootstrap_condition:root?condition:null});
 if(mode==='source'){
  const h=await controller({rest:p=>p.endsWith('/capabilities')?(reads++,cap()):undefined});
  const nodes=await sourceView(h.recovery);check(nodes());await h.recovery.actions.load();check(nodes());
  h.recovery.actions.candidate('b'.repeat(64));check(nodes());
  assert.equal(h.recovery.state.get().bootstrapCondition,null);root=true;
  const a=h.recovery.view().actions;check(nodes());a.key({size:8,arrayBuffer:async()=>{check(nodes());return new ArrayBuffer(8);}});check(nodes());
  await a.start({candidate:'b'.repeat(64),complete:true});check(nodes());
  assert.equal(h.calls.filter(([p])=>p.includes('/key?')).length,1);assert.equal(reads,1);
  h.change();check(nodes());await a.start({candidate:'b'.repeat(64),complete:true});a.key({size:8});check(nodes());h.dispose();await a.start({});check(nodes());
  assert.equal(h.calls.filter(([p])=>p.includes('/key?')).length,1);
 }else{
  const {defer}=await import('./recovery.test.mjs');const gate=defer();
  const h=await harness({rest:(p,o,x)=>p==='/recovery/capabilities'?(reads++,cap()):p==='/recovery/operations'?(prepare=x.row({request_id:o.body.request_id}),x.set({...prepare,phase:'preparing'}),gate.promise):undefined});
  check(h.nodes());assert.equal(h.nodes().filter(n=>['input','select','button'].includes(n.type)).length,3);
  root=true;check(h.nodes());await h.key({size:8,arrayBuffer:async()=>{check(h.nodes());return new ArrayBuffer(8);}});check(h.nodes());
  root=false;const retained=h.button('Восстановить').props.onClick;const running=retained();await h.settle();check(h.nodes());assert.equal(h.calls.filter(([p])=>p.includes('/key?')).length,0);root=true;gate.resolve(prepare);await running;await h.settle();check(h.nodes());
  assert.equal(h.calls.filter(([p])=>p.includes('/key?')).length,1);assert.equal(reads,1);
  h.switch();check(h.nodes());await retained();h.dispose();await retained();assert.equal(h.calls.filter(([p])=>p.includes('/key?')).length,1);
 }
});
for(const metadata of [undefined,null,{contract:'unexpected'}])test(`bundle: guidance survives disabled or unknown capability ${JSON.stringify(metadata)}`,async()=>{
 const h=await harness({rest:p=>p==='/recovery/capabilities'?{code_version:'unknown',bootstrap_condition:metadata}:undefined});check(h.nodes());assert.equal(h.input('Файл ключа восстановления').props.disabled,true);assert.equal(h.calls.some(([,o])=>o?.method==='POST'),false);
});
test('bundle: English conditional guidance precedes file picker',async()=>{
 const h=await harness({rest:p=>p==='/ui-language'?{ui_language:'en'}:undefined});check(h.nodes(),EN);assert.equal(h.nodes().filter(n=>['input','select','button'].includes(n.type)).length,3);
});
const condition={contract:'cooperative-root-boundaries-v1',condition:'no-concurrent-same-user-target-replacement',atomic_target_binding:false};
test('known root condition is available before fresh key choice; no extra authority sent',async()=>{
 const h=await controller({rest:p=>p.endsWith('/capabilities')?{code_version:'recovery-v1',operation_protocol:'recovery-operations-v1',profile_id:'bound',local_only:true,bootstrap_condition:condition}:undefined});
 await h.recovery.actions.load();
 assert.equal(h.recovery.state.get().ready,true);
 assert.equal(h.recovery.state.get().key,false);
 assert.equal(h.recovery.state.get().bootstrapCondition,'cooperative-root-boundaries-v1');
 h.recovery.actions.key({size:8,arrayBuffer:async()=>new ArrayBuffer(8)});
 await h.recovery.actions.start({candidate:'b'.repeat(64),complete:true});
 const body=h.calls.find(([p])=>p==='/recovery/operations')[1].body;
 assert.deepEqual(Object.keys(body).sort(),['acknowledge_incomplete','candidate_id','consent_restore','profile_id','request_id']);
 assert.equal(body.consent_restore,true);
});
