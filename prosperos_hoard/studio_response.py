"""Scope native HTML and JSON URLs; leave binary/Range/stream bodies untouched."""
import json
from html.parser import HTMLParser


def scoped(value, module, port):
    if isinstance(value, list):
        return [scoped(v, module, port) for v in value]
    if isinstance(value, dict):
        return {k: scoped(v, module, port) for k, v in value.items()}
    if isinstance(value, str):
        for origin in ('', f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            if value.startswith(tuple(origin + p for p in ('/api/', '/assets/', '/_hoard/', '/#/'))):
                return '/' + module + value[len(origin):]
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
        if tag == 'body':
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
                raw = json.dumps(scoped(json.loads(raw), self.module, self.port), ensure_ascii=False).encode()
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
