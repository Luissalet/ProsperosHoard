"""Audio decoding for the few forge helpers that accept a file path: ffmpeg to float32 at 48 kHz."""
import os
import subprocess

import numpy as np

SR = 48000
AUDIO_EXTS = {'.wav', '.flac', '.ogg', '.oga', '.opus', '.mp3', '.m4a', '.aac', '.aif', '.aiff'}
FFMPEG = os.environ.get('FOLEY_FFMPEG', 'ffmpeg')
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def load(path, sr=SR, mono=False, start=None, end=None):
    """(n, 2) float32 at sr (a mono file duplicated at unity gain), or (n,) with mono=True"""
    cmd = [FFMPEG, '-v', 'error', '-nostdin']
    if start:
        cmd += ['-ss', str(start)]
    if end is not None:
        cmd += ['-to', str(end)]
    cmd += ['-i', str(path), '-vn', '-f', 'f32le', '-acodec', 'pcm_f32le', '-ac', '1', '-ar', str(sr), '-']
    done = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
    if done.returncode != 0 or not done.stdout:
        raise RuntimeError(f'cannot decode {path}: {done.stderr.decode(errors="replace")[-200:]}')
    x = np.frombuffer(done.stdout, np.float32).copy()
    return x if mono else np.repeat(x[:, None], 2, axis=1)
