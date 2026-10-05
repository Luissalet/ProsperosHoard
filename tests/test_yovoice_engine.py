import io
import json
import wave
import httpx
import pytest
from prosperos_hoard.yovoice_engine import YovoiceEngine


def audio():
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b'\0\0' * 80)
    return b.getvalue()


def test_jobs_keep_identity_and_auth_never_follows_remote_download_path():
    seen=[]
    def respond(req):
        seen.append(req)
        assert req.headers['Authorization'] == 'Bearer ' + 'a'*32
        if req.method == 'PUT':
            assert json.loads(req.content)['mode'] == 'text'
            return httpx.Response(200,json={'status':'running'})
        if req.url.path.startswith('/v1/jobs/'):
            return httpx.Response(200,json={'status':'completed','id':'audio123','downloadPath':'https://foreign.invalid/private'})
        return httpx.Response(200,content=audio())
    engine=YovoiceEngine('http://127.0.0.1:8085','a'*32,'index-2.5-q8',transport=httpx.MockTransport(respond))
    assert engine.synthesize('Hola',voice_ref='speaker1',style='alegre') == audio()
    assert seen[0].url.path == seen[1].url.path
    assert seen[-1].url.path == '/v1/audio/audio123'
    assert all(r.url.host == '127.0.0.1' for r in seen)


def test_rejects_external_server_and_bad_wav():
    assert not YovoiceEngine('https://external.invalid','a'*32,'model').is_installed()
    def respond(req):
        return httpx.Response(200,json={'status':'completed','id':'audio'}) if req.method=='PUT' else httpx.Response(200,content=b'wrong')
    e=YovoiceEngine('http://localhost:8085','a'*32,'kokoro',transport=httpx.MockTransport(respond))
    with pytest.raises(ValueError,match='valid WAV'):
        e.synthesize('Hello',voice_ref='voice')
