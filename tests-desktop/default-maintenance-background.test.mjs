import test from 'node:test';
import assert from 'node:assert/strict';
import {harness} from './default-maintenance.test.mjs';

test('hidden maintenance keeps its admitted job polling and never replays decrypt or key',async()=>{
 const queries=[];
 const h=await harness({query:q=>{queries.push(q);return {};}});
 await h.start();
 h.render();
 const job=queries.find(q=>q.enabled===true&&q.queryKey[2]==='job');
 assert.ok(job,'production busy-job query must be registered');
 // The real SDK re-exports TanStack useQuery; hidden windows otherwise pause intervals.
 assert.equal(job.refetchIntervalInBackground,true,'hidden admitted preparation must continue polling');
 h.set({id:'9'.repeat(32),profile_id:'a'.repeat(64),state:'succeeded',complete:true});
 await job.queryFn();
 assert.ok(h.maintenance.state.get().preview,'the observed completed decrypt must reach its sealed preview');
 assert.equal(h.calls.filter(([p])=>p==='/recovery/default/prepare-auto').length,1);
 assert.equal(h.calls.filter(([p])=>p.endsWith('/key')).length,1);
 assert.equal(h.calls.filter(([p])=>p==='/recovery/default/prepare').length,1);
 assert.equal(h.calls.some(([p])=>p==='/recovery/default/apply'),false);
 h.dispose();
 await job.queryFn();
 assert.equal(h.calls.filter(([p])=>p==='/recovery/default/prepare').length,1);
});
