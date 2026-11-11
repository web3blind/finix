import * as sdk from '@hermes/plugin-sdk';
import {useLocalized} from './i18n.mjs';
import { jsx, jsxs } from 'react/jsx-runtime';
import { useEffect } from 'react';

const scope = () => ({ profile: sdk.host.state.profile.get(), source: sdk.host.activeConnectionId?.(), connection: sdk.host.state.connectionId?.get(), epoch: sdk.gatewayActivationEpoch?.() });
const equal = (a,b) => a.profile===b.profile && a.source===b.source && a.connection===b.connection && a.epoch===b.epoch;
const remedies = {
  SETUP_CHECK_FAILED: 'Проверка не завершена. Расписание не включено. Обновите состояние и проверьте подключение; ключ менять не нужно.',
  SETUP_SOURCE_CHANGED: 'Данные профиля изменились во время копирования. Дождитесь завершения записи приложениями и повторите проверку. Расписание не включено.',
  SETUP_READBACK_FAILED: 'Проверка чтением из хранилища не пройдена. Копия не подтверждена. Проверьте доступ и свободное место в облаке; расписание не включено.',
  SETUP_DEPENDENCY_UNAVAILABLE: 'Недоступен доверенный инструмент age или rclone. Проверьте локальные инструменты Finix и обновите состояние; повторный вход и смена ключа не требуются.',
  SETUP_CLOUD_CONFIG_UNAVAILABLE: 'Недоступны настройки облачного подключения или права доступа к ним. Проверьте подключение; не меняйте ключ восстановления.',
  SETUP_CLOUD_FAILED: 'Облачная операция не завершена. Проверьте сеть и доступ к подключённому облаку, затем обновите состояние.',
  SETUP_ACCESS_DENIED: 'Отказано в доступе к исходным данным или хранилищу. Проверьте права доступа; не отключайте защитные проверки.',
  SETUP_SQLITE_TEMP_REQUIRED: 'Для снимка SQLite нужна защищённая локальная временная папка. Проверьте настройки временного хранилища Finix.',
  SETUP_SQLITE_TEMP_SPACE: 'Недостаточно свободного места для согласованного снимка SQLite. Освободите место во временном хранилище.',
  SETUP_SQLITE_FAILED: 'Не удалось создать согласованный снимок SQLite. Завершите запись в базу и повторите проверку; при повторении проверьте целостность базы локально.',
  SETUP_STATUS_FAILED: 'Не удалось прочитать состояние настройки. Обновите состояние после восстановления связи с backend. Новую копию пока не запускайте.',
  SETUP_CHECK_RUNNING: 'Первая копия ещё выполняется на сервере. Обновите состояние позже, не запускайте повторно.',
  SETUP_SCHEDULE_FAILED: 'Расписание не подтверждено. Обновите состояние, проверьте первую копию и повторите сохранение периода.',
  PROFILE_CHANGED: 'Профиль изменился. Обновите состояние настройки в выбранном профиле.',
  CLOUD_SELECTION_REQUIRED: 'Сначала подключите и примените облачное хранилище.',
  PERIOD_FORMAT_24h_OR_7d: 'Укажите положительный целый период, например 24h или 7d.'
};
const diagnostic = code => Object.hasOwn(remedies, code) ? `${code}: ${remedies[code]}` : null;
const phases = { checksum:'Проверка локального шифротекста', verify:'Проверка чтением', reconcile:'Продолжение сохранённой копии', commit:'Фиксация', preparing:'Подготовка', discovering:'Поиск файлов', scanning:'Поиск файлов', capturing:'Копирование', copying:'Копирование', encrypting:'Шифрование', uploading:'Загрузка', upload:'Загрузка', cloud_uploading:'Загрузка', readback:'Проверка чтением', verifying:'Проверка чтением', cloud_reading:'Проверка чтением', complete:'Копия проверена' };
const phase = j => j?.phase==='complete' && !complete(j) ? 'Копирование' : phases[j?.phase]??'Копирование';
const active = s => s.job?.state==='running' || s.uncertain;
const complete = j => j?.state==='succeeded' && j.verified===true && j.complete===true && j.omitted_count===0;
const ready = s => s.capable && s.data?.verified===true && !active(s) && complete(s.job) && s.data?.capture?.complete!==false;
const initial = () => ({data:null,job:null,loaded:false,capable:false,busy:false,uncertain:false,connection:false,period:'24h',message:'Загрузка состояния резервной копии…',announcement:'Загрузка состояния резервной копии…'});
const number = v => typeof v==='number' && Number.isFinite(v) && v>=0;
function progress(j){
 const p=j?.progress;if(complete(j))return 100;
 // Percent is phase-local; unknown totals stay indeterminate.
 if(!p || !(number(p.total_bytes)&&p.total_bytes>0 || number(p.total_files)&&p.total_files>0))return null;
 return number(p.percent)?Math.min(99,Math.floor(p.percent)):null;
}
function summary(j){
 if(!j)return 'Сначала подключите облако, затем создайте первую копию.';
 if(j.state==='incomplete'&&j.complete==null)return 'UNKNOWN: Полнота исторической копии неизвестна. Число пропущенных файлов неизвестно. Расписание не включено.';
 if(j.state==='incomplete'||j.state==='succeeded'&&j.complete===false)return `INCOMPLETE: Копия неполная. Пропущено файлов: ${Number.isSafeInteger(j.omitted_count)&&j.omitted_count>=0?j.omitted_count:'?'}. Расписание не включено.`;
 if(complete(j))return 'Первая зашифрованная копия создана, загружена и проверена чтением из хранилища. Теперь выберите период.';
 if(j.state==='failed'||j.state==='interrupted')return diagnostic(j.error_code)??(j.state==='interrupted'?'Копирование прервано. Можно повторить создание копии.':'Копирование не завершено. Можно повторить создание копии.');
 if(j.state==='succeeded')return 'Полнота и проверка копии не подтверждены. Расписание не включено.';
 return `${phase(j)}: ${progress(j)===null?'объём пока неизвестен':`${progress(j)}% этапа`}. Копия ещё не подтверждена.`;
}
let instance=0;
export function createSetup(ctx){
 const state=sdk.atom(initial()),id=++instance;
 let owner=scope(),revision=0,disposed=false,reading=false,lastAnnouncement='',lastAnnouncedAt=0;
 const set=patch=>{if(!disposed)state.set({...state.get(),...patch});};
 const reset=()=>{revision++;owner=scope();reading=false;lastAnnouncement='';lastAnnouncedAt=0;set(initial());};
 function fence(r){if(disposed||r!==revision||!equal(owner,scope())||!Number.isSafeInteger(owner.epoch))throw Error('scope');}
 const valid=r=>{try{fence(r);return true;}catch{return false;}};
 const rest=(r,path,options)=>{fence(r);return ctx.rest(path,options);};
 function accept(r,out,binding){
  fence(r);
  if(out?.code_version!=='backup-jobs-v1'){set({capable:false,loaded:true,connection:false,message:'BACKUP_BACKEND_UPDATE_REQUIRED: Обновите backend Finix и перезапустите шлюз. Запуск копии отключён; старый долгий запрос не используется.',announcement:'BACKUP_BACKEND_UPDATE_REQUIRED: Обновите backend Finix и перезапустите шлюз.'});return false;}
  if(!out.profile_id || typeof out.profile_id!=='string'||(binding&&out.profile_id!==binding))throw Error('contract');
  const j=out.job;
  if(j!==null&&(!j||typeof j.id!=='string'||!j.id||!['running','succeeded','incomplete','failed','interrupted'].includes(j.state)||!j.progress||typeof j.progress!=='object'))throw Error('contract');
  const prior=state.get();if(prior.job?.state==='running'&&(!j||j.id!==prior.job.id))throw Error('contract');
  const message=prior.uncertain&&!j?'Подтверждение запуска ещё не получено. Проверяем сервер автоматически; повторный запуск заблокирован.':summary(j),p=progress(j),key=`${j?.id}:${j?.state}:${phases[j?.phase]??'copy'}:${p===null?'unknown':Math.floor(p/10)}`;
  const announce=prior.connection || key!==lastAnnouncement&&(j?.state!=='running'||lastAnnouncement===''||Date.now()-lastAnnouncedAt>=10000);
  if(announce){lastAnnouncement=key;lastAnnouncedAt=Date.now();}
  set({job:j,capable:true,loaded:true,connection:false,uncertain:prior.uncertain&&!j,message,...(announce?{announcement:message}:{})});return true;
 }
 async function readSetup(r){const data=await rest(r,'/setup',{timeoutMs:10000});fence(r);if(!data||!data.profile_id||typeof data.profile_id!=='string'||typeof data.verified!=='boolean'||state.get().data&&data.profile_id!==state.get().data.profile_id)throw Error('contract');return data;}
 function disconnected(r){if(valid(r))set({connection:true,message:'Связь с сервером временно недоступна. Состояние обновится автоматически. Последний известный этап сохранён; новая копия заблокирована.',announcement:'Связь с сервером временно недоступна. Ожидание автоматического обновления.'});}
 const refresh=async()=>{
  const r=revision;if(reading||disposed||state.get().busy||!valid(r))return null;reading=true;
  try{
   let data=state.get().data;
   if(!data||!active(state.get())){data=await readSetup(r);set({data});}
   let out;
   try{out=await rest(r,'/backup/jobs/current',{timeoutMs:10000});}
   catch(error){fence(r);if([404,405].includes(error?.status)||[404,405].includes(error?.statusCode)||/^(?:HTTP\s+(404|405)\b|(404|405): )/.test(error?.message??'')){accept(r,{},data.profile_id);return null;}throw error;}
   fence(r);if(!accept(r,out,data.profile_id))return null;
   if(complete(out.job)){const after=await readSetup(r);if(after.profile_id!==data.profile_id)throw Error('binding');set({data:after});}
  }catch{disconnected(r);}finally{if(r===revision)reading=false;}return null;
 };
 const action=async name=>{
  const r=revision,s=state.get();if(!valid(r)||s.busy||reading||s.connection||active(s)||!s.data||!s.capable)return;
  if(name==='check'&&(s.data.check_running||s.data.storage==='local'||!['check','schedule','complete'].includes(s.data.stage)))return;
  if(name!=='check'&&name!=='schedule')return;
  if(name==='schedule'&&!ready(s))return;
  if(name==='schedule'&&!/^[1-9][0-9]*[hd]$/.test(s.period)){set({message:diagnostic('PERIOD_FORMAT_24h_OR_7d')});return;}
  set({busy:true});
  try{
   if(name==='check'){
    set({uncertain:true});
    try{const out=await rest(r,'/backup/jobs',{method:'POST',timeoutMs:15000,body:{profile_id:s.data.profile_id,request_id:crypto.randomUUID()}});fence(r);if(!accept(r,out,s.data.profile_id)||!out.job)throw Error('ack');}
    catch{fence(r);const out=await rest(r,'/backup/jobs/current',{timeoutMs:10000});fence(r);accept(r,out,s.data.profile_id);}
    if(complete(state.get().job)){const after=await readSetup(r);if(after.profile_id!==s.data.profile_id)throw Error('binding');set({data:after});}
   }else{
    const out=await rest(r,'/setup/schedule',{method:'POST',timeoutMs:15000,body:{profile_id:s.data.profile_id,period:s.period}});fence(r);if(out?.profile_id!==s.data.profile_id)throw Error('binding');
    const after=await readSetup(r);const hours=Number(s.period.slice(0,-1))*(s.period.endsWith('d')?24:1);
    if(after.profile_id!==s.data.profile_id||!after.verified||after.capture?.complete===false||after.interval_hours!==hours)throw Error('readback');
    set({data:after,message:`Расписание включено: каждые ${after.interval_hours} ч. Настройки прочитаны обратно.`,announcement:'Расписание включено. Настройки прочитаны обратно.'});
   }
  }catch{disconnected(r);}finally{if(valid(r))set({busy:false});}
 };
 const subscriptions=[sdk.host.state.profile,sdk.host.state.connectionId,sdk.host.state.gateway].map(x=>x?.subscribe?.(()=>{if(!equal(owner,scope()))reset();})).filter(Boolean);
 ctx.onDispose(()=>{disposed=true;revision++;subscriptions.forEach(stop=>stop());state.set(initial());});
 return {state,set,refresh,action,view(){if(!disposed&&!equal(owner,scope()))reset();const r=revision;return {s:state.get(),scope:[id,r,owner.profile,owner.source,owner.connection,owner.epoch],refresh:()=>valid(r)?refresh():null,action:n=>valid(r)?action(n):null,set:p=>{if(valid(r))set(p);}};}};
}

