"""Timing round trips through the editor/agent API, with isolated audio and stores."""
import copy
import pytest

from prosperos_hoard import engine, lyrics_sync as sync, productions as prod, timeline
from test_productions import tiny_spec


def setup_song(store):
    project = store.create_project('Timing test')
    path = store.path_for_asset_file('a_timingtest', '.wav')
    path.write_bytes(b'test audio never decoded by these tests')
    song = store.create_asset(project_id=project['id'], kind='audio', file_path=path.relative_to(store.data_dir).as_posix(),
        mime='audio/wav', source='import', asset_id='a_timingtest', duration_s=20)
    text = '[Verse]\nA quiet street\nNot heard here\nBright blue lights'
    spec = tiny_spec(song={'asset_id': song['id'], 'lyrics': text})
    state = prod.create_production(store.data_dir, 'Timing test', spec, project_id=project['id'])
    state['done']['song'] = {'song_asset_id': song['id'], 'duration_s': 20}
    state['done']['frames'] = {'items': {'1': {'best': 'a_keep_frame'}}, 'complete': True}
    state['done']['clips'] = {'items': {'1': {'asset_id': 'a_keep_clip'}}, 'complete': True}
    state['done']['animatic'] = {'renders': {'16:9': 'a_old_render'}}
    prod.save_state(store.data_dir, state)
    return state, {'song_asset_id': song['id'], 'lyrics': text, 'method': 'manual', 'lines': [
        {'text': 'A quiet street', 'time_s': 2.125, 'end_s': 4.25},
        {'text': 'Not heard here', 'time_s': None, 'end_s': None},
        {'text': 'Bright blue lights', 'time_s': 12.5, 'end_s': 14.75}]}


def test_manual_production_roundtrip_preserves_text_gaps_assets_and_caption_ends(client):
    c, app, _ = client
    store = app.state.store
    state, body = setup_song(store)
    before = copy.deepcopy(state)
    response = c.post(f"/api/productions/{state['slug']}/lyrics-timing", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['timed_lines'] == 2 and not result['complete']
    saved = prod.load_state(store.data_dir, state['slug'])
    assert saved['spec'] == before['spec']
    assert saved['done']['frames'] == before['done']['frames']
    assert saved['done']['clips'] == before['done']['clips']
    assert 'animatic' not in saved['done']
    lyrics = engine.read_lyrics(store, result['id'])
    assert lyrics['lines'][0]['end_s'] == 4.25
    timing = prod.shot_timing(store.data_dir, store, saved)
    assert timing['text_timing']['lines'][1]['time_s'] is None
    assert timing['lines'][0]['end_s'] == 4.25
    captions = timeline.lyrics_clips_from_lines(lyrics['lines'], 20, False)
    assert captions[0]['end_s'] == 4.25 and captions[-1]['end_s'] == 14.75
    graphic = timeline._lines_in_window(lyrics['lines'], 0, 20)
    assert graphic[0]['end_s'] == 4.25
    engine.save_lyrics(store, result['id'], '[00:03.00]Changed separately')
    assert 'text_timing' not in engine.read_lyrics(store, result['id'])


def test_alignment_preview_never_writes_and_preserves_missing_text(client, monkeypatch):
    c, app, _ = client
    state, body = setup_song(app.state.store)
    before = (app.state.store.data_dir / 'productions' / state['slug'] / 'state.json').read_bytes()
    monkeypatch.setattr(sync, 'transcribe_words', lambda *a, **k: {'model': 'test', 'device': 'cpu', 'segments': [
        {'words': [{'word': word, 'start_s': at + i * .3, 'end_s': at + i * .3 + .2}
                   for at, words in [(2, 'a quiet street'), (12, 'bright blue lights')] for i, word in enumerate(words.split())]}]})
    response = c.post(f"/api/productions/{state['slug']}/lyrics-timing", json={**body, 'method': 'align', 'preview': True})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['timed_lines'] == 2 and result['lines'][1]['text'] == 'Not heard here'
    assert before == (app.state.store.data_dir / 'productions' / state['slug'] / 'state.json').read_bytes()
    assert app.state.store.list_assets(state['project_id'], kind='lyrics')['items'] == []


def test_changed_source_and_overlaps_rejected_without_state_write(client):
    c, app, _ = client
    state, body = setup_song(app.state.store)
    before = (app.state.store.data_dir / 'productions' / state['slug'] / 'state.json').read_bytes()
    bad_rows = copy.deepcopy(body['lines'])
    bad_rows[2]['time_s'] = 3
    for changed in ({'lyrics': 'New text'}, {'song_asset_id': 'a_other'}, {'lines': bad_rows}):
        response = c.post(f"/api/productions/{state['slug']}/lyrics-timing", json={**body, **changed})
        assert response.status_code >= 400, response.text
        assert before == (app.state.store.data_dir / 'productions' / state['slug'] / 'state.json').read_bytes()


def test_source_changed_during_asr_is_not_bound(client, monkeypatch):
    c, app, _ = client
    state, body = setup_song(app.state.store)
    def changed(*args, **kwargs):
        current = prod.load_state(app.state.store.data_dir, state['slug'])
        current['spec']['song']['lyrics'] = 'New draft written during ASR'
        prod.save_state(app.state.store.data_dir, current)
        return {'segments': []}
    monkeypatch.setattr(sync, 'transcribe_words', changed)
    response = c.post(f"/api/productions/{state['slug']}/lyrics-timing", json={**body, 'method': 'align'})
    assert response.status_code >= 400
    current = prod.load_state(app.state.store.data_dir, state['slug'])
    assert current['spec']['song']['lyrics'] == 'New draft written during ASR'
    assert not current['done'].get('lyrics')
    assert app.state.store.list_assets(state['project_id'], kind='lyrics')['items'] == []


def test_agent_api_can_link_manual_timing_to_production(client):
    c, app, _ = client
    state, body = setup_song(app.state.store)
    response = c.post('/api/agent/studio_time_lyrics', params={'project': state['project_id']}, json={**body, 'production': state['slug']})
    assert response.status_code == 200, response.text
    assert prod.load_state(app.state.store.data_dir, state['slug'])['done']['lyrics']['lyrics_asset_id'] == response.json()['id']

