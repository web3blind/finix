import * as sdk from '@hermes/plugin-sdk';
const message='Автоматический импорт и открытие профиля недоступны: требуется публичный контракт Hermes для атомарной публикации. Ключ не запрашивается. Копии можно просмотреть ниже; используйте отдельное офлайн-восстановление в пустую папку. Сохранённые операции не возобновляются.';
export function createRecovery(ctx){
 const state=sdk.atom({ready:false,items:[],busy:false,message,unavailable:true});
 let disposed=false;
 const scope=()=>[sdk.host.state.profile.get(),sdk.host.activeConnectionId?.(),sdk.host.state.connectionId?.get()];
 let owner=scope();
 const same=()=>owner.every((v,i)=>v===scope()[i]);
 const actions={load:async()=>{
  if(disposed||state.get().loaded)return null;
  const active=scope();
  if(active[1]!=='local'||active[2]!=='local')return null;
  try{
   const catalog=await ctx.rest('/recovery/catalog');
   if(disposed||!same())return null;
   const items=Array.isArray(catalog?.items)?catalog.items.filter(x=>typeof x.id==='string'&&typeof x.label==='string').map(x=>({id:x.id,label:x.label,complete:x.complete===true})):[];
   state.set({...state.get(),loaded:true,items,message});
  }catch{if(!disposed&&same())state.set({...state.get(),loaded:true,message:message+' Каталог недоступен в этом подключении.'});}
  return null;
 },clear:()=>{}};
 ctx.onDispose(()=>{disposed=true;});
 return {state,actions,view(){if(!same()){owner=scope();state.set({ready:false,items:[],busy:false,message,unavailable:true});}return {value:{...state.get(),scope:owner},actions};}};
}
