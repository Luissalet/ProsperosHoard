"""Real mounted ASGI assets work for packaged UIs and legacy owner URLs."""
import asyncio

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from prosperos_hoard.studio import create_app


def test_nested_static_mount_and_url_for_preserve_native_prefix(tmp_path):
    assets = tmp_path / 'built-assets'
    assets.mkdir()
    (assets / 'entry.js').write_bytes(b'window.syntheticPackageAsset=true;')

    async def factory(*args):
        native = FastAPI()
        native.mount('/assets', StaticFiles(directory=assets), name='built-assets')
        @native.get('/')
        def page(request: Request):
            return HTMLResponse('<html><head><script src="' + str(request.url_for('built-assets', path='entry.js')) + '"></script></head><body></body></html>')
        return native

    async def scenario():
        app = create_app(tmp_path / 'owner', port=64839, factories={'generation':factory})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1:64839') as client:
            try:
                enabled = await client.put('/api/studio/modules/generation',json={'enabled':True})
                assert enabled.status_code == 200
                page = await client.get('/generation/')
                assert 'http://127.0.0.1:64839/generation/assets/entry.js' in page.text
                for url in ('/generation/assets/entry.js', '/assets/entry.js'):
                    response = await client.get(url)
                    assert response.status_code == 200, (url,response.text)
                    assert response.content == b'window.syntheticPackageAsset=true;'
            finally:
                await app.studio.close()
    asyncio.run(scenario())
