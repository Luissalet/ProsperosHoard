"""`python -m prosperos_hoard [--port] [--data-dir] [--demo] [--no-browser]`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import logging
import logging.handlers
import webbrowser
from pathlib import Path

import uvicorn

from . import __version__
from .hoard_link.atomic import write_text_atomic
from .hoard_link.appconfig import load_dotenv
from .studio import create_app
from .resources import installed

DEFAULT_PORT = 8815


def _setup_logging(data_dir: Path) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(log_dir / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("prosperos_hoard")
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def main() -> None:
    parser = argparse.ArgumentParser(prog="prosperos_hoard")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--demo", action="store_true", help="use ./data-demo, seeded with synthetic demo data and a fake ComfyUI")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    if installed():
        # Installed files are immutable code/resources, never the user's profile.
        repo_root = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local') / 'ProsperosHoard'
    # Settings kept out of git (engine and model folders such as PROSPERO_AUK_HOME); the real environment wins.
    configuration_root = repo_root
    if installed() and not args.demo:
        selected = args.data_dir or os.environ.get('PROSPERO_DATA_DIR')
        if selected:
            configuration_root = Path(selected).expanduser().resolve().parent
    load_dotenv(configuration_root / ".env")
    if args.data_dir:
        data_dir = args.data_dir
    elif args.demo:
        data_dir = repo_root / "data-demo"
    else:
        data_dir = Path(os.environ.get("PROSPERO_DATA_DIR") or repo_root / "data")
    data_dir = data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    _setup_logging(data_dir)

    app = create_app(data_dir, port=args.port, demo=args.demo)

    if args.demo:
        from .devtools.demo_seed import seed_demo_data

        # Seed only on explicit activation; the owner starts with both modules OFF.
        original = app.studio.modules['generation'].factory
        async def demo_factory(*values):
            import json
            from .devtools.fake_comfy import FakeComfyServer
            fake = FakeComfyServer(data_dir / 'fake_comfy')
            fake.motion_models = os.environ.get('PROSPERO_DEMO_MOTION','').strip() == '1'
            port = await asyncio.to_thread(fake.run_in_thread)
            path = data_dir / 'backend.json'
            try:
                config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
                config['comfy'] = {'url':f'http://127.0.0.1:{port}'}
                config.setdefault('training',{'lora_dir':str(fake.lora_dir),'trainers':[{'kind':'fake','name':'demo trainer','delay':.01}]})
                fake.lora_dir.mkdir(parents=True,exist_ok=True)
                write_text_atomic(path,json.dumps(config,indent=2))
                native = await original(*values)
                if not native.state.store.list_projects(limit=1)['items']:
                    await asyncio.to_thread(seed_demo_data,native.state.store,native.state.backend)
                lifespan = native.router.lifespan_context
                @contextlib.asynccontextmanager
                async def demo_lifespan(child):
                    try:
                        async with lifespan(child):
                            yield
                    finally:
                        await asyncio.to_thread(fake.stop)
                        if fake._thread:
                            await asyncio.to_thread(fake._thread.join)
                native.router.lifespan_context = demo_lifespan
                return native
            except BaseException:
                await asyncio.to_thread(fake.stop)
                if fake._thread:
                    await asyncio.to_thread(fake._thread.join)
                raise
        app.studio.modules['generation'].factory = demo_factory

    print(f"[prosperos-hoard] Prospero's Hoard v{__version__} starting on http://127.0.0.1:{args.port}")
    if not args.no_browser:
        try:
            webbrowser.open(f"http://127.0.0.1:{args.port}")
        except Exception:
            pass

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
