const $ = id => document.getElementById(id);
const labels = {generation:'Generación',editing:'Montaje'};
$('management').hidden = !new URLSearchParams(location.search).has('manage');
let selected;
async function api(path, method='GET', body) {
  const response = await fetch('/api/studio/' + path, {method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  const result = await response.json(); if (!response.ok) throw Error(result.error || response.statusText); return result;
}
function node(tag, text, parent) {const element=document.createElement(tag);if(text)element.textContent=text;if(parent)parent.append(element);return element;}
function fail(error){$('error').textContent=error.message;}
async function modules(){
  const state=await api('modules');$('modules').replaceChildren();
  if(!new URLSearchParams(location.search).has('manage') && state.modules.some(m=>m.id==='generation'&&m.enabled)){
    location.replace('/generation/#/create');return;
  }
  for(const module of state.modules){const row=node('div',null,$('modules'));node('strong',labels[module.id],row);node('span',module.phase==='on'?'Activo':module.phase==='stopping'?'Cierre pendiente':'Apagado',row);
    const label=node('label',null,row),automatic=node('input',null,label);automatic.type='checkbox';automatic.checked=module.automatic;label.append(' Reanudar trabajos anteriores');
    const button=node('button',module.phase==='stopping'?'Reintentar apagado':module.enabled?'Apagar':'Activar',row);button.onclick=async()=>{button.disabled=true;$('error').textContent='';try{await api('modules/'+module.id,'PUT',{enabled:module.phase!=='stopping'&&!module.enabled,automatic:automatic.checked});await modules();}catch(e){fail(e);await modules();}};
    if(module.enabled){const link=node('a','Abrir '+labels[module.id],row);link.href=module.url;}
  }
}
async function projects(){const rows=await api('projects');$('projects').replaceChildren();for(const p of rows){const b=node('button',p.title,$('projects'));b.onclick=()=>openProject(p.id).catch(fail);}}
async function openProject(id){selected=id;const p=await api('projects/'+id);const area=$('project');area.replaceChildren();node('h3',p.title,area);
  const form=node('form',null,area);const label=node('label','Corte de generación ',form),input=node('select',null,label);input.required=true;input.name='timeline_id';
  const native=await api('objects');node('option','Selecciona un corte',input).value='';
  for(const source of native.generation)for(const timeline of source.timelines){const option=node('option',source.name+' · '+timeline.name,input);option.value=timeline.id;}
  if(!input.options.length||input.options.length===1)node('p','Activa Generación y crea un corte en su timeline; después vuelve a este proyecto.',form);
  node('p','Lleva el corte editable, sus escenas y archivos originales al montaje. Se conserva cada revisión y su resultado.',form);
  node('button','Abrir este corte en Montaje',form);form.onsubmit=async event=>{event.preventDefault();try{await api('projects/'+id+'/transfer','POST',{timeline_id:input.value});await openProject(id);}catch(e){fail(e);}};
  const list=node('ul',null,area);for(const object of p.objects){const li=node('li',null,list),link=node('a',labels[object.module]+' · '+object.kind+' · '+object.native_id,li);link.href=object.route;node('div','Revisión '+object.revision.slice(0,12),li);}
  if(!p.objects.length)node('p','Este proyecto todavía no tiene escenas o cortes vinculados.',area);
  for(const transfer of p.transfers){const line=node('p','Corte recibido · '+transfer.receipt.sha256.slice(0,12)+' · ',area);const download=node('a','Descargar XML editable',line);download.href='/api/studio/artifacts/'+id+'/'+transfer.receipt.sha256;const skipped=transfer.receipt.result.skipped||[];if(skipped.length)line.append(' · Medios pendientes: '+skipped.map(x=>x.name).join(', '));}
}
$('new-project').onsubmit=async event=>{event.preventDefault();try{const p=await api('projects','POST',{title:new FormData(event.target).get('title')});event.target.reset();await projects();await openProject(p.id);}catch(e){fail(e);}};
$('open-workspace').onclick=async()=>{const button=$('open-workspace');button.disabled=true;$('error').textContent='';$('opening').textContent='Abriendo el lienzo…';try{await api('modules/generation','PUT',{enabled:true,automatic:false});location.href='/generation/#/create';}catch(e){fail(e);button.disabled=false;$('opening').textContent='';}};
Promise.all([modules(),projects()]).catch(fail);
