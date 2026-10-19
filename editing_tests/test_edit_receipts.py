from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import copy
import threading
import time

import pytest

from prosperos_hoard.editing import agent_tools, projects
from prosperos_hoard.editing.db import Database
from prosperos_hoard.editing.errors import Conflict, LumiereError


def test_concurrent_retries_keep_one_clip_one_revision_and_original_ids(services):
    p=projects.create(services,'Durable edit')
    args={'project':p['id'],'ops':[{'op':'add_text','text':'Opening','start':0,'length':3000}],
          'request_id':'opening-v1','base_rev':p['rev']}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:agent_tools.call_tool(services,'timeline_edit',args),range(4)))
    assert sum(not r['replayed'] for r in results)==1
    assert len({json.dumps(r['results'],sort_keys=True) for r in results})==1
    doc=projects.doc(services,p['id'])
    assert sum(len(t.clips) for t in doc.tracks)==1
    assert projects.view(services,p['id'])['rev']==p['rev']+1
    # A separate connection reads the same durable receipt after reconnection.
    other=Database(services.db.path)
    try:
        assert other.one('SELECT COUNT(*) AS c FROM edit_receipts')['c']==1
    finally:other.close()
    with pytest.raises(Conflict):
        agent_tools.call_tool(services,'timeline_edit',{**args,'ops':[{'op':'add_text','text':'Different','start':0,'length':3000}]})
    assert projects.doc(services,p['id']).dump()==doc.dump()


def test_replay_after_undo_reports_current_revision_without_redoing(services):
    p=projects.create(services,'Undo')
    args={'project':p['id'],'ops':[{'op':'add_text','text':'Title','start':0,'length':3000}], 'request_id':'one'}
    first=agent_tools.call_tool(services,'timeline_edit',args)
    projects.undo(services,p['id'])
    before=projects.view(services,p['id'])
    replay=agent_tools.call_tool(services,'timeline_edit',args)
    assert replay['replayed'] and replay['rev']==first['rev'] and replay['current_rev']==before['rev']
    assert projects.view(services,p['id'])==before
    assert not any(t.clips for t in projects.doc(services,p['id']).tracks)


def test_receipt_failure_rolls_back_the_edit_and_history(services):
    p=projects.create(services,'Atomic edit');before=projects.view(services,p['id'])
    services.db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON edit_receipts BEGIN SELECT RAISE(ABORT,'injected failure'); END;")
    with pytest.raises(sqlite3.IntegrityError):
        projects.edit(services,p['id'],[{'op':'add_text','text':'No partial effect','start':0,'length':3000}],request_id='fail')
    assert projects.view(services,p['id'])==before
    assert services.db.one('SELECT COUNT(*) AS c FROM edit_receipts')['c']==0
    assert services.db.one('SELECT COUNT(*) AS c FROM history WHERE project_id=?',(p['id'],))['c']==1


def test_rest_and_mcp_share_receipts_and_unkeyed_edits_still_apply(client):
    p=client.post('/api/projects',json={'name':'Shared edit'}).json()
    ops=[{'op':'add_text','text':'Same edit','start':0,'length':3000}]
    first=client.post(f"/api/projects/{p['id']}/edit",json={'ops':ops,'request_id':'shared'}).json()
    second=client.post('/api/agent/call',json={'name':'timeline_edit','arguments':{'project':p['id'],'ops':ops,'request_id':'shared'},'reason':'Share the receipt with the web edit'},
                       headers={'Authorization':f'Bearer {client.svc.token}'})
    assert second.status_code==200 and second.json()['replayed']
    assert second.json()['results']==first['results']
    projects.edit(client.svc,p['id'],[{'op':'marker_add','t':500,'label':'Again'}])
    projects.edit(client.svc,p['id'],[{'op':'marker_add','t':500,'label':'Again'}])
    assert len(projects.doc(client.svc,p['id']).markers)==2


@pytest.mark.parametrize('same_key',[True,False])
def test_separate_connections_serialize_reads_edits_and_receipts(services,monkeypatch,same_key):
    p=projects.create(services,'Two connections')
    peer=copy.copy(services);peer.db=Database(services.db.path)
    start=threading.Barrier(2)
    original=projects.apply_ops
    def slow_apply(*args,**kwargs):
        result=original(*args,**kwargs)
        time.sleep(.1)  # force a window for a stale concurrent read without a transaction
        return result
    monkeypatch.setattr(projects,'apply_ops',slow_apply)
    def edit_one(i):
        start.wait()
        text='Same' if same_key else f'Title {i}'
        return projects.edit(services if i==0 else peer,p['id'],
                             [{'op':'add_text','text':text,'start':0 if same_key else i*3000,'length':3000}],
                             request_id='same' if same_key else f'key-{i}')
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(edit_one,range(2)))
        clips=[c for t in projects.doc(services,p['id']).tracks for c in t.clips]
        assert len(clips)==(1 if same_key else 2)
        if same_key:
            assert sum(r['replayed'] for r in results)==1
            assert results[0]['results']==results[1]['results']
        else:
            assert {c.text for c in clips}=={'Title 0','Title 1'}
        assert services.db.one('SELECT COUNT(*) AS c FROM edit_receipts')['c']==len(clips)
    finally:peer.db.close()
