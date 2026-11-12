import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
import {harness} from './default-maintenance.test.mjs';
// Explicit input: an actually emitted generated-identity ASGI envelope, not a
// handwritten schema. SDK request context remains the VM harness's model.
const envelope=JSON.parse(await readFile(process.argv[2],'utf8'));
assert.equal(envelope.contract,'default-preview-job-v1');assert.equal(envelope.state,'succeeded');
const root=envelope.result.preview.root_binding,job=envelope.id,profile='a'.repeat(64);
const h=await harness({bundle:true,rest:path=>{
 if(path==='/recovery/default/capabilities')return {code_version:'default-recovery-v1',preview_contract:'default-preview-job-v1',supported:true,apply_enabled:false,active_shared_autoruns_supported:false,serverRootToken:root};
 if(path==='/recovery/default/prepare-auto')return {id:job,profile_id:profile,state:'awaiting_key',complete:true};
 if(path.endsWith('/key'))return {id:job,profile_id:profile,state:'running',complete:null};
 if(path===`/recovery/jobs/${job}`)return {id:job,profile_id:profile,state:'succeeded',complete:true};
 if(path==='/recovery/default/prepare')return {contract:'default-preview-job-v1',id:job,state:'running',error:null,result:null,cancellable:true};
 if(path===`/recovery/default/preview/${job}`)return envelope;
}});
await h.start();await h.maintenance.actions.poll();assert.equal(h.maintenance.state.get().preview,null);await h.maintenance.actions.poll();
assert.equal(h.maintenance.state.get().preview.preview.plan_digest,envelope.result.preview.plan_digest);
assert.equal(h.calls.filter(([path])=>path==='/recovery/default/prepare').length,1);
assert.equal(h.calls.filter(([path])=>path.endsWith('/key')).length,1);
assert.equal(h.calls.some(([path])=>path==='/recovery/default/apply'),false);
console.log(JSON.stringify({generatedASGIEnvelopeConsumedByShippedController:true,planDigest:envelope.result.preview.plan_digest,noApply:true}));
