"""Minimal WAV reader and writer (PCM 16/24/32 and float 32/64), a drop-in for the two soundfile calls the sound
code makes: read(path, always_2d, dtype) -> (data, rate) and write(path, data, rate, subtype)."""
import struct

import numpy as np


def read(path, always_2d=False, dtype='float64', **_):
    with open(path, 'rb') as fh:
        raw = fh.read()
    if raw[:4] != b'RIFF' or raw[8:12] != b'WAVE':
        raise ValueError(f'{path}: not a WAV file')
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        tag, size = raw[pos:pos + 4], struct.unpack('<I', raw[pos + 4:pos + 8])[0]
        body = raw[pos + 8:pos + 8 + size]
        if tag == b'fmt ':
            fmt = struct.unpack('<HHIIHH', body[:16])
            if fmt[0] == 0xFFFE and len(body) >= 26:
                fmt = (struct.unpack('<H', body[24:26])[0],) + fmt[1:]
        elif tag == b'data':
            data = body
            break
        pos += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise ValueError(f'{path}: no fmt or data chunk')
    code, ch, rate, _, _, bits = fmt
    if code == 3:
        x = np.frombuffer(data, '<f4' if bits == 32 else '<f8')
    elif code == 1 and bits == 16:
        x = np.frombuffer(data, '<i2') / 32768.0
    elif code == 1 and bits == 24:
        b = np.frombuffer(data[:len(data) // 3 * 3], np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        x = np.where(v >= 1 << 23, v - (1 << 24), v) / 8388608.0
    elif code == 1 and bits == 32:
        x = np.frombuffer(data, '<i4') / 2147483648.0
    else:
        raise ValueError(f'{path}: unsupported WAV format {code}/{bits}')
    x = x[:len(x) // ch * ch].reshape(-1, ch).astype(dtype)
    return (x if always_2d or ch > 1 else x[:, 0]), rate


def write(path, data, rate, subtype='PCM_24'):
    x = np.asarray(data)
    x = x[:, None] if x.ndim == 1 else x
    ch = x.shape[1]
    if subtype == 'FLOAT':
        body, code, bits = np.ascontiguousarray(x, '<f4').tobytes(), 3, 32
    elif subtype == 'PCM_16':
        body, code, bits = np.ascontiguousarray(np.round(np.clip(x, -1, 1) * 32767), '<i2').tobytes(), 1, 16
    else:
        v = np.round(np.clip(x, -1, 1) * 8388607).astype(np.int32).reshape(-1)
        body, code, bits = np.stack([v & 255, (v >> 8) & 255, (v >> 16) & 255], 1).astype(np.uint8).tobytes(), 1, 24
    block = ch * bits // 8
    head = b'fmt ' + struct.pack('<IHHIIHH', 16, code, ch, rate, rate * block, block, bits)
    with open(path, 'wb') as fh:
        fh.write(b'RIFF' + struct.pack('<I', 4 + len(head) + 8 + len(body)) + b'WAVE' + head + b'data' + struct.pack('<I', len(body)) + body)
