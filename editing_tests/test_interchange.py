from pathlib import Path
import json

import opentimelineio as otio
import pytest

from conftest import needs_ffmpeg
from prosperos_hoard.editing import interchange, media, projects, agent_tools
from prosperos_hoard.editing.errors import LumiereError
from prosperos_hoard.editing.timeline import Clip, Track, Transition, Marker, TextStyle, Filter, Keyframe, Transform

pytestmark = needs_ffmpeg
S, O = otio.schema, otio.opentime


def montage(svc, folder):
    info = media.import_path(svc,str(folder/'talk.mp4'))
    audio = media.import_path(svc,str(folder/'clicks.wav'))
    created = projects.create(svc,'Interchange — prueba',width=640,height=360,fps=24000/1001)
    p = projects.doc(svc,created['id'])
    p.tracks[0].clips = [Clip(media=info['id'],start=0,src_in=1000,src_out=3000,label='A'),
                         Clip(media=info['id'],start=1500,src_in=2000,src_out=4000,label='B',transition_in=Transition(dur=500))]
    p.tracks[1].clips = [Clip(type='text',start=600,length=1800,text='Título ñ',style=TextStyle(size=46))]
    p.tracks[2].clips = [Clip(media=audio['id'],start=250,src_in=0,src_out=3000,volume_db=-4)]
    overlay=Track(kind='video',name='Overlay',volume_db=-2,hidden=True)
    overlay.clips=[Clip(media=info['id'],start=1000,src_in=1000,src_out=2000,filters=[Filter(type='sepia')],
                        keyframes={'opacity':[Keyframe(t=0,v=0),Keyframe(t=500,v=1)]})]
    p.tracks.append(overlay)
    p.notes='Notas preservadas'
    p.markers=[Marker(t=1750,label='Corte',kind='chapter')]
    projects.save(svc,created['id'],p,'Fixture')
    return created['id'],p


def write(folder,doc):
    path=folder/'montage.otio'
    path.write_text(doc,encoding='utf-8')
    return str(path)


def test_real_serializer_roundtrip_all_tracks_and_render(services,media_dir,tmp_path):
    pid,before=montage(services,media_dir)
    exported=interchange.export_project(services,pid)
    parsed=otio.adapters.read_from_string(exported['otio'],adapter_name='otio_json')
    assert len(parsed.tracks)==6 and exported['tracks']==4 and exported['derived_audio_tracks']==2
    assert parsed.tracks[0][1].schema_name()=='Transition'
    assert parsed.tracks[0].duration().to_seconds()==pytest.approx(3.5)
    assert parsed.tracks[0][0].visible_range().duration.to_seconds()==pytest.approx(2)
    assert parsed.tracks[0][2].visible_range().duration.to_seconds()==pytest.approx(2)
    assert parsed.tracks[2][0].schema_name()=='Gap'
    assert parsed.tracks.markers[0].color==S.MarkerColor.ORANGE
    result=interchange.import_project(services,write(tmp_path,exported['otio']))
    after=projects.doc(services,result['id'])
    assert after.dump()==before.dump()
    assert any(r['status']=='metadata_only' for r in exported['report'])
    assert projects.undo(services,result['id']) and not any(t.clips for t in projects.doc(services,result['id']).tracks)
    projects.redo(services,result['id'])
    from prosperos_hoard.editing.render.runner import render_frame
    for instant in (900,1750):  # Title and the middle of the actual dissolve.
        a=render_frame(services,pid,instant,width=640,fmt='png')
        b=render_frame(services,result['id'],instant,width=640,fmt='png')
        assert Path(a).read_bytes()==Path(b).read_bytes()


def test_external_otio_relative_refs_missing_gap_transition_and_fractional_rate(services,media_dir,tmp_path):
    rate=24000/1001
    rt=lambda frames: O.RationalTime(frames,rate)
    tr=lambda start,length: O.TimeRange(rt(start),rt(length))
    # Independent standard schema fixture, no Lumiere metadata.
    a=S.Clip(name='A',media_reference=S.ExternalReference(target_url=Path(media_dir/'talk.mp4').as_uri()),source_range=tr(24,36))
    b=S.Clip(name='B',media_reference=S.ExternalReference(target_url=Path(media_dir/'talk.mp4').as_uri()),source_range=tr(48,36))
    b.effects.append(S.Effect(name='Unsupported grade',effect_name='external.fx'))
    missing=S.Clip(name='Offline',media_reference=S.ExternalReference(target_url='missing.mp4'),source_range=tr(0,24))
    track=S.Track(kind=S.TrackKind.Video,children=[S.Gap(source_range=tr(0,12)),a,S.Transition(in_offset=rt(6),out_offset=rt(6)),b,missing])
    track2=S.Track(kind=S.TrackKind.Audio,children=[S.Gap(source_range=tr(0,24)),S.Clip(media_reference=S.ExternalReference(target_url=Path(media_dir/'clicks.wav').as_uri()),source_range=tr(0,48))])
    doc=S.Timeline(name='Independent',tracks=[track,track2],global_start_time=rt(0))
    doc.tracks.markers.append(S.Marker(name='Frame 1',marked_range=tr(1,0)))
    result=interchange.import_project(services,write(tmp_path,otio.adapters.write_to_string(doc,adapter_name='otio_json')))
    p=projects.doc(services,result['id'])
    assert p.canvas.fps==rate and len(p.tracks)==2
    assert [(c.start,c.src_in,c.src_out) for c in p.tracks[0].clips]==[(500,1001,2753),(1752,1752,3504)]
    assert p.tracks[0].clips[1].transition_in.dur==500
    assert p.markers[0].t==42
    assert p.markers[0].color=='#FF0000' and p.markers[0].kind=='note'
    statuses={r['status'] for r in result['report']}
    assert {'omitted','approximated','unsupported'} <= statuses


