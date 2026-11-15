import * as sdk from '@hermes/plugin-sdk';
import {useLocalized} from './i18n.mjs';
const message='Замена физического default и автоматический откат недоступны: Hermes не предоставляет публичную удерживаемую блокировку всех писателей и точную транзакционную замену. Ключ не запрашивается. Прежние журналы и данные сохранены; возобновление запрещено. Офлайн-восстановление в пустую папку остаётся доступным.';
export function createDefaultMaintenance(ctx){
 const state=sdk.atom({ready:false,busy:false,message,unavailable:true});
 const actions={load:async()=>null,clear:()=>{}};
 return {state,actions,view:()=>({value:state.get(),actions})};
}
export function DefaultMaintenance({maintenance}){
 sdk.useValue(maintenance.state);const {jsx,jsxs}=useLocalized();
 return jsxs('section',{'aria-label':'Default recovery unavailable',children:[jsx('h2',{children:'Восстановление default'}),jsx('p',{role:'status',children:maintenance.state.get().message})]});
}
