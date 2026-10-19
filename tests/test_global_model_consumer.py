"""Real consumer and sync facade, inert transports only; never a model call."""
import json

import httpx
import pytest

from prosperos_hoard import global_model as global_llm
from prosperos_hoard.hoard_link.config import LinkConfig
from prosperos_hoard.editing.hoard_link.config import LinkConfig as EditingLinkConfig
from prosperos_hoard.mv_planner import _stream_text, WriterStalled


def selection(revision='rev-a'):
    return dict(endpoint_id='global-a',model='exact-model',connection_revision=revision,
        vision=True,url='http://faustus.invalid/api/global-model/v1/chat/completions',
        headers={'Authorization':'Bearer synthetic-family'})


@pytest.mark.parametrize('config_type', [LinkConfig, EditingLinkConfig])
def test_real_sync_facade_keeps_overrides_and_one_job_pin(monkeypatch, config_type):
    gets, posts = [], []
    def descriptor(config):
        gets.append(config)
        return selection('rev-a' if len(gets)==1 else 'rev-b')
    def transport(request):
        body=json.loads(request.content)
        posts.append((str(request.url),dict(request.headers),body))
        assert body['expected_connection_revision']==('rev-a' if len(posts)<=2 else 'rev-b')
        assert body['expected_model']==body['model']=='exact-model'
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'{}'},'finish_reason':'stop'}]})
    monkeypatch.setattr(global_llm,'descriptor',descriptor)
    client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
    link=global_llm.GlobalLink(config_type(),client=client)
    try:
        with global_llm.scope():
            one=link.sync.chat([{'role':'user','content':'first'}],response_format={'type':'json_object'})
            two=link.sync.chat([{'role':'user','content':'repair'}],response_format={'type':'json_object'})
        assert one.text==two.text=='{}'
        assert len(gets)==1 and len(posts)==2
        assert isinstance(link.sync._inner,global_llm.GlobalLink)
        assert all(p[1]['authorization']=='Bearer synthetic-family' for p in posts)
        assert all(p[2]['response_format']=={'type':'json_object'} for p in posts)
        with global_llm.scope():
            link.sync.chat([{'role':'user','content':'next job'}])
        assert len(gets)==2 and len(posts)==3
    finally:
        thread=link.sync._thread
        link.sync.close()
        if thread: thread.join()


def test_stale_pin_is_not_retried_or_replaced(monkeypatch):
    calls=[]
    monkeypatch.setattr(global_llm,'descriptor',lambda config:selection())
    def transport(request):
        calls.append(request)
        return httpx.Response(409,json={'detail':{'error':'global_model_changed'}})
    link=global_llm.GlobalLink(LinkConfig(),client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    try:
        with global_llm.scope(),pytest.raises(Exception):
            link.sync.chat([{'role':'user','content':'synthetic'}])
        assert len(calls)==1
        assert str(calls[0].url).startswith('http://faustus.invalid/api/global-model/')
    finally:
        thread=link.sync._thread
        link.sync.close()
        if thread: thread.join()


@pytest.mark.parametrize('ending', [['data: [DONE]'], [], ['data: {"choices":[{"finish_reason":"length","delta":{}}]}','data: [DONE]']])
def test_global_planner_rejects_partial_or_truncated_stream(ending):
    lines=['data: {"choices":[{"delta":{"content":"partial"}}]}']+ending
    with pytest.raises(WriterStalled):
        list(_stream_text(lines,'openai',strict=True))


def test_global_planner_accepts_completed_content():
    lines=['data: {"choices":[{"delta":{"content":"{}"}}]}',
           'data: {"choices":[{"finish_reason":"stop","delta":{}}]}','data: [DONE]']
    assert ''.join(_stream_text(lines,'openai',strict=True))=='{}'
