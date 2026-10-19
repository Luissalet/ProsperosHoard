from pathlib import Path

import opentimelineio as otio
import pytest

from conftest import needs_ffmpeg
from test_interchange import montage,write
from prosperos_hoard.editing import interchange,projects
from prosperos_hoard.editing.timeline import SpeedKey

pytestmark=needs_ffmpeg
S,O=otio.schema,otio.opentime


def exported(svc,media_dir):
    pid,before=montage(svc,media_dir)
    data=interchange.export_project(svc,pid)
    return pid,before,otio.adapters.read_from_string(data['otio'],adapter_name='otio_json'),data


def audio(doc,origin):
    return next(t for t in doc.tracks if t.metadata.get('lumiere',{}).get('derived_audio_for')==origin)


def imported(svc,folder,doc):
    result=interchange.import_project(svc,write(folder,otio.adapters.write_to_string(doc,adapter_name='otio_json')))
    return projects.doc(svc,result['id']),result


def test_explicit_audio_untouched_collapses_to_exact_native(services,media_dir,tmp_path):
    pid,before,doc,data=exported(services,media_dir)
    assert data['derived_audio_tracks']==2
    lane=audio(doc,before.tracks[0].id)
    assert lane.kind==S.TrackKind.Audio and lane.enabled
    assert isinstance(lane[0],S.Clip) and isinstance(lane[1],S.Transition)
    hidden=audio(doc,before.tracks[3].id)
    assert hidden.enabled and not doc.tracks[3].enabled
    after,result=imported(services,tmp_path,doc)
    assert after.dump()==before.dump() and not result['report']


def test_removed_derived_lane_does_not_resurrect_sound(services,media_dir,tmp_path):
    _,before,doc,_=exported(services,media_dir)
    doc.tracks.remove(audio(doc,before.tracks[0].id))
    after,result=imported(services,tmp_path,doc)
    assert len(after.tracks)==4 and all(c.mute for c in after.tracks[0].clips)
    assert not after.tracks[3].clips[0].mute  # Untouched hidden source audio still coalesces.
    assert any('removed derived audio' in r['message'] for r in result['report'])


def test_external_sound_move_is_preserved_and_undo_is_atomic(services,media_dir,tmp_path):
    _,before,doc,_=exported(services,media_dir)
    lane=audio(doc,before.tracks[0].id)
    lane.insert(0,S.Gap(source_range=O.TimeRange(O.RationalTime(0,1000),O.RationalTime(250,1000))))
    after,result=imported(services,tmp_path,doc)
    real=next(t for t in after.tracks if t.id==before.tracks[0].id+'_audio')
    assert len(after.tracks)==5 and real.clips[0].start==250 and real.clips[1].start==1750
    assert all(c.mute for c in after.tracks[0].clips) and not real.clips[0].mute
    assert real.clips[0].id!=before.tracks[0].clips[0].id
    assert projects.undo(services,result['id']) and not any(t.clips for t in projects.doc(services,result['id']).tracks)
    projects.redo(services,result['id'])
    assert projects.doc(services,result['id']).dump()==after.dump()


def test_video_move_leaves_external_audio_at_its_real_time(services,media_dir,tmp_path):
    _,before,doc,_=exported(services,media_dir)
    doc.tracks[0].insert(0,S.Gap(source_range=O.TimeRange(O.RationalTime(0,1000),O.RationalTime(400,1000))))
    after,_=imported(services,tmp_path,doc)
    assert after.tracks[0].clips[0].start==400 and after.tracks[0].clips[0].mute
    lane=next(t for t in after.tracks if t.id==before.tracks[0].id+'_audio')
    assert lane.clips[0].start==0


def test_muted_clip_gap_and_hidden_sound_keep_native_state(services,media_dir,tmp_path):
    pid,before,_,_=exported(services,media_dir)
    before.tracks[0].clips[0].mute=True
    before.tracks[3].muted=True
    projects.save(services,pid,before,'Muted source fixture')
    data=interchange.export_project(services,pid)
    doc=otio.adapters.read_from_string(data['otio'],adapter_name='otio_json')
    assert isinstance(audio(doc,before.tracks[0].id)[0],S.Gap)
    assert not audio(doc,before.tracks[3].id).enabled
    after,_=imported(services,tmp_path,doc)
    assert after.dump()==before.dump()


