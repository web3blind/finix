import test from 'node:test';
import assert from 'node:assert/strict';
import {harness,defer} from './recovery.test.mjs';
for(const scope of ['profile','source'])for(const phase of ['prepare','status','import'])test(`unexpected ${scope} change during ${phase} stops automatic continuation`,async()=>{
 const gate=defer();let reached=false,answer;
 const h=await harness({rest:(p,o,{row,receipt})=>{
  if(phase==='prepare'&&p==='/recovery/operations'){reached=true;answer=row({request_id:o.body.request_id});return gate.promise;}
  if(p.includes('/key?'))return row({phase:phase==='import'?'importing':'data_running',cancellable:phase!=='import'});
  if(p==='/recovery/operations/'+('a'.repeat(32))){reached=true;answer=row({state:'succeeded',phase:'backend_target_ready',cancellable:false,receipt:receipt()});return gate.promise;}
 }});
 await h.key();const started=h.button('Восстановить').props.onClick();
 if(phase!=='prepare')await started;
 const polled=phase==='prepare'?Promise.resolve():h.tick();
 for(let i=0;i<100;i++){await Promise.resolve();h.nodes();if(reached)break;}
 assert.equal(reached,true,'must reach the intended await, not fail KEY_DISPATCH: '+JSON.stringify(h.calls.map(x=>x[0]))+' '+h.text());
 if(scope==='profile')h.switchProfile('another');else h.switch('ssh');
 gate.resolve(answer);await started;await polled;await h.settle();
 const paths=h.calls.map(([p])=>p);
 if(phase==='prepare')assert.equal(paths.some(p=>p.includes('/key?')),false);
 else assert.equal(paths.filter(p=>p.includes('/key?')).length,1);
 assert.equal(paths.filter(p=>p==='/recovery/operations').length,1);
 assert.equal(paths.some(p=>/prepare-auto|import-auto|\/start$/.test(p)),false);
 assert.equal(paths.includes('activate'),false);
 assert.equal(h.nodes().some(n=>n.type==='button'&&n.props.children==='Назад к предыдущему профилю'),false);
});
