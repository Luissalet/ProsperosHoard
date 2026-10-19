"""Explicit OTIO sound lanes and reversible native provenance.

Untouched derived lanes collapse back into native video sound. External audio
edits/removals stay authoritative, with the corresponding picture muted.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import opentimelineio as otio

from .timeline import AUDIO_FILTERS, Clip

S = otio.schema
META = 'lumiere'
AUDIO_FIELDS = ('audio_stream','mute','volume_db','audio_fade_in','audio_fade_out')


def _copy(node: Any) -> Any:
    return otio.adapters.read_from_string(otio.adapters.write_to_string(node,adapter_name='otio_json'),adapter_name='otio_json')


def _hash(data: Any) -> str:
    def canonical(value):
        if isinstance(value,dict):return {k:canonical(v) for k,v in value.items()}
        if isinstance(value,list):return [canonical(v) for v in value]
        if isinstance(value,float):
            number=round(value,9)  # RationalTime round-trip noise, far below the native millisecond clock.
            return int(number) if number.is_integer() else number
        return value
    return hashlib.sha256(json.dumps(canonical(data),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def _track_hash(track: Any) -> str:
    return _hash(json.loads(otio.adapters.write_to_string(track,adapter_name='otio_json')))


def _audio_values(clip: dict) -> dict:
    values={k:clip[k] for k in AUDIO_FIELDS if k in clip}
    values['filters']=[f for f in clip.get('filters',[]) if f.get('type') in AUDIO_FILTERS]
    values['keyframes']={k:v for k,v in clip.get('keyframes',{}).items() if k=='volume_db'}
    return values


def _source_hash(track: Any,legacy: bool=False) -> str:
    from .interchange import _json, _signature, _ms
    native=_json(track.metadata.get(META,{})).get('track',{})
    items=[]
    for index,node in enumerate(track):
        if not isinstance(node,S.Clip):continue
        lead=_ms(track[index-1].in_offset) if index and isinstance(track[index-1],S.Transition) else 0
        tail=_ms(track[index+1].out_offset) if index+1<len(track) and isinstance(track[index+1],S.Transition) else 0
        clip=_json(node.metadata.get(META,{})).get('clip',{})
        state=_signature(node,_ms(track.range_of_child(node).start_time),lead,tail)
        if legacy and state.get('foreign_gain') is None:state.pop('foreign_gain',None)
        items.append({'id':clip.get('id'),'state':state,
                      'sound':_audio_values(clip),'speed_keys':clip.get('speed_keys',[]),'reverse':clip.get('reverse',False)})
    return _hash({'enabled':track.enabled,'kind':track.kind,'volume_db':native.get('volume_db',0),
                  'duck':native.get('duck',False),'muted':native.get('muted',False),'items':items})


def derive_audio(timeline: Any, svc: Any, report: list) -> dict:
    from . import media
    from .interchange import _json,_report
    manifest=[]
    derived=[]
    files=set()
    for source in timeline.tracks:
        data=_json(source.metadata.get(META,{}))
        native=data.get('track',{})
        if native.get('kind')!='video':continue
        clips={_json(n.metadata.get(META,{})).get('clip',{}).get('id'):_json(n.metadata.get(META,{})).get('clip',{})
               for n in source if isinstance(n,S.Clip)}
        audible={cid for cid,c in clips.items() if c.get('media') and not c.get('mute') and media.get(svc,c['media']).get('has_audio')}
        if not audible:continue
        audio=_copy(source)
        audio.kind=S.TrackKind.Audio
        audio.name=(source.name[:48]+' · source audio')[:60]
        audio.enabled=not native.get('muted',False)  # Hiding picture does not mute native sound.
        native_audio={**native,'id':native['id']+'_audio','kind':'audio','name':audio.name,'hidden':False}
        audio.metadata[META]={'version':1,'track':native_audio,'derived_audio_for':native['id']}
        audio.metadata['filmcraft']={'volume_db':native.get('volume_db',0)}
        audio.metadata[META]['filmcraft_sound_adapter']=1
        audio.metadata[META]['filmcraft_volume_at_export']=native.get('volume_db',0)
        for index,node in enumerate(list(audio)):
            if not isinstance(node,S.Clip):continue
            c=_json(node.metadata.get(META,{})).get('clip',{})
            if c.get('id') not in audible:
                audio[index]=S.Gap(source_range=otio.opentime.TimeRange(otio.opentime.RationalTime(0,node.duration().rate),node.duration()))
                continue
            master=media.audio_master(svc,c['media'],c.get('audio_stream',0))
            files.add(str(master))
            node.media_reference=S.ExternalReference(target_url=master.resolve().as_uri(),available_range=_copy(node.media_reference).available_range)
            sound=_audio_values(c)
            sound['audio_stream']=0  # The referenced FLAC contains the selected source stream only.
            node.metadata[META]={'version':1,'derived_audio':{'video_track':native['id'],'video_clip':c['id']},'audio':sound}
            node.metadata['filmcraft']={'gain_db':c.get('volume_db',0)}
            node.metadata[META]['filmcraft_sound_adapter']=1
            node.metadata[META]['filmcraft_gain_at_export']=c.get('volume_db',0)
            if c.get('volume_db') or c.get('audio_fade_in') or c.get('audio_fade_out') or c.get('audio_stream') or c.get('keyframes',{}).get('volume_db'):
                _report(report,'metadata_only',c['id'],'Derived audio gain, fades, selected stream and automation use Lumiere metadata; external editors may not apply them.')
            if c.get('reverse') or c.get('speed_keys'):
                _report(report,'approximated',c['id'],'Derived reverse/ramped sound remains editor-dependent; untouched native reimport restores its original settings.')
        manifest.append({'video_track':native['id'],'audio_track':native_audio['id'],
                         'signature_version':1,
                         'source_hash':_source_hash(source),'audio_hash':_track_hash(audio)})
        derived.append(audio)
    for audio in derived:timeline.tracks.append(audio)
    if manifest:timeline.metadata[META]['audio_derivations']=manifest
    return {'tracks':len(derived),'files':sorted(files)}


def classify_audio(timeline: Any, native: dict, report: list,svc: Any,folder: Any) -> tuple[set[int],set[str]]:
    from .interchange import _json,_report
    from .timeline_import import url_to_path
    from . import media
    from .errors import LumiereError
    from pathlib import Path
    def available(track):
        for node in track:
            if not isinstance(node,S.Clip) or not isinstance(node.media_reference,S.ExternalReference):continue
            path=Path(url_to_path(node.media_reference.target_url))
            if not path.is_absolute():path=folder/path
            try:
                media._check_root(svc,path.resolve())
                if not path.is_file():return False
            except (OSError,LumiereError):return False
        return True
    skip=set();mute=set()
    for record in native.get('audio_derivations',[]):
        origin=record['video_track']
        video=[t for t in timeline.tracks if isinstance(t,S.Track) and _json(t.metadata.get(META,{})).get('track',{}).get('id')==origin]
        sound=[(index,t) for index,t in enumerate(timeline.tracks) if isinstance(t,S.Track) and _json(t.metadata.get(META,{})).get('derived_audio_for')==origin]
        if len(video)==1 and len(sound)==1 and available(video[0]) and record.get('source_hash')==_source_hash(video[0],legacy=not record.get('signature_version')) and record.get('audio_hash')==_track_hash(sound[0][1]):
            skip.add(sound[0][0])
        else:
            mute.add(origin)
            _report(report,'approximated',origin,'External video/audio edits or a removed derived audio lane take precedence; picture sound is muted and remaining audio is imported independently.')
    return skip,mute


def restore_derived_audio(clip: Clip, node: Any) -> Clip:
    from .interchange import _json
    data=_json(node.metadata.get(META,{}))
    if data.get('version')!=1 or not data.get('derived_audio'):return clip
    values=_audio_values(data.get('audio',{}))
    # A derived lane holds sound only; original video masks/transforms/IDs never
    # leak into this independent audio clip or override external timing.
    return Clip.model_validate({**clip.model_dump(),**values})