def test_disabled_derived_lane_becomes_explicit_muted_sound(services,media_dir,tmp_path):
    _,before,doc,_=exported(services,media_dir)
    audio(doc,before.tracks[0].id).enabled=False
    after,_=imported(services,tmp_path,doc)
    assert all(c.mute for c in after.tracks[0].clips)
    lane=next(t for t in after.tracks if t.id==before.tracks[0].id+'_audio')
    assert lane.muted


def test_removed_sound_clip_leaves_gap_and_remaining_sound(services,media_dir,tmp_path):
    _,before,doc,_=exported(services,media_dir)
    lane=audio(doc,before.tracks[0].id)
    lane[0]=S.Gap(source_range=O.TimeRange(O.RationalTime(0,lane[0].duration().rate),lane[0].duration()))
    after,_=imported(services,tmp_path,doc)
    sound=next(t for t in after.tracks if t.id==before.tracks[0].id+'_audio')
    assert len(sound.clips)==1 and sound.clips[0].start==1500
    assert all(c.mute for c in after.tracks[0].clips)


def test_external_namespaced_sound_gain_is_not_replaced_by_original(services,media_dir,tmp_path):
    _,before,doc,_=exported(services,media_dir)
    lane=audio(doc,before.tracks[0].id)
    lane[0].metadata['lumiere']['audio']['volume_db']=-12
    after,_=imported(services,tmp_path,doc)
    sound=next(t for t in after.tracks if t.id==before.tracks[0].id+'_audio')
    assert sound.clips[0].volume_db==-12 and all(c.mute for c in after.tracks[0].clips)


def test_ui_import_endpoint_uses_same_audio_coalescing_and_failures_are_atomic(client,media_dir,tmp_path):
    _,before,doc,_=exported(client.svc,media_dir)
    path=write(tmp_path,otio.adapters.write_to_string(doc,adapter_name='otio_json'))
    result=client.post('/api/projects/import-timeline',json={'path':path,'title':'UI round trip'})
    assert result.status_code==200,result.text
    assert projects.doc(client.svc,result.json()['project_id']).dump()==before.dump()
    count=client.svc.db.one('SELECT COUNT(*) c FROM projects')['c']
    denied=client.post('/api/projects/import-timeline',json={'path':str(tmp_path/'unsupported.bin')})
    assert denied.status_code==400 and client.svc.db.one('SELECT COUNT(*) c FROM projects')['c']==count


def test_target_editor_clip_and_track_gain_changes_take_precedence(services,media_dir,tmp_path):
    _,before,doc,_=exported(services,media_dir)
    lane=audio(doc,before.tracks[0].id)
    lane[0].metadata['filmcraft']['gain_db']=-6
    lane.metadata['filmcraft']['volume_db']=-3
    after,_=imported(services,tmp_path,doc)
    sound=next(t for t in after.tracks if t.id==before.tracks[0].id+'_audio')
    assert sound.volume_db==-3 and sound.clips[0].volume_db==-6
    assert all(c.mute for c in after.tracks[0].clips)


def test_normalized_masters_are_stereo_and_originals_are_untouched(services,media_dir):
    import hashlib,json,subprocess
    originals={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (media_dir/'talk.mp4',media_dir/'clicks.wav')}
    _,_,_,result=exported(services,media_dir)
    assert len(result['audio_sources'])==2
    for path in result['audio_sources']:
        streams=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','stream=channels,sample_rate,codec_name','-of','json',path]))['streams']
        assert len(streams)==1 and streams[0]['channels']==2 and streams[0]['sample_rate']=='48000' and streams[0]['codec_name']=='flac'
    assert all(hashlib.sha256(p.read_bytes()).hexdigest()==digest for p,digest in originals.items())


