"""`python -m prosperos_hoard [--port] [--data-dir] [--demo] [--no-browser]`."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import socket
import sys
import webbrowser
from pathlib import Path

import uvicorn

from . import __version__
from .api import create_app

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
    if args.data_dir:
        data_dir = args.data_dir
    elif args.demo:
        data_dir = repo_root / "data-demo"
    else:
        import os

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

    if args.demo:
        import json

        from .devtools.fake_comfy import FakeComfyServer

        fake = FakeComfyServer(data_dir / "fake_comfy")
        fake_port = fake.run_in_thread()
        backend_json = data_dir / "backend.json"
        raw = {}
        if backend_json.is_file():
            try:
                raw = json.loads(backend_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
        raw["comfy"] = {"url": f"http://127.0.0.1:{fake_port}"}
        backend_json.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        print(f"[prosperos-hoard] demo mode: fake ComfyUI (procedural placeholder images) on 127.0.0.1:{fake_port}")

    static_dir = repo_root / "frontend" / "dist"
    app = create_app(data_dir, static_dir if static_dir.is_dir() else None, port=args.port, demo=args.demo)

    if args.demo:
        from .devtools.demo_seed import seed_demo_data

        store = app.state.store
        backend = app.state.backend
        if store.list_projects(limit=1)["items"] == []:
            print("[prosperos-hoard] seeding demo data...")
            seed_demo_data(store, backend)
            print("[prosperos-hoard] demo data ready")

    print(f"[prosperos-hoard] Prospero's Hoard v{__version__} starting on http://127.0.0.1:{args.port}")
    if not args.no_browser:
        try:
            webbrowser.open(f"http://127.0.0.1:{args.port}")
        except Exception:
            pass

    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        app.state.queue.stop()
        instance_lock.close()


if __name__ == "__main__":
    main()
