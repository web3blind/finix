import test from 'node:test';
import assert from 'node:assert/strict';
import {harness} from './default-maintenance.test.mjs';
function nodes(x){return Array.isArray(x)?x.flatMap(nodes):x&&typeof x==='object'?[x,...nodes(x.props?.children)]:[];}
test('temporary initial GET refusal permits manual read-only check through the same primary control',async()=>{
 let attempts=0;
 const h=await harness({rest:(p)=>{if(p==='/recovery/default/capabilities'&&++attempts===1)throw Error('409: {"detail":"PROFILE_UNAVAILABLE"}');}});
 await h.maintenance.actions.load();assert.equal(h.maintenance.state.get().ready,false);
 const check=nodes(h.render()).find(x=>x.props?.children==='Check maintenance availability');
 assert.ok(check,'primary control must offer a check, not stay disabled forever');assert.equal(check.props.disabled,false);
 await check.props.onClick();assert.equal(h.maintenance.state.get().ready,true);
 assert.equal(attempts,2);assert.ok(h.calls.every(([,o])=>!o?.method||o.method==='GET'));
 const prepare=nodes(h.render()).find(x=>x.props?.children==='Prepare physical-default maintenance preview');
 assert.ok(prepare);assert.equal(prepare.props.disabled,true,'fresh local key is still required');
});