def test_concurrent_exports_share_complete_audio_cache(services,media_dir):
    from concurrent.futures import ThreadPoolExecutor
    pid,_=montage(services,media_dir)
    with ThreadPoolExecutor(max_workers=3) as pool:
        result=list(pool.map(lambda _:interchange.export_project(services,pid),range(3)))
    assert result[0]['audio_sources']==result[1]['audio_sources']==result[2]['audio_sources']
    assert all(Path(p).is_file() and Path(p).stat().st_size>0 for p in result[0]['audio_sources'])


def test_earlier_v1_clip_signatures_remain_readable(services,media_dir,tmp_path):
    _,before,doc,_=exported(services,media_dir)
    # Approximate a prior v1 linked-audio file by restoring its original URL
    # and dropping the newly introduced target-gain signature field.
    for track in doc.tracks:
        for node in track:
            if isinstance(node,S.Clip) and node.metadata.get('lumiere',{}).get('signature'):
                meta=node.metadata['lumiere']
                node.metadata.pop('filmcraft',None)
                meta['signature'].pop('foreign_gain',None)
    # Keep current derived manifest in sync with that unchanged source layout.
    from prosperos_hoard.editing.interchange_audio import _source_hash
    for record in doc.metadata['lumiere']['audio_derivations']:
        source=next(t for t in doc.tracks if t.metadata.get('lumiere',{}).get('track',{}).get('id')==record['video_track'])
        record['source_hash']=_source_hash(source,legacy=True);record.pop('signature_version',None)
    after,_=imported(services,tmp_path,doc)
    assert after.dump()==before.dump()


def test_offline_picture_does_not_discard_available_source_sound(services,media_dir,tmp_path):
    import shutil
    folder=tmp_path/'independent-source';folder.mkdir()
    for name in ('talk.mp4','clicks.wav'):shutil.copy(media_dir/name,folder/name)
    _,_,doc,_=exported(services,folder)
    (folder/'talk.mp4').unlink()
    after,result=imported(services,tmp_path,doc)
    sound=[c for t in after.tracks if t.kind=='audio' for c in t.clips]
    assert len(sound)==4 and not after.tracks[0].clips
    assert any(r['status']=='omitted' for r in result['report'])


def test_missing_original_audio_uses_available_normalized_reference(services,media_dir,tmp_path):
    import shutil
    folder=tmp_path/'independent-source';folder.mkdir()
    for name in ('talk.mp4','clicks.wav'):shutil.copy(media_dir/name,folder/name)
    _,_,doc,_=exported(services,folder)
    (folder/'clicks.wav').unlink()
    after,result=imported(services,tmp_path,doc)
    assert len(after.tracks[2].clips)==1
    assert any('normalized stereo reference' in r['message'] for r in result['report'])


def test_signature_none_to_gain_edit_is_a_change_not_a_type_error():
    assert not interchange._close({'foreign_gain':None},{'foreign_gain':0})
    assert interchange._close({}, {'foreign_gain':None})


def test_stripped_metadata_keeps_separate_sound_without_double_mix(services,media_dir,tmp_path):
    _,before,doc,_=exported(services,media_dir)
    doc.metadata.clear()
    for t in doc.tracks:
        t.metadata.clear()
        for node in t:
            node.metadata.clear()
    after,_=imported(services,tmp_path,doc)
    video=[c for t in after.tracks if t.kind=='video' for c in t.clips]
    sound=[c for t in after.tracks if t.kind=='audio' for c in t.clips]
    assert all(c.mute for c in video) and len(sound)==4 and all(not c.mute for c in sound)


def test_native_speed_ramp_reverse_audio_restores_without_approximation_on_import(services,media_dir,tmp_path):
    pid,before,_,_=exported(services,media_dir)
    before.tracks[0].clips[0].speed_keys=[SpeedKey(t=1000,v=.8),SpeedKey(t=3000,v=1.2)]
    before.tracks[0].clips[1].start=2200
    before.tracks[0].clips[1].reverse=True
    before.tracks[0].clips[1].transition_in=None
    projects.save(services,pid,before,'Speed sound fixture')
    data=interchange.export_project(services,pid)
    doc=otio.adapters.read_from_string(data['otio'],adapter_name='otio_json')
    after,result=imported(services,tmp_path,doc)
    assert after.dump()==before.dump()
    assert any('reverse/ramped' in r['message'] for r in data['report'])
    assert not result['report']
