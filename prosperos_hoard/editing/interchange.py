"""OTIO interchange using its official schema/serializer, never executing adapters.

Standard tracks, gaps, media ranges, dissolves and markers travel between editors.
Lumiere-specific appearance is carried in versioned metadata. An unchanged clip
round-trips that appearance exactly; changed external timing takes precedence.
The native project clock is milliseconds, so fractional imports report rounding.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, TYPE_CHECKING

import opentimelineio as otio

from . import projects, media
from .errors import LumiereError
from .timeline import Canvas, Clip, Crop, Marker, Project, Track, Transform, Transition, validate
from .timeline_import import Resolver, _read_text, url_to_path
from .util import atomic_write

if TYPE_CHECKING:
    from .services import Services

META = 'lumiere'
S = otio.schema
O = otio.opentime
MARKER_COLORS={'BLACK':'#000000','WHITE':'#FFFFFF','RED':'#FF0000','PINK':'#FF69B4','ORANGE':'#FFA500',
               'YELLOW':'#FFFF00','GREEN':'#00FF00','CYAN':'#00FFFF','BLUE':'#0000FF','PURPLE':'#800080','MAGENTA':'#FF00FF'}


def _standard_marker_color(color: str) -> str:
    rgb=tuple(int(color[i:i+2],16) for i in (1,3,5))
    return min(MARKER_COLORS,key=lambda name:sum((a-b)**2 for a,b in zip(rgb,tuple(int(MARKER_COLORS[name][i:i+2],16) for i in (1,3,5)))))


def _native_marker(marker: Any,time: int) -> Marker:
    data=_json(marker.metadata.get(META,{})).get('marker',{})
    return Marker.model_validate({'color':MARKER_COLORS.get(marker.color,'#F5B700'),**data,'t':time,'label':marker.name[:120]})


def _json(value: Any) -> Any:
    # OTIO wraps dictionaries/lists in its C++ containers.
    return json.loads(json.dumps(value, default=lambda x: dict(x) if hasattr(x, 'keys') else list(x)))


def _time(ms: float, rate: float) -> Any:
    return O.RationalTime(ms * rate / 1000, rate)


def _range(start: float, duration: float, rate: float) -> Any:
    return O.TimeRange(_time(start, rate), _time(duration, rate))


def _ms(time: Any) -> float:
    seconds = time.to_seconds()
    if not math.isfinite(seconds) or not math.isfinite(time.rate) or time.rate <= 0:
        raise LumiereError('Invalid OTIO time or rate.', code='bad_request')
    return seconds * 1000


def _report(report: list, status: str, item: str, message: str) -> None:
    report.append({'status': status, 'item': item, 'message': message})


def _foreign_metadata(report: list, node: Any, item: str) -> None:
    for namespace in node.metadata:
        if namespace != META:
            own=_json(node.metadata.get(META,{}))
            foreign=_json(node.metadata[namespace])
            if namespace=='filmcraft' and own.get('filmcraft_sound_adapter')==1 and isinstance(foreign,dict) and set(foreign)<={'gain_db','volume_db'}:
                continue
            _report(report, 'unsupported', item,
                    f'External metadata namespace {namespace} is not fully mapped; unmapped appearance/audio/settings may differ. Keep the source OTIO file.')


def _filmcraft_framing(c: Clip, node: Any, info: dict, canvas: Canvas, report: list) -> None:
    """Map verified static motion fields; animated/anisotropic framing stays a gap."""
    data = _json(node.metadata.get('filmcraft', {}))
    effects = [e for e in data.get('effects', []) if e.get('effect') == 'motion' and e.get('enabled', True)]
    if len(effects) != 1 or data.get('scale_to_frame', False):
        return
    params = effects[0].get('params', {})
    if any(set(p) != {'value'} for p in params.values()):
        return  # Keyframes/expressions must not be flattened to their initial value.
    def value(key, kind, default=None):
        return params.get(key, {}).get('value', {}).get(kind, default)
    anchor, position = value('anchor','Vec2'), value('position','Vec2')
    scale, rotation = value('scale','Float'), value('rotation','Float')
    if not isinstance(anchor,dict) or not isinstance(position,dict) or scale is None or rotation is None:
        return
    if anchor != {'x':info['width']/2, 'y':info['height']/2}:
        return  # Noncentral anchors need their own comparative rotation tests.
    if not value('uniform_scale','Bool',True) or value('anti_flicker','Float',0) != 0:
        return
    video = _json(node.media_reference.metadata.get('filmcraft', {})).get('info', {}).get('video', {})
    if video.get('par', [1,1]) != [1,1]:
        return
    candidate={'fit':'none','scale':scale/100,'rotation':rotation,
               'x':(position['x']-canvas.width/2)/canvas.width,
               'y':(position['y']-canvas.height/2)/canvas.height}
    try:
        c.transform=Transform.model_validate(candidate)
    except ValueError:
        return
    _report(report,'approximated',node.name,'Static FilmCraft centered-anchor position, uniform scale and rotation mapped; color/effect rendering remains editor-dependent.')


def _signature(node: Any, position: float, lead: float, tail: float) -> dict:
    ref = node.media_reference
    parent = node.parent()
    index = parent.index(node) if isinstance(parent,S.Track) else 0
    previous = parent[index-1] if index else None
    return {'position': position, 'start': _ms(node.trimmed_range().start_time),
            'duration': _ms(node.duration()), 'lead': lead, 'tail': tail,
            'url': ref.target_url if isinstance(ref, S.ExternalReference) else '',
            'generator': _json(ref.parameters) if isinstance(ref,S.GeneratorReference) else {},
            'enabled': node.enabled,
            'transition': previous.transition_type if isinstance(previous,S.Transition) else '',
            'foreign_gain': _json(node.metadata.get('filmcraft',{})).get('gain_db'),
            'effects': otio.adapters.write_to_string(S.SerializableCollection(children=list(node.effects)), adapter_name='otio_json')}


def export_project(svc: 'Services', project_id: str) -> dict:
    p = projects.doc(svc, project_id)
    if p.sequence_ids():
        raise LumiereError('OTIO export of nested sequences is not yet supported. Unnest them in a copy first.', code='unsupported_interchange')
    issues = validate(p, projects.media_lookup(svc))
    if any(i['level'] == 'error' for i in issues):
        raise LumiereError('Fix the timeline errors before exporting OTIO.', code='invalid_timeline')
    report: list[dict] = []
    timeline = S.Timeline(name=projects.view(svc, project_id)['name'])
    settings = p.dump()
    settings.pop('tracks')
    settings.pop('markers')
    # Multicam requires its other angle media as well, not just visible clips.
    refs = {mid: media.get(svc, mid)['path'] for mid in p.media_ids() | {a.media for g in p.multicams for a in g.angles}}
    timeline.metadata[META] = {'version': 1, 'settings': settings, 'media': refs}
    rate = p.canvas.fps
    sound_files=set()
    timeline.global_start_time = _time(0, rate)
    for track in p.tracks:
        native_track = track.model_dump(exclude={'clips'})
        out = S.Track(name=track.name, kind=S.TrackKind.Audio if track.kind == 'audio' else S.TrackKind.Video)
        out.metadata[META] = {'version': 1, 'track': native_track}
        if track.kind=='audio':
            out.metadata['filmcraft']={'volume_db':track.volume_db}
            out.metadata[META]['filmcraft_sound_adapter']=1
            out.metadata[META]['filmcraft_volume_at_export']=track.volume_db
        out.enabled = not (track.muted or track.hidden)
        if track.volume_db or track.duck:
            _report(report,'metadata_only',track.id,'Track volume and ducking remain Lumiere metadata.')
        clips = sorted(track.clips, key=lambda c: c.start)
        transitions: dict[int, tuple[int, int]] = {}
        for i, c in enumerate(clips):
            if i and c.transition_in and clips[i-1].end > c.start:
                overlap = clips[i-1].end - c.start
                transitions[i] = (overlap // 2, overlap - overlap // 2)
        cursor = 0.0
        for i, c in enumerate(clips):
            lead = transitions.get(i, (0, 0))[0]
            tail = transitions.get(i+1, (0, 0))[1]
            position, length = c.start + lead, c.duration - lead - tail
            if length <= 0 or position < cursor - 0.01:
                raise LumiereError('The transition handles cannot be represented in OTIO.', code='unsupported_interchange')
            if position > cursor + 0.01:
                out.append(S.Gap(source_range=_range(0, position-cursor, rate)))
            if i in transitions:
                a, b = transitions[i]
                out.append(S.Transition(name=c.transition_in.type, transition_type=S.TransitionTypes.SMPTE_Dissolve,
                                        in_offset=_time(a, rate), out_offset=_time(b, rate),
                                        metadata={META: {'type': c.transition_in.type}}))
                if c.transition_in.type not in ('crossfade', 'dissolve'):
                    _report(report, 'approximated', c.id, 'Other editors receive a dissolve; the original transition stays in Lumiere metadata.')
            if c.type == 'text':
                ref = S.GeneratorReference(name=c.text, generator_kind='LumiereText', parameters={'text': c.text})
                source_start = 0
                _report(report, 'metadata_only', c.id, 'Title text/style uses Lumiere generator metadata; other editors may not render it.')
            else:
                info = media.get(svc, c.media)
                ref = S.ExternalReference(target_url=Path(info['path']).resolve().as_uri(),
                                          available_range=_range(0, info.get('duration_ms') or c.src_out, rate))
                if track.kind=='audio' and info.get('has_audio'):
                    master=media.audio_master(svc,c.media,c.audio_stream)
                    sound_files.add(str(master))
                    ref.target_url=master.resolve().as_uri()
                source_start = c.src_at(c.start+lead)
            node = S.Clip(name=c.label or (c.text if c.type == 'text' else info['name']), media_reference=ref,
                          source_range=_range(source_start, length, rate))
            if c.speed != 1 and not c.speed_keys:
                node.effects.append(S.LinearTimeWarp(time_scalar=(-c.speed if c.reverse else c.speed)))
            elif c.reverse and not c.speed_keys:
                node.effects.append(S.LinearTimeWarp(time_scalar=-1))
            if c.filters or c.keyframes or c.mask or c.reframe or c.speed_keys or c.transform != Transform() or c.crop != Crop() or c.volume_db or c.mute or c.fade_in or c.fade_out or c.audio_fade_in or c.audio_fade_out:
                _report(report, 'metadata_only', c.id, 'Appearance/audio/keyframes or speed ramps remain editable in Lumiere metadata; external rendering is editor-dependent.')
            out.append(node)
            if track.kind=='audio':node.metadata['filmcraft']={'gain_db':c.volume_db}
            node.metadata[META] = {'version': 1, 'clip': c.model_dump(), 'name':node.name, 'signature': _signature(node, position, lead, tail)}
            if track.kind=='audio':
                node.metadata[META]['audio_original_source']=c.media
                node.metadata[META]['filmcraft_sound_adapter']=1
                node.metadata[META]['filmcraft_gain_at_export']=c.volume_db
            if c.transition_in and i not in transitions:
                _report(report,'metadata_only',c.id,'Non-overlapping incoming transition is retained in metadata.')
            cursor = position + length
        timeline.tracks.append(out)
    from .interchange_audio import derive_audio
    derived_audio=derive_audio(timeline,svc,report)
    for marker in p.markers:
        timeline.tracks.markers.append(S.Marker(name=marker.label,color=_standard_marker_color(marker.color),marked_range=_range(marker.t, 0, rate),
                                               metadata={META: {'marker': marker.model_dump()}}))
        _report(report,'metadata_only',marker.id,'Marker kind and exact hex color remain Lumiere metadata; standard OTIO carries its name, time and nearest named color.')
    if p.captions.enabled or p.multicams or p.notes or p.canvas.background != '#000000':
        _report(report, 'metadata_only', project_id, 'Captions, multicam groups, notes and canvas settings are Lumiere metadata, not generic OTIO rendering instructions.')
    return {'otio': otio.adapters.write_to_string(timeline, adapter_name='otio_json'), 'report': report,
            'tracks': len(p.tracks), 'clips': sum(len(t.clips) for t in p.tracks), 'fps': rate,
            'otio_tracks':len(timeline.tracks),'derived_audio_tracks':derived_audio['tracks'],'audio_sources':sorted(sound_files|set(derived_audio['files']))}


def _close(a: dict, b: dict) -> bool:
    if 'foreign_gain' not in a and b.get('foreign_gain') is None:
        a={**a,'foreign_gain':None}  # Earlier v1 snapshots had no target-editor gain field.
    if a.keys()!=b.keys():return False
    for key,value in b.items():
        if isinstance(value,(int,float)) and not isinstance(value,bool):
            if not isinstance(a[key],(int,float)) or isinstance(a[key],bool) or not math.isclose(a[key],value,rel_tol=0,abs_tol=.001):return False
        elif a[key]!=value:return False
    return True


def import_project(svc: 'Services', path: str, *, title: str = '', media_dirs: list[str] | None = None, actor: str = 'family') -> dict:
    text, where = _read_text(path, svc)
    try:
        timeline = otio.adapters.read_from_string(text, adapter_name='otio_json')
    except Exception as exc:
        raise LumiereError(f'Invalid OTIO document: {exc}', code='bad_request') from exc
    if not isinstance(timeline, S.Timeline):
        raise LumiereError('The OTIO root must be a Timeline.', code='bad_request')
    if timeline.tracks.source_range is not None:
        raise LumiereError('Trimmed top-level OTIO stacks are not yet supported.', code='unsupported_interchange')
    folders = [Path(d).expanduser().resolve() for d in (media_dirs or [])]
    for folder in folders:
        media._check_root(svc, folder)
        if not folder.is_dir():
            raise LumiereError(f'Media folder does not exist: {folder}')
    resolver = Resolver(svc, [(d,True) for d in folders] + [(where.parent,False)])
    native = _json(timeline.metadata.get(META, {}))
    native = native if native.get('version') == 1 else {}
    settings = native.get('settings', {})
    rate = timeline.global_start_time.rate if timeline.global_start_time else settings.get('canvas',{}).get('fps')
    if rate is None:
        rate = next((n.duration().rate for t in timeline.tracks if isinstance(t,S.Track) for n in t if isinstance(n,S.Clip)), 24)
    if not math.isfinite(rate) or not 1 <= rate <= 240:
        raise LumiereError('OTIO frame rate must be between 1 and 240.', code='bad_request')
    report: list[dict] = []
    from .interchange_audio import classify_audio,restore_derived_audio
    skip_audio,mute_video=classify_audio(timeline,native,report,svc,where.parent)
    _foreign_metadata(report,timeline,'timeline')
    for effect in timeline.tracks.effects:
        _report(report,'unsupported','timeline',f'Stack effect {effect.name or effect.schema_name()} is not applied.')
    if timeline.global_start_time and timeline.global_start_time.value:
        _report(report,'unsupported','timecode','Global starting timecode is not stored in the native project; clip positions remain relative to the timeline origin.')
    p = Project.model_validate({**settings, 'tracks': [], 'markers': []}) if settings else Project(canvas=Canvas(fps=rate), length_mode='longest')
    if not settings:
        film = _json(timeline.metadata.get('filmcraft', {})).get('settings', {})
        if 'width' in film and 'height' in film:
            # FilmCraft exports these concrete sequence settings in its namespace;
            # they do not establish mappings for its other proprietary effects.
            p.canvas = Canvas(width=film['width'],height=film['height'],fps=rate,
                              sample_rate=film.get('sample_rate',48000))
            _report(report,'metadata_only','canvas','FilmCraft sequence dimensions and sample rate imported; other proprietary settings/effects are not applied.')
        else:
            _report(report,'approximated','canvas','OTIO does not specify a standard canvas size; using the native 1920×1080 default.')
    p.canvas.fps = rate
    media_mapping: dict[str,str] = {}
    rounded = 0.0

    def clock(value: float) -> int:
        nonlocal rounded
        rounded = max(rounded, abs(value-round(value)))
        return int(round(value))

    count = 0
    for ti, source in enumerate(timeline.tracks):
        if ti in skip_audio:continue
        if not isinstance(source,S.Track) or source.source_range is not None:
            raise LumiereError('Nested or trimmed OTIO tracks are not yet supported.', code='unsupported_interchange')
        _foreign_metadata(report,source,source.name or f'track {ti}')
        source_meta = _json(source.metadata.get(META,{}))
        data = source_meta.get('track',{}) if source_meta.get('version') == 1 else {}
        kind = 'audio' if source.kind == S.TrackKind.Audio else 'video'
        native_kind = data.get('kind',kind)
        if (native_kind == 'audio') != (kind == 'audio'):
            native_kind = kind
        track = Track.model_validate({**data, 'name': source.name[:60], 'kind': native_kind, 'clips': []})
        foreign_track=_json(source.metadata.get('filmcraft',{}))
        foreign_volume=foreign_track.get('volume_db')
        if foreign_volume is not None and foreign_volume!=source_meta.get('filmcraft_volume_at_export'):
            if isinstance(foreign_volume,(int,float)) and math.isfinite(foreign_volume) and -60<=foreign_volume<=24:track.volume_db=foreign_volume
            else:_report(report,'unsupported',source.name,'FilmCraft track volume is outside the native supported range.')
        if source.enabled != (not (track.muted or track.hidden)):
            track.muted, track.hidden = not source.enabled, False
        if not data:
            track.role = 'main' if kind == 'video' and not any(t.kind == 'video' for t in p.tracks) else ('overlay' if kind == 'video' else 'music')
        for effect in source.effects:
            _report(report,'unsupported',source.name,f'Track effect {effect.name or effect.schema_name()} is not applied.')
        for marker in source.markers:
            p.markers.append(_native_marker(marker,clock(_ms(marker.marked_range.start_time))))
            if _ms(marker.marked_range.duration):
                _report(report,'approximated',marker.name,'Track marker range reduced to its start point.')
        for ni,node in enumerate(source):
            if isinstance(node,(S.Gap,S.Transition)):
                continue
            if not isinstance(node,S.Clip):
                raise LumiereError('Nested OTIO compositions are not yet supported.', code='unsupported_interchange')
            _foreign_metadata(report,node,node.name or f'clip {ni}')
            _foreign_metadata(report,node.media_reference,node.name or f'clip {ni}')
            count += 1
            if count > 3000:
                raise LumiereError('OTIO contains more than 3000 clips.', code='bad_request')
            if not node.enabled:
                _report(report,'omitted',node.name,'Disabled external clip retained as a gap.')
                continue
            lead = _ms(source[ni-1].in_offset) if ni and isinstance(source[ni-1],S.Transition) else 0
            tail = _ms(source[ni+1].out_offset) if ni+1 < len(source) and isinstance(source[ni+1],S.Transition) else 0
            position = _ms(source.range_of_child(node).start_time)
            signature = _signature(node,position,lead,tail)
            meta = _json(node.metadata.get(META,{}))
            unchanged = meta.get('version') == 1 and isinstance(meta.get('signature'),dict) and _close(meta['signature'],signature)
            if unchanged:
                c = Clip.model_validate(meta['clip'])
            else:
                start = clock(position-lead)
                if start < 0:
                    raise LumiereError('Transition extends before the timeline.', code='bad_request')
                speed, reverse = 1., False
                warps = [e for e in node.effects if isinstance(e,S.LinearTimeWarp) and not isinstance(e,S.FreezeFrame)]
                if len(warps) > 1:
                    raise LumiereError('Multiple stacked OTIO time warps cannot be mapped to one native speed.',code='unsupported_interchange')
                for effect in node.effects:
                    if isinstance(effect,S.LinearTimeWarp) and not isinstance(effect,S.FreezeFrame):
                        if effect.time_scalar == 0:
                            raise LumiereError('Zero-speed OTIO effect is unsupported.', code='unsupported_interchange')
                        speed, reverse = abs(effect.time_scalar), effect.time_scalar < 0
                    else:
                        _report(report,'unsupported',node.name,f'Effect {effect.name or effect.schema_name()} is not applied.')
                length = clock(signature['duration'] + lead + tail)
                source_in = clock(signature['start'] - lead*speed)
                if source_in < 0:
                    raise LumiereError('OTIO transition uses negative source handles.',code='unsupported_interchange')
                c = Clip(media='pending',start=start, src_in=source_in, src_out=source_in+clock(length*speed), speed=speed, reverse=reverse)
                # OTIO separates video and audio tracks. Native video clips can
                # carry sound, so enabling that here would double split audio.
                c.mute = track.kind == 'video'
                if reverse:
                    raise LumiereError('External reverse clips require explicit source-range mapping; import is not yet supported.', code='unsupported_interchange')
                if meta:
                    _report(report,'approximated',node.name,'External timing/effect edits take precedence; previous Lumiere appearance metadata was not reapplied.')
            if not unchanged or node.name != meta.get('name'):
                c.label = node.name[:120]
            if track.kind=='audio':c=restore_derived_audio(c,node)
            elif track.id in mute_video:c.mute=True
            if ni and isinstance(source[ni-1],S.Transition):
                trans = source[ni-1]
                duration = clock(_ms(trans.in_offset)+_ms(trans.out_offset))
                c.transition_in = Transition(type=c.transition_in.type if unchanged and c.transition_in else 'crossfade', dur=duration)
                if trans.transition_type != S.TransitionTypes.SMPTE_Dissolve:
                    _report(report,'approximated',node.name,'Transition is mapped to a crossfade.')
            elif c.transition_in and not unchanged:
                c.transition_in = None
            ref = node.media_reference
            if c.type == 'text' and unchanged and isinstance(ref,S.GeneratorReference) and ref.generator_kind == 'LumiereText':
                pass
            elif isinstance(ref,S.ExternalReference):
                original_audio=native.get('media',{}).get(c.media) if unchanged and meta.get('audio_original_source') else None
                if original_audio:
                    media._check_root(svc,Path(original_audio).resolve())
                    if not Path(original_audio).is_file():
                        original_audio=None
                        c.audio_stream=0
                        _report(report,'approximated',node.name,'Original audio source is offline; the normalized stereo reference is imported instead.')
                url = original_audio or url_to_path(ref.target_url)
                target = Path(url)
                if not target.is_absolute() and not (len(url)>2 and url[1]==':'):
                    target = where.parent / target
                info,reason = resolver.get(str(target))
                if info is None:
                    _report(report,'omitted',node.name,f'Media missing or unreadable; its timeline position is retained as a gap: {reason}')
                    continue
                if track.kind == 'video' and not info.get('has_video') and info.get('kind') != 'image':
                    raise LumiereError(f'{node.name} has no video for its track.',code='bad_request')
                if c.media:
                    media_mapping[c.media] = info['id']
                c.media = info['id']
                if track.kind=='audio' and not unchanged:
                    foreign=_json(node.metadata.get('filmcraft',{}))
                    gain=foreign.get('gain_db')
                    if gain is not None and gain!=meta.get('filmcraft_gain_at_export'):
                        if isinstance(gain,(int,float)) and math.isfinite(gain) and -60<=gain<=24:c.volume_db=gain
                        else:_report(report,'unsupported',node.name,'FilmCraft clip gain is outside the native supported range.')
                if not unchanged and track.kind == 'video':
                    _filmcraft_framing(c,node,info,p.canvas,report)
            else:
                _report(report,'omitted',node.name,'Missing or unsupported generator/media reference; timeline position retained as a gap.')
                continue
            track.clips.append(c)
            for marker in node.markers:
                at = c.start + (_ms(marker.marked_range.start_time) - c.src_in) / c.speed
                if at >= 0:
                    p.markers.append(_native_marker(marker,clock(at)))
                else:
                    _report(report,'omitted',marker.name,'Clip marker lies before the timeline.')
        p.tracks.append(track)
    for marker in timeline.tracks.markers:
        p.markers.append(_native_marker(marker,clock(_ms(marker.marked_range.start_time))))
        if _ms(marker.marked_range.duration):
            _report(report,'approximated',marker.name,'Marker range reduced to its start point.')
    # Relink angles even when the angle has no visible clip.
    for group in p.multicams:
        for angle in group.angles:
            if angle.media not in media_mapping:
                info,reason = resolver.get(native.get('media',{}).get(angle.media,''))
                if info is None:
                    raise LumiereError(f'Multicam angle could not be relinked: {reason}',code='media_missing')
                media_mapping[angle.media] = info['id']
            angle.media = media_mapping[angle.media]
    ids = [c.id for _,c in p.all_clips()] + [t.id for t in p.tracks]
    if len(ids) != len(set(ids)):
        raise LumiereError('Duplicate IDs in OTIO metadata.',code='bad_request')
    if not any(t.clips for t in p.tracks):
        raise LumiereError('No readable OTIO clips could be imported.',code='media_missing')
    p.sort()
    p = Project.model_validate(p.dump())
    issues = validate(p,projects.media_lookup(svc))
    if any(i['level'] == 'error' for i in issues):
        raise LumiereError(f'Imported OTIO timeline is invalid: {issues}',code='invalid_timeline')
    if rounded > .000001:
        _report(report,'approximated','clock',f'Fractional frame times rounded to the native millisecond clock; maximum error {rounded:.6f} ms.')
    created = projects.create(svc,title or timeline.name or 'Imported OTIO',width=p.canvas.width,height=p.canvas.height,fps=p.canvas.fps)
    try:
        projects.save(svc,created['id'],p,'Import OTIO',actor=actor)
    except Exception:
        projects.delete(svc,created['id'])
        raise
    return {'ok':True,'project_id':created['id'],'id':created['id'],'source':'otio',
            'clips':sum(len(t.clips) for t in p.tracks),'tracks':len(p.tracks),'report':report,
            'duration_ms':p.duration,'canvas':p.canvas.model_dump(),'issues':issues}


def export_file(svc: 'Services', project_id: str) -> dict:
    result = export_project(svc, project_id)
    path = svc.config.data_dir / 'interchange' / f'{project_id}.otio'
    atomic_write(path, result.pop('otio').encode('utf-8'))
    return {**result, 'path':str(path), 'url':f'{svc.base_url()}/api/projects/{project_id}/otio', 'format':'otio'}
