"""`python -m prosperos_hoard [--port] [--data-dir] [--demo] [--no-browser]`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import logging
import logging.handlers
import socket
import sys
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


def _acquire_instance_lock(data_dir: Path):
    """An exclusive lock on data/.instance.lock for the life of the process.
    A second instance on the same data folder would otherwise requeue the
    first one's running jobs and claim queued ones before its port bind
    failed, leaving them 'running' forever. Returns the open handle (keep a
    reference) or None when another instance holds the lock."""
    fh = (data_dir / ".instance.lock").open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if sys.platform != "win32":  # as uvicorn binds: a lingering closed connection is not "in use"
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


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

    instance_lock = _acquire_instance_lock(data_dir)
    if instance_lock is None:
        print(f"[prosperos-hoard] another Prospero's Hoard is already running on {data_dir}; "
              f"open http://127.0.0.1:{args.port} or stop it first.", file=sys.stderr)
        raise SystemExit(1)
    if not _port_free(args.port):
        print(f"[prosperos-hoard] port {args.port} is already in use; pick another with --port.", file=sys.stderr)
        raise SystemExit(1)

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

    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        instance_lock.close()


if __name__ == "__main__":
    main()
