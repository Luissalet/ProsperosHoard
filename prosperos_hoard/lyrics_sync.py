"""Match supplied text to heard words, or retain explicit human timings."""
from __future__ import annotations

from difflib import SequenceMatcher
import math
from pathlib import Path
import re
from typing import Any
import unicodedata


def text_lines(text: str) -> list[dict[str, Any]]:
    lines = []
    section = None
    for raw in text.replace('\r\n', '\n').splitlines():
        line = re.sub(r'^(?:\[\d+:\d+(?:[.:]\d+)?\])+', '', raw).strip()
        if not line:
            continue
        heading = re.fullmatch(r'\[([^\]]+)\]', line)
        if heading:
            section = heading.group(1).strip()
        else:
            lines.append({'text': line, 'section': section})
    return lines


def tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize('NFKC', text).casefold().replace('’', "'")
    return [word.replace("'", '') for word in re.findall(r"[^\W_]+(?:'[^\W_]+)*", normalized)]


def normalize_timings(text: str, rows: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    """Keep every supplied line, including lines not timed yet."""
    original = text_lines(text)
    if len(rows) != len(original):
        raise ValueError('The timing rows must contain every line of the supplied text, in order.')
    previous_end = 0.0
    result = []
    for index, (source, row) in enumerate(zip(original, rows)):
        if str(row.get('text', '')).strip() != source['text']:
            raise ValueError(f'Line {index + 1} no longer matches the supplied text; reload the text before timing it.')
        start, end = row.get('time_s', row.get('start_s')), row.get('end_s')
        if start is None and end is None:
            result.append({**source, 'time_s': None, 'end_s': None})
            continue
        try:
            start, end = float(start), float(end)
        except (TypeError, ValueError):
            raise ValueError(f'Line {index + 1} needs both a start and an end, or both empty.') from None
        if not all(math.isfinite(value) for value in (start, end)) or not 0 <= start < end <= duration + .02:
            raise ValueError(f'Line {index + 1} must start before it ends, inside the audio duration.')
        if start < previous_end - .02:
            raise ValueError(f'Line {index + 1} overlaps the previous timed line.')
        start, end = round(start, 3), round(min(end, duration), 3)
        if end <= start:
            raise ValueError(f'Line {index + 1} must span at least one millisecond.')
        previous_end = end
        result.append({**source, 'time_s': start, 'end_s': end,
                       **({'needs_review': True} if row.get('needs_review') else {})})
    return result


def _word_matches(wanted: list[str], heard: list[str]) -> dict[int, int]:
    # Global edit alignment keeps an imperfect first chorus before a cleaner
    # repeat; choosing the longest matching block can skip the entire verse.
    if len(wanted) * len(heard) > 2_000_000:
        return {block.a + offset: block.b + offset
                for block in SequenceMatcher(None, wanted, heard, autojunk=False).get_matching_blocks()
                for offset in range(block.size)}
    previous = list(range(len(heard) + 1))
    trace = [bytearray(len(heard) + 1)]
    for i, word in enumerate(wanted, 1):
        current, directions = [i], bytearray(len(heard) + 1)
        for j, other in enumerate(heard, 1):
            choices = (previous[j - 1] + (word != other), previous[j] + 1, current[j - 1] + 1)
            direction = min(range(3), key=choices.__getitem__)
            current.append(choices[direction])
            directions[j] = direction
        trace.append(directions)
        previous = current
    matches, i, j = {}, len(wanted), len(heard)
    while i and j:
        direction = trace[i][j]
        if direction == 0:
            if wanted[i - 1] == heard[j - 1]:
                matches[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif direction == 1:
            i -= 1
        else:
            j -= 1
    return matches


def align_words(text: str, transcript: dict[str, Any], duration: float) -> dict[str, Any]:
    """Match monotonically; missing words remain visible and untimed."""
    lines = text_lines(text)
    wanted, line_tokens = [], []
    for line in lines:
        indices = list(range(len(wanted), len(wanted) + len(tokens(line['text']))))
        wanted.extend(tokens(line['text']))
        line_tokens.append(indices)
    heard, clock = [], []
    for segment in transcript.get('segments') or []:
        for word in segment.get('words') or []:
            try:
                start = float(word.get('start_s', word.get('start')))
                end = float(word.get('end_s', word.get('end')))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(start + end) or not 0 <= start < end <= duration + .05:
                continue
            for token in tokens(str(word.get('word') or word.get('text') or '')):
                heard.append(token)
                clock.append((start, min(end, duration)))
    matches = _word_matches(wanted, heard)
    rows = []
    previous_end = 0.0
    for line, indices in zip(lines, line_tokens):
        found = [index for index in indices if index in matches]
        ratio = len(found) / len(indices) if indices else 0.0
        start = end = None
        if found and ratio >= .65 and found[0] - indices[0] <= 2 and indices[-1] - found[-1] <= 2:
            candidate_start = clock[matches[found[0]]][0]
            candidate_end = clock[matches[found[-1]]][1]
            gaps = [clock[matches[b]][0] - clock[matches[a]][1] for a, b in zip(found, found[1:])]
            # A few matching words across different occurrences are not a line.
            # Leave ambiguous long spans available for explicit human timing.
            coherent = max(gaps, default=0) <= 5 and candidate_end - candidate_start <= max(12, len(indices) * 2)
            if coherent and candidate_start >= previous_end - .02 and candidate_end > candidate_start:
                start, end = round(candidate_start, 3), round(candidate_end, 3)
                previous_end = end
        rows.append({**line, 'time_s': start, 'end_s': end, 'match_ratio': round(ratio, 3),
                     'needs_review': start is None or ratio < 1})
    timed = sum(row['time_s'] is not None for row in rows)
    return {'lines': rows, 'timed_lines': timed, 'total_lines': len(rows),
            'complete': bool(rows) and timed == len(rows), 'method': 'heard_words',
            'model': transcript.get('model'), 'device': transcript.get('device'),
            'note': 'Matched to words heard in the audio. Review uncertain lines; unrecognized lines have no invented times.'}


def as_lrc(rows: list[dict[str, Any]]) -> str:
    output, section = [], None
    for row in rows:
        if row.get('time_s') is None:
            continue
        time = float(row['time_s'])
        stamp = f'[{int(time // 60):02d}:{time % 60:05.2f}]'
        if row.get('section') and row['section'] != section:
            section = row['section']
            output.append(f'{stamp}[{section}]')
        output.append(stamp + row['text'])
    return '\n'.join(output)


def transcribe_words(path: Path, *, language: str | None = None, model: str = 'large-v3-turbo') -> dict[str, Any]:
    """Reuse the shared ASR implementation; never starts another Hoard or a text model."""
    from .hoard_link.media import stt
    from . import audio
    from huggingface_hub.constants import HF_HUB_CACHE

    cache = Path(HF_HUB_CACHE)
    # The shared loader delegates to faster-whisper, whose turbo repository differs
    # from the older Systran naming convention used by model_present().
    turbo_cached = model in ('turbo', 'large-v3-turbo') and any(
        (cache / 'models--mobiuslabsgmbh--faster-whisper-large-v3-turbo').glob('snapshots/*/model.bin'))
    model_dir = cache if turbo_cached or stt.model_present(cache, model) else stt.default_models_dir()
    transcriber = stt.Transcriber(models_dir=model_dir, size=model, device='cpu', owner='prospero')
    try:
        duration = audio.probe_duration_s(path)
        samples = audio.decode_to_mono(path, sr=16000, max_duration_s=max(1200, (duration or 0) + 1))
        # Singing and repeated choruses must survive the speech/silence cleanup.
        return transcriber.transcribe(samples, language=language if language not in ('auto', '') else None,
                                      word_timestamps=True, vad=False, clean=False).as_dict()
    finally:
        transcriber.close()


def time_asset(store: Any, project_id: str, song_asset_id: str, text: str, *,
               method: str = 'align', lines: list[dict[str, Any]] | None = None,
               preview: bool = False, name: str | None = None, language: str | None = None,
               model: str = 'large-v3-turbo') -> dict[str, Any]:
    """The same timing operation for the editor, HTTP and MCP."""
    from . import audio, engine

    store.get_project(project_id)
    song = store.get_asset(song_asset_id)
    if song['kind'] != 'audio':
        raise engine.EngineError('not_audio', 'The selected asset is not audio.')
    if not isinstance(text, str) or not text_lines(text) or len(text.encode('utf-8')) > engine.MAX_LYRICS_BYTES:
        raise engine.EngineError('bad_lyrics', 'Supply nonempty lyrics or script text, at most 512 KB.')
    path = store.data_dir / song['file_path']
    duration = float(song.get('duration_s') or audio.probe_duration_s(path) or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise engine.EngineError('bad_audio', 'The audio duration could not be read.')
    if method == 'align':
        result = align_words(text, transcribe_words(path, language=language, model=model), duration)
    elif method == 'manual':
        try:
            rows = normalize_timings(text, lines or [], duration)
        except ValueError as exc:
            raise engine.EngineError('bad_timing', str(exc)) from None
        count = sum(row['time_s'] is not None for row in rows)
        result = {'lines': rows, 'timed_lines': count, 'total_lines': len(rows),
                  'complete': count == len(rows), 'method': 'manual', 'note': 'Explicit line starts and ends supplied by the editor.'}
    else:
        raise engine.EngineError('bad_timing_method', 'Use align or manual; estimate remains available through time_lyrics.')
    result.update({'song_asset_id': song_asset_id, 'duration_s': duration})
    if preview:
        return {**result, 'preview': True}
    return save_asset(store, project_id, text, result, name=name)


def save_asset(store: Any, project_id: str, text: str, result: dict[str, Any], *, name: str | None = None,
               production_before: dict[str, Any] | None = None) -> dict[str, Any]:
    """Retain partial rows and exact ends alongside a portable LRC export."""
    import hashlib
    from . import engine

    if not result['timed_lines']:
        raise engine.EngineError('no_timing', 'No lines have times yet. Mark them by ear or try another transcription model.')
    result = {**result, 'preview': False}
    lrc = as_lrc(result['lines'])
    asset = engine.create_lyrics(store, project_id, lrc, name or 'Linked lyrics', recipe={
        'operation': 'time_lyrics', 'backend': 'local', 'method': result['method'],
        'input_asset_ids': [result['song_asset_id']], 'derived_from': result['song_asset_id'],
        **({'production_timing_before': production_before} if production_before else {}),
        'text_timing': {**result, 'original_text': text,
                        'lrc_sha256': hashlib.sha256(lrc.encode('utf-8')).hexdigest()}})
    return {**result, 'id': asset['id'], 'preview': False}


def stored_timing(asset: dict[str, Any], lrc: str) -> dict[str, Any] | None:
    """An LRC edited separately must not retain stale explicit timings."""
    import hashlib

    timing = (asset.get('recipe') or {}).get('text_timing')
    if not isinstance(timing, dict) or timing.get('lrc_sha256') != hashlib.sha256(lrc.encode('utf-8')).hexdigest():
        return None
    return timing


def production_timing(store: Any, slug: str, *, method: str = 'align', lines: list[dict[str, Any]] | None = None,
                      preview: bool = False, song_asset_id: str | None = None, text: str | None = None,
                      language: str | None = None, model: str = 'large-v3-turbo', capture_undo: bool = False) -> dict[str, Any]:
    """Listen outside the production lock; bind only to unchanged source text/audio."""
    from . import productions as prod

    with prod.lock_for(slug):
        state = prod.load_state(store.data_dir, slug)
        current_id = ((state.get('done') or {}).get('song') or {}).get('song_asset_id')
        current_text = str(((state.get('spec') or {}).get('song') or {}).get('lyrics') or '')
        if prod.is_running(state, store.data_dir):
            raise prod.ProductionError('production_running', 'Pause the production before editing its lyric timing.')
        if not current_id:
            raise prod.ProductionError('no_song_yet', 'Choose the audio before linking its lyrics.')
        if (song_asset_id is not None and song_asset_id != current_id) or (text is not None and text != current_text):
            raise prod.ProductionError('timing_source_changed', 'The text or audio changed. Reopen the timing editor.')
        project_id = state['project_id']
    result = time_asset(store, project_id, current_id, current_text, method=method, lines=lines,
                        preview=True, language=language, model=model)
    if preview:
        return {**result, 'slug': slug, 'text': current_text}
    with prod.lock_for(slug):
        state = prod.load_state(store.data_dir, slug)
        latest_id = ((state.get('done') or {}).get('song') or {}).get('song_asset_id')
        latest_text = str(((state.get('spec') or {}).get('song') or {}).get('lyrics') or '')
        if latest_id != current_id or latest_text != current_text or prod.is_running(state, store.data_dir):
            raise prod.ProductionError('timing_source_changed', 'The text, audio or production changed during alignment. Reopen the editor.')
        before = None
        if capture_undo and result['timed_lines']:
            from .agent_undo import capture_production_state
            before = capture_production_state(store, state)
        result = save_asset(store, project_id, current_text, result, name=f"{state.get('name') or slug} - linked lyrics",
                            production_before=before)
        state.setdefault('done', {})['lyrics'] = {'lyrics_asset_id': result['id'], 'source': result['method'],
            'lines': result['timed_lines'], 'total_lines': result['total_lines'], 'complete': result['complete']}
        for stage in ('animatic', 'timeline', 'report'):
            state['done'].pop(stage, None)
            state.get('partial', {}).pop(stage, None)
        state['review'] = {}
        prod.log(state, 'lyrics', 'linked_lyrics', asset_id=result['id'], lines=result['timed_lines'])
        prod.save_state(store.data_dir, state)
    return {**result, 'slug': slug}
