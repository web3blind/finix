import test from 'node:test';
import assert from 'node:assert/strict';
import {harness} from './recovery.test.mjs';
test('shipped separate physical-default contribution leaves ordinary three-control Restore unchanged',async()=>{
 const h=await harness();const page=h.contributions.find(x=>x.id==='default-maintenance-page');assert.ok(page,'missing shipped maintenance contribution');assert.equal(page.data.path,'/finix/default-maintenance');assert.equal(h.nodes().filter(x=>x.type==='select').length,1);assert.equal(h.nodes().filter(x=>x.type==='input').length,1);assert.equal(h.nodes().filter(x=>x.type==='button').length,1);
 assert.ok(h.contributions.some(x=>x.id==='default-maintenance-open'));
});