export function SetupCheck({setup,enabled}){
 const {jsx,jsxs,language}=useLocalized();
 sdk.useGatewayActivationEpoch?.();sdk.useValue(setup.state);sdk.useValue(sdk.host.state.profile);if(sdk.host.state.connectionId)sdk.useValue(sdk.host.state.connectionId);
 const v=setup.view(),s=v.s,j=s.job,p=progress(j),running=active(s),locked=s.busy||running||s.connection;
 useEffect(()=>{void v.refresh();},[setup]);
 const watching=!s.loaded||running||s.connection||s.capable&&(s.data?.check_running||s.data?.storage==='local'||!['check','schedule','complete'].includes(s.data?.stage));
 sdk.useQuery?.({queryKey:['finix','backup',...v.scope],queryFn:v.refresh,enabled:!!watching,retry:false,refetchInterval:watching?2000:false,refetchOnWindowFocus:false});
 const button=(label,fn,disabled=false)=>jsx(sdk.Button,{type:'button',onClick:fn,disabled:s.busy||disabled,children:label});
 const counters=j?.progress,counts=counters?`${number(counters.completed_bytes)?counters.completed_bytes:'?'} / ${number(counters.total_bytes)?counters.total_bytes:'?'} байт; ${number(counters.completed_files)?counters.completed_files:'?'} / ${number(counters.total_files)?counters.total_files:'?'} файлов`:'';
 const elapsed=j&&Number.isFinite(Date.parse(j.started_at))?Math.max(0,Math.floor(((running?Date.now():Date.parse(j.updated_at))-Date.parse(j.started_at))/1000)):null;
 const updated=j&&Number.isFinite(Date.parse(j.updated_at))?new Date(j.updated_at).toLocaleTimeString(language==='en'?'en-US':'ru-RU'):'неизвестно';
 return jsxs('section',{'aria-label':'Проверка и расписание','aria-busy':s.busy,children:[
  jsx('h2',{children:'3. Проверка, первая копия и период'}),
  jsx('p',{children:'Копия создаётся на сервере. Закрытие страницы не отменяет работу; при открытии состояние загрузится автоматически. Проценты относятся к текущему этапу; вся копия подтверждается только после проверки чтением.'}),
  button('Обновить состояние настройки',v.refresh),
  button('Проверить и создать первую копию',()=>{if(enabled&&!locked)return v.action('check');},!enabled||locked||!s.capable||!s.data||s.data.check_running||s.data.storage==='local'||!['check','schedule','complete'].includes(s.data.stage)),
  j&&jsx('p',{children:`${phase(j)}: ${p===null?'объём пока неизвестен':`${p}%${complete(j)?' всей копии':' этапа'}`} — ${counts}`}),
  j&&jsx('progress',{'aria-label':'Ход резервного копирования: текущий этап',max:100,...(p===null?{}:{value:p,'aria-valuenow':p})}),
  j&&jsx('p',{children:`Прошло: ${elapsed??'?'} с. Последнее обновление: ${updated}.`}),
  ready(s)&&!locked&&jsxs('div',{children:[
   jsxs('label',{children:['Период резервного копирования',jsx(sdk.Input??'input',{value:s.period,'aria-describedby':'finix-period-help',onChange:e=>v.set({period:e.currentTarget.value})})]}),
   jsx('p',{id:'finix-period-help',children:'Положительное целое число и h (часы) или d (дни), например 24h или 7d. Расписание изменится только после сохранения.'}),
   button('Сохранить и включить',()=>v.action('schedule'),!/^[1-9][0-9]*[hd]$/.test(s.period))
  ]}),
  jsx('p',{children:s.message}),
  jsx('p',{role:'status','aria-live':'polite','aria-atomic':true,style:{position:'absolute',width:1,height:1,overflow:'hidden',clipPath:'inset(50%)'},children:s.announcement})
 ]});
}
