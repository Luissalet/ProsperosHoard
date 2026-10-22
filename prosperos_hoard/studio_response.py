"""Scope native HTML and JSON URLs; leave binary/Range/stream bodies untouched."""
import json
from html.parser import HTMLParser


def scoped(value, module, port):
    """One known server URL, never an arbitrary JSON string or disk path."""
    if isinstance(value, str):
        for origin in ('', f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            if value.startswith(tuple(origin + p for p in ('/api/', '/assets/', '/_hoard/', '/#/'))):
                return '/' + module + value[len(origin):]
    return value


URL_FIELDS = frozenset({
    'url', 'href', 'thumbnail', 'thumbnail_url', 'thumb_url', 'preview_url', 'proxy_url',
    'waveform_url', 'download_url', 'file_url', 'project_url', 'native_url',
    'render_url', 'media_receive_url', 'image_url', 'audio_url', 'video_url',
    'events_url', 'stream_url', 'source_url', 'poster_url',
})
OPAQUE_FIELDS = frozenset({'snapshot', 'snapshots', 'manifest', 'manifests', 'document', 'doc',
    'native_document', 'prompt', 'prompts', 'metadata', 'provenance', 'params', 'parameters',
    'arguments', 'recipe', 'settings'})
MEDIA_URL_FIELDS = frozenset({'play', 'file', 'poster', 'sprite', 'waveform'})


def scoped_json(value, module, port):
    """Rewrite declared HTTP-link fields while native snapshots/text stay exact."""
    if isinstance(value, list):
        return [scoped_json(item, module, port) for item in value]
    if isinstance(value, dict):
        return {key: item if key in OPAQUE_FIELDS else
            {name: scoped(link, module, port) if name in MEDIA_URL_FIELDS else link
                for name, link in item.items()} if key == 'urls' and isinstance(item, dict) else
            scoped(item, module, port) if key in URL_FIELDS and isinstance(item, str) else
            scoped_json(item, module, port) for key, item in value.items()}
    return value


class HTML(HTMLParser):
    def __init__(self, module, port):
        super().__init__(convert_charrefs=False)
        self.module, self.port, self.parts = module, port, []

    def handle_starttag(self, tag, attrs):
        # Preserve original syntax/inline scripts, changing only URL attributes.
        raw = self.get_starttag_text()
        import re
        def replace(match):
            value = match.group(3)
            rewritten = scoped(value, self.module, self.port)
            return match.group(1) + match.group(2) + rewritten + match.group(2)
        raw = re.sub(r'((?:src|href|action)\s*=\s*)(["\'])(.*?)\2', replace, raw, flags=re.I)
        self.parts.append(raw)
        if tag == 'head':
            self.parts.append('<link rel="stylesheet" href="/studio-assets/studio.css">'
                f'<script data-module="{self.module}" src="/studio-assets/native.js"></script>')
        if tag == 'body' and self.module != 'generation':
            self.parts.append('<nav class="studio-owner" aria-label="Prospero"><a href="/">'
                '<img src="/studio-assets/app-icon.png" alt="">Prospero</a>'
                '<a href="/generation/">Generación</a><a href="/editing/">Montaje</a>'
                '<a href="/">Proyectos y módulos</a></nav>')

    def handle_endtag(self, tag): self.parts.append(f'</{tag}>')
    def handle_startendtag(self, tag, attrs): self.handle_starttag(tag, attrs)
    def handle_data(self, data): self.parts.append(data)
    def handle_comment(self, data): self.parts.append('<!--' + data + '-->')
    def handle_decl(self, decl): self.parts.append('<!' + decl + '>')
    def handle_entityref(self, name): self.parts.append('&' + name + ';')
    def handle_charref(self, name): self.parts.append('&#' + name + ';')


class NativeResponse:
    def __init__(self, send, module, port):
        self.send, self.module, self.port = send, module, port
        self.start, self.kind, self.parts = None, None, []

    async def __call__(self, message):
        if message['type'] == 'http.response.start':
            headers = dict(message.get('headers', []))
            kind = headers.get(b'content-type', b'').split(b';')[0]
            if kind in (b'text/html', b'application/json'):
                self.start, self.kind = message, kind
                return
            # Redirects must also stay within their native namespace.
            message = dict(message, headers=[(k, scoped(v.decode('latin-1'), self.module, self.port).encode('latin-1'))
                if k == b'location' else (k, v) for k, v in message.get('headers', [])])
        if self.start is not None and message['type'] == 'http.response.body':
            self.parts.append(message.get('body', b''))
            if message.get('more_body'):
                return
            raw = b''.join(self.parts)
            if self.kind == b'application/json':
                raw = json.dumps(scoped_json(json.loads(raw), self.module, self.port), ensure_ascii=False).encode()
            else:
                parser = HTML(self.module, self.port)
                parser.feed(raw.decode('utf-8'))
                parser.close()
                raw = ''.join(parser.parts).encode()
            start, self.start = self.start, None
            start = dict(start, headers=[(k, v) for k, v in start['headers'] if k not in (b'content-length', b'etag')])
            await self.send(start)
            return await self.send({'type': 'http.response.body', 'body': raw})
        await self.send(message)