def test_external_edit_takes_precedence_and_unsupported_nested_is_atomic(services,media_dir,tmp_path):
    pid,_=montage(services,media_dir)
    data=interchange.export_project(services,pid)
    doc=otio.adapters.read_from_string(data['otio'],adapter_name='otio_json')
    # Edit the independent overlay's source trim; old appearance metadata must not overwrite it.
    node=next(n for n in doc.tracks[3] if isinstance(n,S.Clip))
    node.source_range=O.TimeRange(O.RationalTime(2000,1000),node.source_range.duration)
    imported=interchange.import_project(services,write(tmp_path,otio.adapters.write_to_string(doc,adapter_name='otio_json')))
    c=projects.doc(services,imported['id']).tracks[3].clips[0]
    assert c.src_in==2000 and not c.filters
    assert any('take precedence' in r['message'] for r in imported['report'])
    count=services.db.one('SELECT COUNT(*) c FROM projects')['c']
    doc.tracks[0].append(S.Stack(children=[S.Track()]))
    with pytest.raises(LumiereError,match='Nested'):
        interchange.import_project(services,write(tmp_path,otio.adapters.write_to_string(doc,adapter_name='otio_json')))
    assert services.db.one('SELECT COUNT(*) c FROM projects')['c']==count


def test_native_http_and_mcp_use_actual_file(client,media_dir,tmp_path):
    pid,_=montage(client.svc,media_dir)
    token={'Authorization':f'Bearer {client.svc.token}'}
    export=client.post('/api/agent/call',headers=token,json={'name':'project_export_otio','arguments':{'project':pid},'reason':'Export the montage for another editor'})
    assert export.status_code==200,export.text
    result=export.json()
    assert Path(result['path']).is_file()
    download=client.get(f'/api/projects/{pid}/otio')
    assert download.status_code==200 and download.content==Path(result['path']).read_bytes()
    imported=client.post('/api/agent/call',headers=token,json={'name':'project_from_timeline','arguments':{'otio_path':result['path']},'reason':'Open the exported montage again'})
    assert imported.status_code==200,imported.text
    assert projects.doc(client.svc,imported.json()['id']).dump()==projects.doc(client.svc,pid).dump()
    assert 'otio_path' in agent_tools.ProjectFromTimelineArgs.model_json_schema()['properties']


def test_missing_or_malformed_file_does_not_create_project(services,tmp_path):
    path=tmp_path/'bad.otio'
    for data in ['{', '{"OTIO_SCHEMA":"Timeline.999","tracks":{}}']:
        path.write_text(data,encoding='utf-8')
        with pytest.raises(LumiereError):
            interchange.import_project(services,str(path))
    assert services.db.one('SELECT COUNT(*) c FROM projects')['c']==0


def test_native_speed_ramps_reverse_and_nonoverlapping_transition_roundtrip(services,media_dir,tmp_path):
    from prosperos_hoard.editing.timeline import SpeedKey
    pid,_=montage(services,media_dir)
    p=projects.doc(services,pid)
    p.main_track().clips=[
        Clip(media=p.main_track().clips[0].media,start=0,src_in=1000,src_out=3000,speed=2),
        Clip(media=p.main_track().clips[0].media,start=1000,src_in=3000,src_out=5000,reverse=True),
        Clip(media=p.main_track().clips[0].media,start=3000,src_in=1000,src_out=4000,
             speed_keys=[SpeedKey(t=1000,v=1),SpeedKey(t=4000,v=2)],transition_in=Transition(dur=300)),
    ]
    projects.save(services,pid,p,'Speed fixture')
    exported=interchange.export_project(services,pid)
    result=interchange.import_project(services,write(tmp_path,exported['otio']))
    assert projects.doc(services,result['id']).dump()==p.dump()
    assert any('Non-overlapping' in r['message'] for r in exported['report'])


