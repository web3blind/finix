import * as sdk from '@hermes/plugin-sdk';
import {useLocalized,scopedLanguage} from './i18n.mjs';
import {jsx,jsxs} from 'react/jsx-runtime';
import {useEffect} from 'react';

export {createRecovery} from './recovery-operation.mjs';
const warning=c=>c===false?'Копия неполная. Некоторые данные отсутствуют.':c!==true?'Полнота копии не проверена. Некоторые данные могут отсутствовать.':'';
const primary=c=>c===false?'Восстановить неполную копию':c!==true?'Восстановить копию с непроверенной полнотой':'Восстановить';
export function Recovery({recovery}){
 sdk.useGatewayActivationEpoch?.();sdk.useValue(recovery.state);sdk.useValue(sdk.host.state.profile);if(sdk.host.state.connectionId)sdk.useValue(sdk.host.state.connectionId);
 const {value:s,actions:a}=recovery.view(),selected=s.items.find(x=>x.id===s.candidate);
 const {jsx,jsxs}=useLocalized(s.rollback?s.displayLanguage:null);
 sdk.useQuery?.({queryKey:['finix','recovery','catalog',...s.scope],queryFn:()=>a.load(),enabled:!s.ready&&!s.busy&&!s.activated,retry:(count,error)=>count<3&&error?.message==='RECOVERY_PROFILE_UNAVAILABLE',retryDelay:1000,staleTime:Infinity,refetchOnWindowFocus:false});
 sdk.useQuery?.({queryKey:['finix','recovery','job',...s.scope,s.job],queryFn:()=>a.poll(),enabled:s.busy&&s.polling,retry:false,refetchInterval:s.busy&&s.polling?1000:false,refetchOnWindowFocus:false});
 useEffect(()=>()=>recovery.actions.clear(),[recovery]);
 const disabled=(s.busy&&!s.awaitingKey)||s.activated||s.rollback;
 return jsxs('section',{'aria-label':'Восстановление Finix',style:{padding:'16px',overflow:'auto',height:'100%'},children:[
  jsx('h1',{children:'Восстановление Finix'}),
  jsxs('label',{children:['Резервная копия',jsx('select',{'aria-label':'Резервная копия',value:s.candidate??'',disabled:disabled||!s.ready,onChange:e=>a.candidate(e.currentTarget.value),children:s.items.length?s.items.map(x=>jsx('option',{value:x.id,children:`${x.label} — ${x.created_at??x.date??'дата неизвестна'} — ${x.source??'источник неизвестен'} — ${x.complete===true?'полная':x.complete===false?'неполная':'полнота не проверена'}`},x.id)):jsx('option',{value:'',children:s.ready?'Нет доступных копий':'Загрузка…'})})]}),
  jsx('p',{role:'note',children:'При восстановлении через общий backend другой процесс вашей учётной записи ОС не должен одновременно заменять создаваемый профиль.'}),
  jsxs('label',{children:['Файл ключа восстановления',jsx('input',{'aria-label':'Файл ключа восстановления',type:'file',accept:'.txt,text/plain',disabled:disabled||(!s.ready&&!s.awaitingKey),onChange:e=>{const f=e.currentTarget.files?.[0];e.currentTarget.value='';return a.key(f);}})]}),
  selected&&warning(selected.complete)&&jsx('p',{role:'note',children:warning(selected.complete)}),
  jsx(sdk.Button,{type:'button',disabled:disabled||(!s.awaitingKey&&(!s.ready||!s.candidate))||!s.key,onClick:()=>{if(s.awaitingKey&&s.key)return a.start({});if(!disabled&&s.ready&&s.key&&selected)return a.start({candidate:selected.id,complete:selected.complete});},children:primary(selected?selected.complete:true)}),
  jsx('p',{children:'Будет создан и открыт восстановленный профиль. Текущий профиль сохранится.'}),
  jsx('p',{role:'status','aria-live':'polite',children:s.message}),
  s.busy&&jsx('progress',{'aria-label':'Ход восстановления'}),
  s.busy&&jsx(sdk.Button,{type:'button',disabled:!s.cancellable,onClick:a.cancel,children:s.cancellable?'Отменить восстановление':'Текущая стадия не допускает отмену'}),
  s.rollback&&(!s.busy||s.selectionFailed)&&jsx(sdk.Button,{type:'button',onClick:a.back,children:'Назад к предыдущему профилю'})
 ]});
}
