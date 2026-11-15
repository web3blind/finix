import * as sdk from '@hermes/plugin-sdk';
import {useLocalized} from './i18n.mjs';
export {createRecovery} from './recovery-operation.mjs';
export function Recovery({recovery}){
 sdk.useValue(recovery.state);sdk.useValue(sdk.host.state.profile);
 if(sdk.host.state.connectionId)sdk.useValue(sdk.host.state.connectionId);
 const {value:s,actions:a}=recovery.view();const {jsx,jsxs}=useLocalized();
 sdk.useQuery?.({queryKey:['finix','recovery','catalog',...s.scope],queryFn:()=>a.load(),enabled:!s.loaded,retry:false,staleTime:Infinity,refetchOnWindowFocus:false});
 return jsxs('section',{'aria-label':'Восстановление Finix',children:[jsx('h1',{children:'Восстановление Finix'}),jsx('p',{role:'status',children:s.message}),jsx('ul',{children:s.items.map(x=>jsx('li',{children:x.label+(x.complete?' — полная копия':' — полнота не подтверждена')},x.id))})]});
}
