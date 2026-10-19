"""Actual stdio MCP → private ASGI host → native synthetic cut/editor stores.

No user data, browser, model or GPU. Media probe/prepare are deliberately inert;
this proves native IDs, timeline interchange and ownership, not codec/render quality.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


async def main():
    import httpx
    import uvicorn
    from PIL import Image
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from prosperos_hoard import global_model
    from prosperos_hoard.hoard_link import family
    from prosperos_hoard.editing import services, ffmpeg, media
    from prosperos_hoard.editing.hoard_link import family as edit_family
    from prosperos_hoard.studio import create_app
    global_model.descriptor = lambda config: (_ for _ in ()).throw(AssertionError('No live model descriptor in this fixture'))
    family.emit = edit_family.emit = lambda *a,**k: None
    services.Services._build_link = lambda self: None
    services.Services.tools = lambda self: None
    ffmpeg.probe = lambda *a: {}
    ffmpeg.summarize = lambda *a: dict(kind='image',duration_ms=0,width=64,height=64,fps=24,has_video=True,has_audio=False)
    media.schedule_prepare = lambda *a: None
    with tempfile.TemporaryDirectory(prefix='prospero-mcp929-') as raw:
        data=Path(raw)
        inbox=data/'inbox';inbox.mkdir()
        image=inbox/'scene.png';Image.new('RGB',(64,64),'#e65db6').save(image)
        app=create_app(data,port=64239)
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=64239,log_level='error',loop='asyncio'))
        task=asyncio.create_task(server.serve())
        while not server.started:
            if task.done(): await task
            await asyncio.sleep(.01)
        env={**os.environ,'PROSPERO_URL':'http://127.0.0.1:64239','PROSPERO_DATA_DIR':str(data),
             'PROSPERO_TOKEN_FILE':str(data/'mcp-token'),'PROSPERO_TOKEN':'',
             'HOARD_AGENT_ID':'codex-fisiones-prospero-qa','HOARD_AGENT_SESSION':'synthetic929'}
        receipt={'method':'actual stdio MCP/private ASGI; native stores/handlers; inert media probe; no model/browser/GPU'}
        try:
            parameters=StdioServerParameters(command=sys.executable,args=[str(ROOT/'prosperos_hoard'/'mcp_server.py')],env=env,cwd=str(ROOT))
            async with stdio_client(parameters) as (read,write),ClientSession(read,write) as session:
                await session.initialize()
                listed=await session.list_tools()
                assert len(listed.tools)==168
                assert len({t.name for t in listed.tools})==168
                receipt['mcp_tools']=len(listed.tools)
                async def call(name,args=None):
                    result=await session.call_tool(name,args or {})
                    assert not result.isError,(name,result)
                    value=json.loads(next(c.text for c in result.content if c.type=='text'))
                    assert not (isinstance(value,dict) and value.get('error')),(name,value)
                    return value
                cold=await call('studio_modules')
                assert all(not m['enabled'] for m in cold['modules'])
                receipt['initial_off']=True
                for module in ('generation','editing'):
                    await call('studio_module_set',{'module':module,'enabled':True,'reason':'Exercise only this synthetic module fixture'})
                native=await call('studio_create_project',{'name':'Synthetic generated scene','reason':'Create the isolated source scene'})
                source_id=native['id']
                asset=await call('studio_import',{'project':source_id,'path':str(image),'kind':'image','reason':'Import the synthetic frame'})
                asset_id=asset.get('id') or asset.get('asset_id')
                store=app.studio.modules['generation'].app.state.store
                timeline=await asyncio.to_thread(store.create_timeline,source_id,'Synthetic finished generation',tracks=[
                    {'type':'visual','clips':[{'asset_id':asset_id,'kind':'image','start_s':0,'duration_s':2}]}])
                read_cut=await call('studio_timeline',{'project':source_id,'action':'get','timeline_id':timeline['id'],'reason':'Read the synthetic existing cut'})
                assert read_cut['clips_total']==1
                project=await call('studio_project_create',{'title':'Synthetic audiovisual project','reason':'Connect the synthetic source and edit'})
                transferred=await call('studio_project_transfer',{'project':project['id'],'timeline_id':timeline['id'],'reason':'Bring the synthetic editable cut into montage'})
                target=next(o for o in transferred['objects'] if o['module']=='editing')
                target_id=target['native_id']
                target_doc=await call('project_get',{'project':target_id})
                assert target_doc['tracks']
                legacy=await call('production_export_lumiere',{'production':'','timeline_id':timeline['id'],
                    'reason':'Verify the existing export command uses the incorporated editor'})
                assert legacy['ok'] and legacy['url'].startswith('/editing/#/p/')
                receipt['api_mcp_native_transfer']=True
                receipt['legacy_export_internal']=True
                for module in ('generation','editing'):
                    await call('studio_module_set',{'module':module,'enabled':False,'reason':'Join all resources of the isolated fixture'})
                saved=await call('audiovisual_projects',{'project':project['id']})
                assert any(o['native_id']==target_id for o in saved['objects'])
                sha=saved['transfers'][0]['receipt']['sha256']
                async with httpx.AsyncClient(base_url='http://127.0.0.1:64239') as client:
                    artifact=await client.get('/api/studio/artifacts/'+project['id']+'/'+sha,headers={'Range':'bytes=0-31'})
                    assert artifact.status_code==206 and artifact.content.startswith(b'<?xml')
                receipt['off_project_and_range_artifact']=True
                await call('studio_module_set',{'module':'editing','enabled':True,'reason':'Verify native edit ID survives reopening'})
                reopened=await call('project_get',{'project':target_id})
                assert reopened['project']==target_id
                await call('studio_module_set',{'module':'editing','enabled':False,'reason':'Close the final isolated module instance'})
                receipt['native_id_reopened']=True
                receipt['final_off']=all(m.phase=='off' and not m.requests for m in app.studio.modules.values())
        finally:
            server.should_exit=True
            await task
        print(json.dumps(receipt,ensure_ascii=False,indent=2))
        (ROOT/'tasks'/'studio-mcp-verification.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':
    asyncio.run(main())
