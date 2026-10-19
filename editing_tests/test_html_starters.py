import hashlib
import json

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from conftest import FakeLink, make_services
from prosperos_hoard.editing.html_motion import HtmlMotion
from prosperos_hoard.editing.html_starters import HtmlStarterArgs, catalogue, save_args
from prosperos_hoard.editing.main import create_app
from test_html_motion import BROWSER


@pytest.mark.parametrize('profile', ['title', 'layered', 'oscillators', 'elastic'])
@BROWSER
def test_seek_out_of_order_is_repeatable_with_real_worker(profile, tmp_path):
    store = HtmlMotion(tmp_path)
    args = save_args(HtmlStarterArgs(profile=profile, text='UNA IDEA', width=640, height=360, duration=4))
    item = store.save(**args.model_dump())
    result = store.storyboard(item['id'], [3, 1, 0, 1])
    hashes = [f['sha256'] for f in result['frames']]
    assert hashes[1] == hashes[3], 'Repeated time depends on previous Canvas state'
    assert len(set(hashes)) == 3
    assert result['source_sha256'] == hashlib.sha256(args.html.encode()).hexdigest()
    assert store.storyboard_source(item['id'], 1, result['id']) == args.html.encode()
    assert 'requestAnimationFrame' not in args.html and 'http://' not in args.html


def test_text_is_literal_even_with_script_closing_markup():
    literal = '</script><script>window.hacked=1</script> & café "sí"'
    source = save_args(HtmlStarterArgs(profile='title', text=literal)).html
    assert source.count('</script>') == 1 and 'window.hacked=1</script>' not in source
    config = json.loads(source.split('const CONFIG=', 1)[1].split(';\nconst W', 1)[0])
    assert config['text'] == literal


@BROWSER
def test_elastic_keeps_accent_and_joined_emoji_as_drawn_graphemes(tmp_path):
    store = HtmlMotion(tmp_path)
    text = 'e\u0301\U0001f469\u200d\U0001f4bb'
    args = save_args(HtmlStarterArgs(profile='elastic', text=text, width=640, height=360))
    item = store.save(**args.model_dump())
    with store.page(item) as (page, errors):
        drawn = page.evaluate('''() => {
            const drawn=[],proto=CanvasRenderingContext2D.prototype,old=proto.fillText;
            proto.fillText=function(text,...rest){drawn.push(text);return old.call(this,text,...rest);};
            try {window.seek(3);return drawn;}finally {proto.fillText=old;}
        }''')
        assert not errors
        assert drawn == ['e\u0301', '\U0001f469\u200d\U0001f4bb']
    frames = store.storyboard(item['id'], [3, 1, 3])['frames']
    assert frames[0]['sha256'] == frames[2]['sha256']


@pytest.mark.parametrize('changes', [{'profile': 'unknown'}, {'duration': float('inf')}, {'fps': float('nan')}, {'width': 0}])
def test_invalid_source_configuration(changes):
    with pytest.raises(ValidationError):
        save_args(HtmlStarterArgs(**{'profile': 'title', **changes}))


def test_catalogue_does_not_expose_mutable_shared_state():
    first = catalogue()
    first['starters'][0]['name'] = 'changed'
    assert catalogue()['starters'][0]['name'] == 'Título cinético'


def test_native_api_creates_separate_editable_sources(tmp_path):
    svc = make_services(tmp_path, link=FakeLink())
    app = create_app(svc.config, services=svc)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        listed = client.get('/api/html-motion/starters')
        assert listed.status_code == 200 and len(listed.json()['starters']) == 4
        one = client.post('/api/html-motion/starters', json={'profile': 'layered', 'text': 'PRIMERA'}).json()
        two = client.post('/api/html-motion/starters', json={'profile': 'elastic', 'text': 'SEGUNDA'}).json()
        assert one['id'] != two['id']
        original = client.get('/api/html-motion/' + one['id']).json()
        assert original['html'] == save_args(HtmlStarterArgs(profile='layered', text='PRIMERA')).html
        edit = client.post('/api/html-motion', json={'composition': one['id'], 'expected_revision': 1, 'html': original['html']+'<!-- own correction -->', 'name': 'EDITADA'})
        assert edit.status_code == 200 and edit.json()['revision'] == 2
        assert client.get('/api/html-motion/' + one['id'], params={'revision': 1}).json()['html'] == original['html']
        assert client.get('/api/html-motion/' + two['id']).json()['revision'] == 1
        invalid = client.post('/api/html-motion/starters', json={'profile': 'unknown'})
        assert invalid.status_code == 400 and 'profile:' in invalid.json()['error']