def test_stacked_external_warps_are_not_silently_discarded(services,media_dir,tmp_path):
    node=S.Clip(media_reference=S.ExternalReference(target_url=Path(media_dir/'talk.mp4').as_uri()),
                source_range=O.TimeRange(O.RationalTime(0,24),O.RationalTime(96,24)))
    node.effects.extend([S.LinearTimeWarp(time_scalar=2),S.LinearTimeWarp(time_scalar=2)])
    doc=S.Timeline(tracks=[S.Track(children=[node])])
    with pytest.raises(LumiereError,match='stacked'):
        interchange.import_project(services,write(tmp_path,otio.adapters.write_to_string(doc,adapter_name='otio_json')))
    assert services.db.one('SELECT COUNT(*) c FROM projects')['c']==0


def test_filmcraft_canvas_and_unmapped_appearance_are_reported(services,media_dir,tmp_path):
    node=S.Clip(media_reference=S.ExternalReference(target_url=Path(media_dir/'talk.mp4').as_uri()),
                source_range=O.TimeRange(O.RationalTime(0,24),O.RationalTime(48,24)),
                metadata={'filmcraft':{'effects':[{'effect':'motion','params':{'scale':{'value':{'Float':150}}}}]}})
    doc=S.Timeline(tracks=[S.Track(children=[node])],global_start_time=O.RationalTime(0,24),
                   metadata={'filmcraft':{'settings':{'width':640,'height':360,'sample_rate':44100}}})
    result=interchange.import_project(services,write(tmp_path,otio.adapters.write_to_string(doc,adapter_name='otio_json')))
    canvas=projects.doc(services,result['id']).canvas
    assert (canvas.width,canvas.height,canvas.sample_rate)==(640,360,44100)
    assert any(r['status']=='unsupported' and 'filmcraft' in r['message'] for r in result['report'])
    assert any(r['item']=='canvas' and 'dimensions' in r['message'] for r in result['report'])


def test_external_split_audio_is_not_mixed_twice(services,media_dir,tmp_path):
    from prosperos_hoard.editing.render.compiler import audio_graph, MediaRef
    reference=lambda: S.ExternalReference(target_url=Path(media_dir/'talk.mp4').as_uri())
    span=lambda: O.TimeRange(O.RationalTime(0,24),O.RationalTime(48,24))
    doc=S.Timeline(tracks=[S.Track(kind=S.TrackKind.Video,children=[S.Clip(media_reference=reference(),source_range=span())]),
                           S.Track(kind=S.TrackKind.Audio,children=[S.Clip(media_reference=reference(),source_range=span())])])
    result=interchange.import_project(services,write(tmp_path,otio.adapters.write_to_string(doc,adapter_name='otio_json')))
    p=projects.doc(services,result['id'])
    assert p.tracks[0].clips[0].mute and not p.tracks[1].clips[0].mute
    def lookup(mid):
        info=media.get(services,mid)
        return MediaRef(**{k:info[k] for k in ('id','path','kind','width','height','has_audio','has_video','duration_ms')})
    graph=audio_graph(p,p.duration,lookup,lambda mid,stream:'master.flac')
    assert graph.clips==1, 'The video and separate audio track must not double the mix.'


def test_filmcraft_static_framing_and_animated_gap(services,media_dir,tmp_path):
    info=media.import_path(services,str(media_dir/'talk.mp4'))
    params={'anchor':{'value':{'Vec2':{'x':info['width']/2,'y':info['height']/2}}},
            'position':{'value':{'Vec2':{'x':360,'y':160}}},'scale':{'value':{'Float':75}},
            'rotation':{'value':{'Float':12}},'uniform_scale':{'value':{'Bool':True}}}
    node=S.Clip(media_reference=S.ExternalReference(target_url=Path(media_dir/'talk.mp4').as_uri()),
                source_range=O.TimeRange(O.RationalTime(0,24),O.RationalTime(48,24)),
                metadata={'filmcraft':{'effects':[{'effect':'motion','params':params}]}})
    doc=S.Timeline(tracks=[S.Track(children=[node])],metadata={'filmcraft':{'settings':{'width':640,'height':360}}})
    def imported():
        result=interchange.import_project(services,write(tmp_path,otio.adapters.write_to_string(doc,adapter_name='otio_json')))
        return projects.doc(services,result['id']).tracks[0].clips[0],result
    clip,result=imported()
    assert clip.transform.fit=='none' and clip.transform.scale==.75 and clip.transform.rotation==12
    assert clip.transform.x==pytest.approx(40/640) and clip.transform.y==pytest.approx(-20/360)
    assert any('uniform scale' in r['message'] for r in result['report'])
    node.metadata['filmcraft']['effects'][0]['params']['scale']['keyframes']=[{'time':1,'value':100}]
    clip,result=imported()
    assert clip.transform==Transform()
    assert not any('uniform scale' in r['message'] for r in result['report'])
