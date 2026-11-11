import assert from 'node:assert/strict';
import {readFile,mkdtemp,mkdir,writeFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import path from 'node:path';
import {_electron} from 'playwright-core';
// No browser fallback: external Chromium/Edge and personal app profiles are prohibited.
export async function launchOwnedElectron(){
 const pinsPath=process.env.FINIX_ELECTRON_PINS;
 assert.ok(pinsPath,'OWNED_ELECTRON_HARNESS_UNAVAILABLE: supply independently verified entry/runtime pins');
 const pins=JSON.parse(await readFile(pinsPath,'utf8'));
 assert.equal(pins.syntheticDOMOnly,true);
 for(const [file,sha]of [[pins.runtime,pins.runtime_sha256],[pins.entry,pins.entry_sha256]])assert.equal(createHash('sha256').update(await readFile(file)).digest('hex'),sha,'Electron entry/runtime changed');
 assert.equal(path.basename(pins.runtime).toLowerCase(),'electron.exe');
 const scope=await mkdtemp(path.join(pins.evidenceRoot,'electron-'));
 for(const name of ['sdk-root','userData','sessionData','cache','crashes','tmp'])await mkdir(path.join(scope,name));
 const env={...process.env,FINIX_ELECTRON_SCOPE:scope,HERMES_HOME:path.join(scope,'sdk-root'),HERMES_DISABLE_LAZY_INSTALLS:'1',TMPDIR:path.join(scope,'tmp'),TMP:path.join(scope,'tmp'),TEMP:path.join(scope,'tmp')};delete env.ELECTRON_RUN_AS_NODE;
 const app=await _electron.launch({executablePath:pins.runtime,args:[pins.entry],env,timeout:15000});
 const page=await app.firstWindow();
 let facts;
 try{
 facts=await app.evaluate(({app})=>({entry:globalThis.FINIX_OWNED_ENTRY,argv:process.argv,pid:process.pid,home:process.env.HERMES_HOME,userData:app.getPath('userData'),sessionData:app.getPath('sessionData'),electron:process.versions.electron}));
 assert.equal(path.resolve(facts.entry),path.resolve(pins.entry));assert.ok(facts.argv.includes(pins.entry));assert.equal(facts.home,path.join(scope,'sdk-root'));assert.equal(facts.userData,path.join(scope,'userData'));assert.equal(facts.sessionData,path.join(scope,'sessionData'));
 }catch(error){await app.close();throw error;}
 const childProcess=app.process();
 return {newPage:async()=>page,close:async()=>{await app.close();assert.notEqual(childProcess.exitCode,null,'owned Electron did not exit');await writeFile(path.join(scope,'harness-facts.json'),JSON.stringify({...facts,runtime:pins.runtime,runtime_sha256:pins.runtime_sha256,entry_sha256:pins.entry_sha256,launcherExited:true,syntheticDOMOnly:true},null,2));}};
}
