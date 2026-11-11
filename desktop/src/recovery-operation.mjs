import * as sdk from '@hermes/plugin-sdk';
import {scopedLanguage} from './i18n.mjs';
const snapshot=()=>({profile:sdk.host.state.profile.get(),source:sdk.host.activeConnectionId?.(),connection:sdk.host.state.connectionId?.get(),epoch:sdk.gatewayActivationEpoch?.()});
const equal=(a,b)=>a&&b&&Object.keys(a).every(k=>a[k]===b[k]);
const local=s=>s.source==='local'&&s.connection==='local'&&Number.isSafeInteger(s.epoch);
const nameOK=n=>typeof n==='string'&&/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(n)&&n!=='default';
const uuid=n=>typeof n==='string'&&/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(n);
const phases=new Set(['preparing','awaiting_key','data_queued','data_running','importing','target_bootstrap','history_importing','verifying_target','cleanup','backend_target_ready','cancelled']);
const initial=()=>({ready:false,items:[],candidate:null,key:false,busy:false,polling:false,cancellable:false,job:null,name:null,result:null,activated:false,message:'Загрузка доступных копий…'});
const HINT='recovery.selection.v1';
const failureMessages=new Map([['WRONG_KEY','Ключ не подходит.'],['CORRUPT_ARCHIVE','Архив повреждён.'],['TARGET_NOT_EMPTY','Папка назначения не пуста.'],['COMPLETENESS_CHANGED','Проверка выявила неполную или непроверенную копию. Выберите ключ повторно и подтвердите восстановление специальной кнопкой.']]);
const failureMessage=code=>failureMessages.get(code)??'Восстановление не завершено. Выберите ключ повторно.';
export function createRecovery(ctx){
 // Query data is a null completion marker; it cannot hydrate another controller.
 // The SDK re-evaluates modules on reload while retaining its shared Query cache.
 const state=sdk.atom(initial()),id=crypto.randomUUID();
 const dismissed=new Set();let keyGeneration=0;
 let owner=snapshot(),revision=0,disposed=false,file=null,bytes=null,prior=null,transition=null,loading=false,polling=false,owned=null,intent=null,keySent=false,keyReading=false,cancelIntent=false,selecting=false,foreground=false,sendAuthorized=false,last=null;
 const publish=patch=>{if(!disposed)state.set({...state.get(),...patch});};
 function wipe(){keyGeneration++;if(bytes)new Uint8Array(bytes).fill(0);bytes=null;file=null;publish({key:false});}
 function reset(){revision++;wipe();owner=snapshot();loading=false;polling=false;owned=null;intent=null;keySent=false;sendAuthorized=false;last=null;cancelIntent=false;prior=null;foreground=false;publish(initial());}
 function fence(r=revision){if(disposed||r!==revision||!equal(owner,snapshot())||!local(owner)){wipe();throw Error('scope');}}
 async function registry(r){fence(r);const rows=await sdk.host.connections();fence(r);if(!Array.isArray(rows)||rows.filter(x=>x.id==='local').length!==1||!rows.some(x=>x.id==='local'&&x.kind==='local'))throw Error('locality');}
 function rest(r,path,options){fence(r);if(foreground)throw Error('foreground');return ctx.rest(path,options);}
 function uncertain(r){if(r!==revision||disposed)return;wipe();publish({selectionFailed:foreground&&!selecting,busy:true,polling:!foreground,cancellable:!selecting&&state.get().cancellable,message:'Операция не подтверждена. Ожидание точного состояния; повторный запуск заблокирован.'});}
 function validate(v,{attach=false}={}){
  if(!v||!(/^[a-f0-9]{32}$/.test(v.id))||!uuid(v.request_id)||v.profile_id!==state.get().profileId||!['running','succeeded','cancelled','failed','interrupted','manual_required'].includes(v.state)||!phases.has(v.phase)||typeof v.cancellable!=='boolean'||typeof v.cancel_requested!=='boolean'||(v.name!==undefined&&!nameOK(v.name)))throw Error('operation');
  if(owned&&(v.id!==owned.id||v.request_id!==owned.request_id||(owned.name&&v.name!==owned.name)))throw Error('foreign');
  if(!attach&&intent&&v.request_id!==intent)throw Error('intent');
  if(v.name===owner.profile)throw Error('target');return v;
 }
 function observe(v){last=v;owned={id:v.id,request_id:v.request_id,name:v.name};publish({awaitingKey:v.phase==='awaiting_key'&&!sendAuthorized&&!keySent&&!cancelIntent&&!v.cancel_requested,job:v.id,name:v.name,busy:true,polling:true,cancellable:v.state==='running'&&['preparing','awaiting_key','data_queued','data_running'].includes(v.phase)&&v.cancellable&&!v.cancel_requested,result:{state:v.state,phase:v.phase},message:v.cancel_requested?'Отмена запрошена. Ожидание завершения обработчика.':v.phase==='awaiting_key'?'Выберите ключ для продолжения этой операции.':v.phase==='data_running'?'Скачивание и проверка копии…':'Восстановление данных…'});}
 async function describe(r,name){const routes=await sdk.host.profileRoutes();fence(r);const matches=Array.isArray(routes)?routes.filter(x=>x.profile===name||x.targetProfile===name):[];if(matches.length!==1)throw Error('route');const route=matches[0];if(route.mode!=='local'||route.connectionId!=='local'||route.profile!==name||route.targetProfile!==name)throw Error('route');const value=await sdk.host.requestProfile(route,'profiles.describe',{name});fence(r);if(value?.name!==name)throw Error('description');}
 async function activate(r,v){
  // Claim the selection continuation synchronously, before registry's first await.
  if(selecting||foreground)return;
  const receipt=v.receipt,name=v.name;
  if(v.phase!=='backend_target_ready'||!receipt||receipt.name!==name||receipt.served!==name||receipt.backend_target_ready!==true||receipt.archive_verified!==true||receipt.content_verified!==true||receipt.integrations_paused!==true||receipt.history_pending!==false||receipt.full_acceptance!==false||receipt.contract!=='sdk-new-content-copy-v1'||!nameOK(name))throw Error('receipt');
  if(!['ensureAgent','profileRoutes','requestProfile'].every(k=>typeof sdk.host[k]==='function'))throw Error('sdk');
  selecting=true;publish({polling:false,cancellable:false,message:'Подключение восстановленного профиля…'});
  try{await registry(r);fence(r);}catch(error){selecting=false;throw error;}
  prior={...owner};
  ctx.storage?.set(HINT,{version:1,source:owner.profile,connection:'local',profileId:state.get().profileId,id:v.id,phase:'selecting',target:name});
  transition={source:'local',profile:name};foreground=true;
  try{await sdk.host.ensureAgent('local',name);}finally{const now=snapshot();if(!disposed&&r===revision&&local(now)&&now.profile===name)owner=now;transition=null;selecting=false;}
  fence(r);if(owner.profile!==name)throw Error('selection');await describe(r,name);selecting=false;
  ctx.storage?.set(HINT,{version:1,source:prior.profile,connection:'local',profileId:state.get().profileId,id:v.id,phase:'connected',target:name});
  publish({activated:true,busy:false,polling:false,cancellable:false,message:'Восстановленный профиль открыт. История проверена; интеграции отключены. Полное тождество активных метаданных не подтверждено.'});
 }
 async function consume(r,v){
  if(selecting||foreground)return;
  validate(v,{attach:!intent});observe(v);
  if(v.state==='running'){
   if(cancelIntent&&!v.cancel_requested&&state.get().cancellable){const ack=await rest(r,`/recovery/operations/${v.id}/cancel`,{method:'POST'});fence(r);validate(ack);observe(ack);return;}
   if(v.phase==='awaiting_key'&&!v.cancel_requested&&!cancelIntent&&file&&sendAuthorized&&!keySent&&!keyReading){
    keyReading=true;const selectedFile=file,generation=keyGeneration;let buffer;try{buffer=await selectedFile.arrayBuffer();fence(r);if(cancelIntent||generation!==keyGeneration)return;if(buffer.byteLength!==selectedFile.size||buffer.byteLength<1||buffer.byteLength>16384)throw Error('size');bytes=buffer;await registry(r);fence(r);if(cancelIntent||generation!==keyGeneration)return;keySent=true;const request=crypto.randomUUID();const ack=await rest(r,`/recovery/operations/${v.id}/key?request_id=${request}`,{method:'POST',upload:{filename:'recovery.txt',contentType:'text/plain',bytes},timeoutMs:30000});fence(r);validate(ack);await consume(r,ack);}finally{if(buffer)new Uint8Array(buffer).fill(0);keyReading=false;wipe();}
   }return;
  }
  wipe();publish({cancellable:false});if(v.state==='succeeded'){await activate(r,v);return;}
  if(v.state==='interrupted'||v.state==='manual_required'){publish({busy:true,polling:false,message:'Восстановление прервано. Исходные данные и частично восстановленный профиль сохранены. Требуется ручная проверка; повторный импорт заблокирован.'});return;}
  if(v.state==='failed'&&v.error==='COMPLETENESS_CHANGED')publish({items:state.get().items.map(x=>x.id===state.get().candidate?{...x,complete:false}:x)});
  owned=null;intent=null;keySent=false;cancelIntent=false;publish({busy:false,polling:false,message:v.state==='cancelled'?'Восстановление отменено.':failureMessage(v.error)});
 }
 const actions={
  load:async()=>{if(disposed||loading||state.get().ready||state.get().busy||state.get().activated)return null;const r=revision;let capabilityPending=false;loading=true;try{
   await registry(r);const hint=ctx.storage?.get(HINT,null);
   // A global hint authorizes display verification only, never server work or selection.
   if(hint?.version===1&&hint.connection==='local'&&hint.target===owner.profile&&nameOK(hint.target)){await describe(r,owner.profile);publish({activated:true,message:'Подключен профиль '+owner.profile+'. Состояние архива в этом подключении не подтверждается.'});return null;}
   capabilityPending=true;const cap=await rest(r,'/recovery/capabilities');capabilityPending=false;fence(r);if(cap?.code_version!=='recovery-v1'||cap.operation_protocol!=='recovery-operations-v1'||cap.local_only!==true||typeof cap.profile_id!=='string'||!cap.profile_id)throw Error('contract');publish({profileId:cap.profile_id});
   const condition=cap.bootstrap_condition;
   if(condition!=null&&(condition.contract!=='cooperative-root-boundaries-v1'||condition.condition!=='no-concurrent-same-user-target-replacement'||condition.atomic_target_binding!==false))throw Error('contract');
   publish({bootstrapCondition:condition?.contract??null});
   const current=await rest(r,'/recovery/operations/current');fence(r);if(current){validate(current,{attach:true});if(!['failed','cancelled'].includes(current.state)&&!(current.state==='succeeded'&&dismissed.has(current.id))){await consume(r,current);return null;}}
   const catalog=await rest(r,'/recovery/catalog');fence(r);if(catalog?.profile_id!==cap.profile_id||!Array.isArray(catalog.items))throw Error('catalog');
   const items=catalog.items.filter(x=>typeof x.id==='string'&&/^[a-f0-9]{64}$/.test(x.id)&&typeof x.label==='string').map(x=>({...x,complete:x.complete===true?true:x.complete===false?false:null}));items.sort((a,b)=>(Date.parse(b.created_at??b.date??'')||0)-(Date.parse(a.created_at??a.date??'')||0));const selected=items.find(x=>x.complete===true)??items[0];publish({ready:true,items,candidate:selected?.id??null,message:items.length?'Выберите копию и файл ключа.':'Доступных копий нет. Восстановление пока невозможно.'});
  }catch(error){
   // The pinned IPC bridge strips numeric status. Admit only this exact closed
   // capability refusal, never arbitrary conflicts, ticket errors or mutations.
   if(capabilityPending&&!owned&&!intent&&typeof error?.message==='string'&&/^(?:Error invoking remote method 'hermes:api': Error: )?409: \{\s*"detail"\s*:\s*"PROFILE_UNAVAILABLE"\s*\}$/.test(error.message)){
    fence(r);publish({ready:false,message:'Шлюз не подтвердил доступность профиля (PROFILE_UNAVAILABLE). Восстановление заблокировано.'});throw Error('RECOVERY_PROFILE_UNAVAILABLE');
   }
   if(owned||intent)uncertain(r);else if(r===revision)publish({ready:false,message:'Требуется локальный шлюз с протоколом recovery-operations-v1. Восстановление недоступно.'});
  }finally{if(r===revision)loading=false;}return null;},
  candidate:value=>{fence();if(state.get().busy||!state.get().items.some(x=>x.id===value))return;wipe();publish({candidate:value,result:null});},
  key:f=>{fence();const s=state.get();if((s.busy&&!s.awaitingKey)||s.activated||prior||(!s.ready&&!s.awaitingKey))return;wipe();if(!f||!Number.isSafeInteger(f.size)||f.size<1||f.size>16384){publish({message:'Выберите файл ключа размером не более 16 КиБ.'});return;}file=f;publish({key:true});},
  start:async input=>{const s=state.get();if(s.awaitingKey&&file&&owned&&!keySent&&!keyReading&&!cancelIntent){const r=revision;sendAuthorized=true;publish({awaitingKey:false});try{await consume(r,last);}catch{uncertain(r);}return;}const selected=s.items.find(x=>x.id===s.candidate);if(disposed||s.busy||s.activated||prior||!s.ready||!selected||!file||input?.candidate!==selected.id||input.complete!==selected.complete)return;const r=revision;intent=crypto.randomUUID();sendAuthorized=true;keySent=false;cancelIntent=false;publish({busy:true,polling:true,cancellable:true,displayLanguage:scopedLanguage(),message:'Подготовка нового профиля…'});try{await registry(r);const value=await rest(r,'/recovery/operations',{method:'POST',body:{profile_id:s.profileId,candidate_id:selected.id,consent_restore:true,request_id:intent,acknowledge_incomplete:selected.complete!==true}});fence(r);await consume(r,value);}catch{uncertain(r);}},
  poll:async()=>{if(disposed||polling||selecting||!state.get().busy||!state.get().polling||foreground)return null;const r=revision;polling=true;try{await registry(r);fence(r);if(selecting||foreground)return null;const v=await rest(r,owned?`/recovery/operations/${owned.id}`:'/recovery/operations/current');fence(r);await consume(r,v);}catch{uncertain(r);}finally{if(r===revision)polling=false;}return null;},
  cancel:async()=>{const s=state.get();if(!s.busy||!s.cancellable||selecting||foreground)return;cancelIntent=true;wipe();publish({cancellable:false,message:'Отмена запрошена. Ожидание завершения обработчика.'});const r=revision;if(!owned)return;try{await registry(r);const ack=await rest(r,`/recovery/operations/${owned.id}/cancel`,{method:'POST'});fence(r);validate(ack);await consume(r,ack);}catch{uncertain(r);}},
  back:async()=>{if(!prior||(state.get().busy&&!state.get().selectionFailed))return;const r=revision,target=prior;publish({busy:true});try{fence(r);transition={source:target.source,profile:target.profile};try{await sdk.host.ensureAgent(target.source,target.profile);}finally{transition=null;}if(disposed||r!==revision)return;const now=snapshot();if(now.source!==target.source||now.connection!==target.connection||now.profile!==target.profile)throw Error('rollback');if(owned)dismissed.add(owned.id);reset();}catch{uncertain(r);}},
  clear:()=>{wipe();} // Unmount is not cancellation and cannot release backend ownership.
 };
 const expected=now=>transition&&now.source===transition.source&&now.connection===transition.source&&(now.profile===transition.profile||now.profile===owner.profile);
 const subscriptions=[sdk.host.state.profile,sdk.host.state.connectionId,sdk.host.state.gateway].map(store=>store?.subscribe?.(()=>{const now=snapshot();if(!equal(owner,now)&&!expected(now))reset();})).filter(Boolean);
 ctx.onDispose(()=>{disposed=true;revision++;wipe();subscriptions.forEach(stop=>stop());});
 return {state,actions,view(){const now=snapshot();if(!equal(owner,now)&&!expected(now))reset();const r=revision;return {value:{...state.get(),rollback:!!prior,scope:[id,r,owner.profile,owner.source,owner.connection,owner.epoch]},actions:Object.fromEntries(Object.entries(actions).map(([k,fn])=>[k,(...args)=>{if(disposed||r!==revision||!equal(owner,snapshot())){wipe();return null;}try{return fn(...args);}catch{return null;}}]))};}};
}
