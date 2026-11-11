import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {transform} from 'esbuild';
import {controller} from './recovery-operation.test.mjs';
import {resolve} from 'node:path';
const sdk=process.env.FINIX_TEST_SDK_SOURCE;
if (!sdk) throw new Error('Set FINIX_TEST_SDK_SOURCE to a pinned Hermes SDK source checkout (see docs/TEST-SDK.md).');
const file=resolve(sdk,'apps/desktop/electron/plugin-profile-routes.ts');
const producer=await readFile(file,'utf8');
const compiled=await transform(producer,{loader:'ts',format:'esm',target:'es2022'});
const {buildRegistryProfileRoutes}=await import('data:text/javascript;base64,'+Buffer.from(compiled.code).toString('base64'));
test('actual installed SDK registry producer local descriptors consumed positively',async()=>{
 const routes=buildRegistryProfileRoutes({agents:[{connectionId:'local',profile:'restored'}],sources:[{id:'local',kind:'local'}],primaryConnectionId:'local'});
 assert.equal(routes[0].mode,'local');const h=await controller({routes});await h.start();h.set(h.row({state:'succeeded',phase:'backend_target_ready',cancellable:false,receipt:{name:'restored',served:'restored',backend_target_ready:true,archive_verified:true,content_verified:true,integrations_paused:true,history_pending:false,full_acceptance:false,contract:'sdk-new-content-copy-v1'}}));await h.recovery.actions.poll();assert.equal(h.recovery.state.get().activated,true);
});

