import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';

test('catalog preview stays available without keys, posts, selection or hint adoption',async()=>{
 const source=await readFile(new URL('../desktop/src/recovery-operation.mjs',import.meta.url),'utf8');
 const atom=v=>({get:()=>v,set:x=>{v=x;}});
 const calls=[];const retained={id:'old-pending',phase:'importing'};
 const sdk={atom,host:{state:{profile:atom('source'),connectionId:atom('local')},activeConnectionId:()=> 'local'}};
 const context=vm.createContext({});const entry=new vm.SourceTextModule(source,{context});
 await entry.link(name=>{assert.equal(name,'@hermes/plugin-sdk');return new vm.SyntheticModule(['atom','host'],function(){this.setExport('atom',sdk.atom);this.setExport('host',sdk.host);},{context});});
 await entry.evaluate();
 const c=entry.namespace.createRecovery({onDispose:()=>{},storage:{get:()=>retained,set:()=>assert.fail('rewrote pending state'),remove:()=>assert.fail('deleted pending state')},rest:async(path,options)=>{calls.push(path);assert.equal(options,undefined);assert.equal(path,'/recovery/catalog');return {items:[{id:'fixture',label:'Cloud copy',complete:true}]};}});
 await c.actions.load();
 assert.deepEqual(calls,['/recovery/catalog']);assert.equal(c.state.get().items[0].label,'Cloud copy');
 assert.equal(c.state.get().unavailable,true);assert.equal(c.actions.key,undefined);assert.equal(c.actions.start,undefined);
 assert.equal(retained.phase,'importing');
});
