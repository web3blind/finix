import * as sdk from '@hermes/plugin-sdk';
import {jsx,jsxs} from 'react/jsx-runtime';
import {useEffect} from 'react';

// Separate from ordinary Restore: no profile selection, import, start or path input.
const snapshot=()=>({profile:sdk.host.state.profile.get(),source:sdk.host.activeConnectionId?.(),connection:sdk.host.state.connectionId?.get(),transport:sdk.host.state.gateway?.get(),epoch:sdk.gatewayActivationEpoch?.()});
const equal=(a,b)=>a&&b&&Object.keys(a).every(k=>a[k]===b[k]);
const local=s=>s.source==='local'&&s.connection==='local'&&Number.isSafeInteger(s.epoch);
const hex=(v,n=64)=>typeof v==='string'&&new RegExp(`^[a-f0-9]{${n}}$`).test(v);
const keys=(o,names)=>o&&typeof o==='object'&&!Array.isArray(o)&&Object.keys(o).sort().join('|')===[...names].sort().join('|');
const INITIAL=()=>({verified:false,ready:false,busy:false,polling:false,checkExisting:false,key:false,items:[],candidate:null,preview:null,cancellable:false,message:'Checking physical-default maintenance availability…'});
const HINT='finix.default-maintenance.read-hint.v1';
const states=new Set(['prepared','registered','acknowledged','draining','stopped','publishing','published_offline','ready','rolling_back','rolled_back_ready','rollback_not_ready','refused','manual_required','reconciliation_required']);
function validPreview(v){
 if(!keys(v,['id','state','confirmation','downtime','rollback','active_shared_autoruns_supported','preservation','preview','apply_enabled'])||!hex(v.id,32)||v.state!=='prepared'||!hex(v.confirmation)||v.downtime!==true||v.rollback!==true||v.active_shared_autoruns_supported!==false||typeof v.apply_enabled!=='boolean')throw Error('preview');
 const p=v.preview,r=v.preservation;
 if(!keys(p,['contract','plan_digest','root_binding','scope','replacement_policy','replacement_roots','affected_files','removed_files','kept_shared_members','kept_inventory','inert_only'])||p.contract!=='default-maintenance-preview-v1'||!hex(p.plan_digest)||!hex(p.root_binding)||p.scope!=='replace-real-default'||p.replacement_policy!=='profile-context-v1'||JSON.stringify(p.replacement_roots)!=='["memories","skills"]'||p.kept_inventory!=='opaque-subtrees-not-cloned'||p.inert_only!==true||!['affected_files','removed_files','kept_shared_members'].every(k=>Number.isSafeInteger(p[k])&&p[k]>=0))throw Error('preview');
 if(!keys(r,['policy','storage_format','consent_notice','retention','storage_cost','keepSharedResources'])||r.storage_format!=='opaque-container-v2'||r.policy!=='runtime-attempt-preserve-v1'||r.retention!=='explicit-cleanup-only'||r.storage_cost!=='complete old copies + complete current copies and native originals'||r.keepSharedResources!==true||typeof r.consent_notice!=='string'||r.consent_notice.length>1024)throw Error('policy');
 return v;
}
export function createDefaultMaintenance(ctx){
 const state=sdk.atom(INITIAL()),instance=crypto.randomUUID();let keyGeneration=0,owner=snapshot(),revision=0,disposed=false,file=null,buffer=null,loading=false,polling=false,job=null,maintenanceId=null,preview=null,previewSent=false,keySent=false,applySent=false,cancelPending=false,previewReadOnly=false,profileId=null,gate=false,rootToken=null,registryIdentity=null,anchor=null,reconnect=false,revoked=false;
 const publish=patch=>{if(!disposed)state.set({...state.get(),...patch});};
 function wipe(){keyGeneration++;file=null;if(buffer)new Uint8Array(buffer).fill(0);buffer=null;publish({key:false});}
 function reset(){
  const next=snapshot(),expected=anchor&&applySent&&local(next)&&next.profile===owner.profile&&next.source===owner.source&&next.connection===owner.connection;
  revision++;wipe();owner=next;loading=false;polling=false;gate=false;preview=null;
  // A dispatched operation is never forgotten or converted into fresh consent.
  if(state.get().busy||applySent||previewSent||keySent){
   revoked=revoked||!expected;reconnect=!!expected&&!revoked;
   publish({...INITIAL(),busy:true,polling:reconnect,cancellable:false,message:'Hermes is restarting; reopen Finix to check the result. The existing operation is locked; Apply will not be retried.'});return;
  }
  job=null;maintenanceId=null;previewSent=false;keySent=false;applySent=false;profileId=null;rootToken=null;registryIdentity=null;anchor=null;reconnect=false;revoked=false;cancelPending=false;publish(INITIAL());
 }
 function fence(r=revision){if(disposed||revoked||r!==revision||!equal(owner,snapshot())||!local(owner)){wipe();throw Error('scope');}}
 async function registry(r){fence(r);const rows=await sdk.host.connections();fence(r);if(!Array.isArray(rows)||rows.filter(x=>x.id==='local').length!==1||!rows.some(x=>x.id==='local'&&x.kind==='local'))throw Error('locality');const identity=JSON.stringify(rows.find(x=>x.id==='local'));if(registryIdentity!==null&&identity!==registryIdentity){revoked=true;throw Error('locality');}registryIdentity=identity;}
 async function authenticate(r){
  await registry(r);const cap=await rest(r,'/recovery/default/capabilities');fence(r);
  if(cap?.code_version!=='default-recovery-v1'||cap.supported!==true||!hex(cap.serverRootToken))throw Error('owner');
  if(cap.serverRootToken!==anchor.rootToken){revoked=true;throw Error('root');}
  if(cap.local_only!==true||cap.profile_id!==anchor.profileId){revoked=true;throw Error('source');}
  reconnect=false;
 }
 function rest(r,path,options){fence(r);return ctx.rest(path,options);}
 function hint(phase){ctx.storage?.set(HINT,{version:1,profile:owner.profile,connection:'local',profileId,phase,job,maintenanceId,rootToken});}
 function uncertain(r){if(disposed||r!==revision)return;wipe();publish({busy:true,polling:!!(job||maintenanceId),cancellable:false,message:'Acknowledgement uncertain. Only the exact existing job will be read; another maintenance operation is blocked.'});}
 function decrypt(v){if(!v||!hex(v.id,32)||v.id!==job||v.profile_id!==profileId||!['awaiting_key','running','succeeded','failed','cancelled'].includes(v.state))throw Error('job');return v;}
 async function consume(r,v){
  decrypt(v);publish({busy:true,polling:true,cancellable:!previewSent&&!cancelPending&&['awaiting_key','running'].includes(v.state),message:v.state==='running'?'Downloading, decrypting and verifying in private staging…':'Preparing isolated decrypted copy…'});
  if(v.state==='awaiting_key')return;
  if(v.state==='running')return;
  wipe();publish({cancellable:false});
  if(v.state!=='succeeded'){hint('terminal');publish({busy:false,polling:false,ready:false,message:v.state==='cancelled'?'Maintenance preparation cancelled; the physical default was not replaced.':'Decryption failed. The physical default was not replaced.'});return;}
  if(v.complete!==true)throw Error('incomplete');
  if(previewSent)return; // An uncertain preview ACK must never mint another plan.
  previewSent=true;hint('preview-pending');
  const p=await rest(r,'/recovery/default/prepare',{method:'POST',body:{profile_id:profileId,recovery_job:job,consent_prepare:true,preview_contract:'default-preview-job-v1'}});fence(r);consumePreview(r,p);
 }
 function consumePreview(r,v){
  fence(r);
  if(!keys(v,['contract','id','state','error','result','cancellable'])||v.contract!=='default-preview-job-v1'||v.id!==job||!['running','succeeded','failed','cancelled','interrupted'].includes(v.state)||typeof v.cancellable!=='boolean')throw Error('preview-job');
  if(v.state==='running'){
   if(v.result!==null||v.error!==null)throw Error('preview-job');
   publish({busy:true,polling:true,cancellable:!previewReadOnly&&!cancelPending&&v.cancellable,message:'Sealing the exact physical-default preview in private staging…'});return;
  }
  if(v.state!=='succeeded'){
   if(v.result!==null||!['PREVIEW_DEADLINE','PREVIEW_CANCELLED','PREVIEW_INTERRUPTED','DEFAULT_RECOVERY_FAILED','STALE_PROFILE','BUSY','INCOMPLETE_ACK_REQUIRED','OWNER_NOT_VERIFIED','DEFAULT_AUTORUN_NOT_SUPPORTED','SIBLING_AUTORUN_NOT_SUPPORTED','SDK_VERSION_NOT_SUPPORTED','LOCAL_OWNER_REQUIRED','INVALID_CAPABILITY','INVALID_CANDIDATE','INVALID_IDENTITY','DECRYPT_FAILED','JOB_NOT_PREPARED','NOT_FOUND','ROOT_EPOCH_CHANGED','WRONG_DEFAULT_ROOT','REOPEN_LIVE_OWNER_UNVERIFIED','DEFAULT_RECOVERY_REVIEW_REQUIRED','CONSENT_REQUIRED'].includes(v.error))throw Error('preview-job');
   hint('preview-terminal');publish({busy:false,polling:false,ready:false,cancellable:false,message:`Preview preparation stopped (${v.error}). The physical default was not replaced; retained state requires reconciliation before another preparation.`});return;
  }
  if(v.error!==null||v.cancellable!==false)throw Error('preview-job');
  const p=validPreview(v.result);
  if(p.preview.root_binding!==rootToken)throw Error('root');
  if(previewReadOnly){maintenanceId=p.id;applySent=true;hint('preview-read-only');publish({busy:false,polling:false,cancellable:false,message:'Sealed preview retained. Reopened status grants no new Apply consent.'});return;}
  // Own a detached immutable projection: callers cannot mutate a rendered plan.
  preview=JSON.parse(JSON.stringify(p));maintenanceId=preview.id;anchor=Object.freeze({maintenanceId,planDigest:preview.preview.plan_digest,rootToken,profileId,registryIdentity,profile:owner.profile,source:owner.source,connection:owner.connection});hint('preview');
  publish({preview:JSON.parse(JSON.stringify(preview)),busy:false,polling:false,cancellable:false,ready:false,message:gate&&preview.apply_enabled?'Review this exact sealed physical-default maintenance plan.':'Prepared preview only. Physical-default Apply is unavailable pending independent review.'});
 }
 async function readPreview(r){const v=await rest(r,`/recovery/default/preview/${job}`);fence(r);consumePreview(r,v);}
 async function readMaintenance(r){
  const v=await rest(r,`/recovery/default/status/${maintenanceId}`);fence(r);
  if(!keys(v,['id','state','error'])||v.id!==maintenanceId||!states.has(v.state)||![null,'DEFAULT_RECOVERY_FAILED'].includes(v.error))throw Error('status');
  const running=['registered','acknowledged','draining','stopped','publishing','published_offline','rolling_back'].includes(v.state);
  const message=v.state==='ready'?'Backend observed physical default READY. Stock default history-route verification is still required; usable recovery is not yet confirmed.':v.state==='rolled_back_ready'?'Maintenance failed; the old default returned READY. Preserved failed-start data remains retained.':v.state==='prepared'?'Sealed preview retained. Reopened status grants no new Apply consent.':running?'Physical-default maintenance in progress. This phase cannot be cancelled.':'Maintenance requires manual review. Preserved data is retained; duplicate Apply is blocked.';
  publish({busy:running||!['ready','rolled_back_ready','prepared','refused'].includes(v.state),polling:running,cancellable:false,verified:false,message});
  if(v.state==='ready'){
   // A Finix status dictionary alone is not stock Desktop usability evidence.
   // Never enable archived plugins merely to prove readiness.
   try{
    if(typeof sdk.host.profileRoutes!=='function'||typeof sdk.host.requestProfile!=='function')return;
    await registry(r);const routes=await sdk.host.profileRoutes();fence(r);
    const matches=Array.isArray(routes)?routes.filter(x=>x.profile==='default'||x.targetProfile==='default'):[];
    if(matches.length!==1)throw Error('route');const route=matches[0];
    if(route.mode!=='local'||route.connectionId!=='local'||route.profile!=='default'||route.targetProfile!=='default')throw Error('route');
    const description=await sdk.host.requestProfile(route,'profiles.describe',{name:'default'});fence(r);if(description?.name!=='default')throw Error('description');
    const history=await sdk.host.requestProfile(route,'session.list',{scope:'stored',limit:200});fence(r);
    if(history?.scope!=='stored'||!Array.isArray(history.sessions)||history.sessions.length>200||!history.sessions.every(x=>typeof x?.id==='string'&&x.id.length>0&&x.id.length<=512))throw Error('history');
    publish({verified:true,busy:false,polling:false,message:'Physical default READY observed and its exact stock stored-history route is readable. Full historical metadata equality and AI-turn readiness are not claimed.'});
   }catch{fence(r);publish({verified:false,message:'Physical default READY was observed, but exact stock default/history-route verification failed. Usable recovery is not yet confirmed; duplicate Apply remains blocked.'});}
  }
 }
 const actions={
  load:async()=>{if(disposed||revoked||applySent||loading||state.get().ready||state.get().busy||preview)return null;const r=revision;loading=true;try{
   await registry(r);const cap=await rest(r,'/recovery/default/capabilities');fence(r);
   if(cap?.code_version!=='default-recovery-v1'||cap.preview_contract!=='default-preview-job-v1'||cap.supported!==true||cap.local_only!==true||!hex(cap.profile_id)||cap.active_shared_autoruns_supported!==false||typeof cap.apply_enabled!=='boolean'||!hex(cap.serverRootToken))throw Error('unsupported');gate=cap.apply_enabled;rootToken=cap.serverRootToken;profileId=cap.profile_id;
   const saved=ctx.storage?.get(HINT,null);
   // Global storage is an untrusted read-only hint; never hydrate consent or key authority.
   if(saved?.version===1&&saved.profile===owner.profile&&saved.connection==='local'&&saved.profileId===profileId&&saved.phase!=='terminal'){
    if(hex(saved.maintenanceId,32)){maintenanceId=saved.maintenanceId;applySent=true;publish({busy:true,polling:true});await readMaintenance(r);return null;}
    if(hex(saved.job,32)){job=saved.job;previewSent=true;previewReadOnly=true;if(saved.rootToken===rootToken&&['preview-pending','preview-terminal'].includes(saved.phase)){publish({busy:true,polling:true,cancellable:false,message:'Reattached for exact preview status only; no new preparation or Apply consent.'});await readPreview(r);}else publish({busy:true,polling:false,message:'Previous preparation requires exact-operation reconciliation. No creation or key request will be repeated.'});return null;}
    publish({busy:true,polling:false,message:'Previous preparation acknowledgement is unknown. Reconcile the original operation before starting another.'});return null;
   }
   const catalog=await rest(r,'/recovery/catalog');fence(r);if(catalog?.profile_id!==profileId||!Array.isArray(catalog.items))throw Error('catalog');
   // Closed projection: catalog labels and private/arbitrary fields are never rendered.
   const items=catalog.items.filter(x=>hex(x.id)&&x.complete===true).map(x=>({id:x.id,complete:true}));publish({ready:true,items,candidate:items[0]?.id??null,message:items.length?'Select a complete copy and a local recovery-key file to prepare a sealed maintenance preview. This does not stop or replace the default.':'No complete backup is available for physical-default maintenance.'});
  }catch{if(r===revision)publish({ready:false,message:'Physical-default maintenance is unavailable for this source, device, owner or active scope. Ordinary Restore remains separate.'});}finally{if(r===revision)loading=false;}return null;},
  candidate:value=>{fence();if(state.get().busy||preview||!state.get().items.some(x=>x.id===value))return;wipe();publish({candidate:value});},
  key:f=>{fence();if(!state.get().ready||state.get().busy||preview)return;wipe();if(!f||!Number.isSafeInteger(f.size)||f.size<1||f.size>16384)return;file=f;publish({key:true});},
  prepare:async input=>{const s=state.get();if(disposed||s.busy||!s.ready||!file||!s.candidate||preview||input?.candidate!==s.candidate)return;const r=revision,selected=file,generation=keyGeneration;let data;publish({busy:true,polling:false,cancellable:false,message:'Preparing isolated maintenance decryption…'});hint('prepare-pending');try{
   await registry(r);const v=await rest(r,'/recovery/default/prepare-auto',{method:'POST',body:{profile_id:profileId,candidate_id:s.candidate,consent_restore:true}});fence(r);if(!hex(v?.id,32)||v.profile_id!==profileId||v.state!=='awaiting_key')throw Error('prepare');job=v.id;hint('decrypt');
   data=await selected.arrayBuffer();fence(r);if(generation!==keyGeneration)throw Error('cleared');buffer=data;if(buffer.byteLength!==selected.size||buffer.byteLength<1||buffer.byteLength>16384)throw Error('size');await registry(r);fence(r);if(generation!==keyGeneration)throw Error('cleared');keySent=true;
   const ack=await rest(r,`/recovery/jobs/${job}/key`,{method:'POST',upload:{filename:'recovery.txt',contentType:'text/plain',bytes:buffer},timeoutMs:30000});fence(r);await consume(r,ack);
  }catch{uncertain(r);}finally{if(data)new Uint8Array(data).fill(0);if(r===revision)wipe();}},
  checkExisting:async()=>{if(disposed||revoked||polling||!state.get().checkExisting||!job||!keySent||previewSent||maintenanceId||applySent||cancelPending)return null;const r=revision;polling=true;publish({checkExisting:false});try{
   await registry(r);const cap=await rest(r,'/recovery/default/capabilities');fence(r);
   if(cap?.code_version!=='default-recovery-v1'||cap.supported!==true||cap.serverRootToken!==rootToken){revoked=true;throw Error('root');}
   if(cap.local_only!==true||cap.profile_id!==profileId){revoked=true;throw Error('source');}
   publish({polling:true,message:'Checking the exact existing preparation; no key or decryption request will be repeated.'});
  }catch{if(r===revision)publish({polling:false,checkExisting:!revoked&&equal(owner,snapshot())&&local(owner),message:'Existing preparation could not be authenticated for this exact source and physical root. No mutation was sent.'});return null;}finally{if(r===revision)polling=false;}
  return actions.poll();},
  poll:async()=>{if(disposed||revoked||polling||!state.get().busy||!state.get().polling)return null;const r=revision;let readingJob=false,readingPreview=false;polling=true;try{await registry(r);if(reconnect)await authenticate(r);if(maintenanceId)await readMaintenance(r);else if(previewSent&&job){readingPreview=true;await readPreview(r);}else if(job){readingJob=true;const v=await rest(r,`/recovery/jobs/${job}`);readingJob=false;fence(r);await consume(r,v);}}catch{uncertain(r);if(r===revision){reconnect=false;const checkExisting=readingJob&&!revoked&&equal(owner,snapshot())&&local(owner)&&keySent&&!previewSent&&!maintenanceId&&!applySent&&!cancelPending;publish({polling:previewSent&&!readingPreview&&!maintenanceId&&!applySent,checkExisting,message:checkExisting?'The existing preparation could not be read. Check it again on this same device; decryption and key submission will not be repeated.':'Hermes is restarting or the owner could not be verified; reopen Finix to check the result. The existing operation remains locked and Apply will not be retried.'});}}finally{if(r===revision)polling=false;}return null;},
  apply:async rendered=>{const s=state.get();if(disposed||s.busy||applySent||!gate||!preview?.apply_enabled||!rendered||JSON.stringify(rendered)!==JSON.stringify(preview)||JSON.stringify(s.preview)!==JSON.stringify(preview))return;const r=revision;applySent=true;wipe();publish({busy:true,polling:true,cancellable:false,message:'Confirming this exact physical-default maintenance plan…'});hint('apply-pending');try{await registry(r);await rest(r,'/recovery/default/apply',{method:'POST',body:{profile_id:profileId,id:preview.id,confirmation:preview.confirmation,consent_apply:true}});fence(r);await readMaintenance(r);}catch{uncertain(r);}},
  cancel:async()=>{if(disposed||!state.get().busy||!state.get().cancellable||!job||maintenanceId||cancelPending||previewReadOnly)return;const r=revision;cancelPending=true;wipe();publish({cancellable:false});try{await registry(r);const v=await rest(r,previewSent?`/recovery/default/preview/${job}/cancel`:`/recovery/jobs/${job}/cancel`,previewSent?{method:'POST',body:{profile_id:profileId}}:{method:'POST'});fence(r);if(previewSent)consumePreview(r,v);else await consume(r,v);}catch{uncertain(r);}},
  clear:()=>wipe()
 };
 const subs=[sdk.host.state.profile,sdk.host.state.connectionId,sdk.host.state.gateway].map(s=>s?.subscribe?.(()=>{if(!equal(owner,snapshot()))reset();})).filter(Boolean);
 ctx.onDispose(()=>{disposed=true;revision++;wipe();subs.forEach(stop=>stop());});
 return {state,actions,view(){if(!equal(owner,snapshot()))reset();const r=revision;return {value:{...state.get(),applyEnabled:gate&&preview?.apply_enabled===true&&!applySent,confirmationConsumed:applySent,scope:[instance,r,owner.profile,owner.source,owner.connection,owner.epoch]},actions:Object.fromEntries(Object.entries(actions).map(([k,fn])=>[k,(...args)=>{if(disposed||r!==revision||!equal(owner,snapshot())){wipe();return null;}try{return fn(...args);}catch{return null;}}]))};}};
}
export function DefaultMaintenance({maintenance}){
 sdk.useGatewayActivationEpoch?.();sdk.useValue(maintenance.state);sdk.useValue(sdk.host.state.profile);if(sdk.host.state.connectionId)sdk.useValue(sdk.host.state.connectionId);
 const {value:s,actions:a}=maintenance.view();
 sdk.useQuery?.({queryKey:['finix','default-maintenance','load',...s.scope],queryFn:()=>a.load(),enabled:!s.ready&&!s.busy&&!s.preview,retry:false,staleTime:Infinity,refetchOnWindowFocus:false});
 sdk.useQuery?.({queryKey:['finix','default-maintenance','job',...s.scope],queryFn:()=>a.poll(),enabled:s.busy&&s.polling,retry:false,refetchInterval:s.busy&&s.polling?1000:false,refetchOnWindowFocus:false});
 useEffect(()=>()=>maintenance.actions.clear(),[maintenance]);
 const p=s.preview?.preview;
 return jsxs('section',{'aria-label':'Finix physical-default maintenance',style:{padding:16,overflow:'auto',height:'100%'},children:[
  jsx('h1',{children:'Physical-default maintenance'}),jsx('p',{children:'Separate maintenance of the actual SDK default, not ordinary Restore to a new profile. Available only from its verified local Finix source. No destination, profile-name or mode setup is required.'}),
  !p&&jsxs('label',{children:['Complete backup',jsx('select',{'aria-label':'Maintenance backup',value:s.candidate??'',disabled:!s.ready||s.busy,onChange:e=>a.candidate(e.currentTarget.value),children:s.items.map((x,i)=>jsx('option',{value:x.id,children:`Complete backup ${i+1} · ${x.id.slice(0,12)}`},x.id))})]}),
  !p&&jsxs('label',{children:['Local recovery-key file',jsx('input',{'aria-label':'Maintenance recovery key',type:'file',accept:'.txt,text/plain',disabled:!s.ready||s.busy,onChange:e=>{const f=e.currentTarget.files?.[0];e.currentTarget.value='';return a.key(f);}})]}),
  !p&&jsx(sdk.Button,{type:'button',disabled:!s.ready||s.busy||!s.key||!s.candidate,onClick:()=>a.prepare({candidate:s.candidate}),children:'Prepare physical-default maintenance preview'}),
  p&&jsxs('section',{'aria-label':'Sealed maintenance preview',children:[
   jsx('h2',{children:'Review this exact maintenance plan'}),jsx('p',{children:`Plan ${p.plan_digest} · physical-root binding ${p.root_binding}`}),
   jsx('p',{children:`Replace profile-owned memories and skills, including removal of old-only items: ${p.affected_files} existing members; ${p.removed_files} removals. Other planned incoming profile data is published under the sealed plan.`}),
   jsx('p',{children:`Keep shared host resources, installations, control plane and sibling profiles. ${p.kept_shared_members} retained top-level members are inventoried; opaque subtrees are not recursively verified or cloned.`}),
   jsx('p',{children:'Downtime: applying authorizes a scoped stop of the verified idle default owner and a restart. Active work must be idle before proceeding. Active shared autoruns, supervisors and sibling-serving multiplexers are unsupported; only the admitted inert scope is supported.'}),
   jsx('p',{children:'Rollback preserves OLD data. A failed new startup may write data before readiness; allowed late-write variants are preserved as inert custody before OLD is restored. Unknown variants or uncertain stop/ownership require manual preservation, not forced rollback.'}),
   jsx('p',{children:'Storage cost: complete OLD copies, complete current copies and native originals. Custody is included in future backups and retained indefinitely until a separately authorized cleanup. There is no automatic retention expiry.'}),
   jsx('p',{children:'Recovered cron, messaging, hooks, MCP, external secrets and executable plugins remain parked. Only the exact pinned current trusted Finix control plane is exempt; archived plugin code is not enabled for status.'}),
   jsx(sdk.Button,{type:'button',disabled:s.busy||!s.applyEnabled,onClick:()=>a.apply(s.preview),children:s.confirmationConsumed?'Maintenance confirmation already submitted':s.applyEnabled?'Confirm maintenance: replace this physical default':'Physical-default Apply unavailable — independent review required'})]}),
  s.checkExisting&&jsx(sdk.Button,{type:'button',onClick:a.checkExisting,children:'Check existing preparation'}),
  jsx('p',{role:'status','aria-live':'polite',children:s.message}),s.busy&&jsx('progress',{'aria-label':'Default maintenance progress'}),s.busy&&jsx(sdk.Button,{type:'button',disabled:!s.cancellable,onClick:a.cancel,children:s.cancellable?'Cancel preparation':'This maintenance phase cannot be cancelled'})
 ]});
}
