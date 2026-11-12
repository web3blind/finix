import * as sdk from '@hermes/plugin-sdk';
import {jsx as reactJsx, jsxs as reactJsxs} from 'react/jsx-runtime';
import {english} from './messages.mjs';

const escape = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const phrases = new RegExp(Object.keys(english).sort((a,b)=>b.length-a.length).map(escape).join('|'),'g');
export const translate = (text, language) => typeof text==='string' && language==='en' ? text.replace(phrases, s=>english[s]) : text;
const snapshot=()=>JSON.stringify([sdk.host.state.profile.get(),sdk.host.activeConnectionId?.(),sdk.host.state.connectionId?.get(),sdk.gatewayActivationEpoch?.()]);
let current=null;

// useI18n is public in SDK 0.23.0, but exposes no profile/route binding.
// Attest the selected route with one closed read-only locale field. Never use
// the previous shell locale while its profile configuration is initializing.
export function createLocale(ctx){
 const state=sdk.atom({scope:null,language:null});let revision=0,disposed=false,owner=null,pending=null;
 const listeners=new Set();
 const refresh=()=>{
  if(disposed)return null;
  const binding=snapshot();if(owner===binding&&pending)return pending;
  owner=binding;const r=++revision;state.set({scope:binding,language:null});
  pending=(async()=>{let language='en';try{const out=await ctx.rest('/ui-language',{timeoutMs:10000});if(out?.ui_language==='ru')language='ru';}catch{/* unavailable or older backend: default to English */}
   if(!disposed&&r===revision&&binding===snapshot()){state.set({scope:binding,language});listeners.forEach(fn=>fn(language));}
  })();return pending;
 };
 const subscriptions=[sdk.host.state.profile,sdk.host.state.connectionId,sdk.host.state.gateway].map(s=>s?.subscribe?.(()=>{if(owner!==snapshot())void refresh();})).filter(Boolean);
 const controller={state,refresh,view(){if(owner!==snapshot())void refresh();return state.get().scope===snapshot()?state.get().language:null;},subscribe:fn=>{listeners.add(fn);return()=>listeners.delete(fn);}};
 current=controller;void refresh();
 ctx.onDispose(()=>{disposed=true;revision++;subscriptions.forEach(fn=>fn());listeners.clear();if(current===controller)current=null;});
 return controller;
}

export const scopedLanguage=()=>current?.view()??null;
export function useLocalized(pinned=null){
 const host=sdk.useI18n?.();
 if(current)sdk.useValue(current.state);
 const bound=pinned==='en'||pinned==='ru'?pinned:current?.view()??(typeof sdk.useI18n==='function'?null:'en');
 // Prefer public SDK locale once the selected route agrees. The attestation
 // also covers a recovered profile whose integrations remain disabled.
 const language=bound===null?null:host&&!host.isLoadingConfig&&host.locale===bound?host.locale:bound;
 const text=v=>Array.isArray(v)?v.map(text):translate(v,language);
 const wrap=fn=>(type,props,key)=>{
  if(type==='section'&&language===null)return fn('section',{'aria-busy':true,children:reactJsx('p',{role:'status','aria-live':'polite',children:'Finix…'})},key);
  const p={...props};
  if(Object.hasOwn(p,'children'))p.children=text(p.children);
  for(const k of ['aria-label','title','placeholder'])if(typeof p[k]==='string')p[k]=translate(p[k],language);
  if(type==='section')p.lang=language??'en';
  return fn(type,p,key);
 };
 return {jsx:wrap(reactJsx),jsxs:wrap(reactJsxs),language:language??'en'};
}
