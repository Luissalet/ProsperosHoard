"""`python -m lumiere_hoard` — run the app with uvicorn on 127.0.0.1."""

from __future__ import annotations

import logging

import uvicorn

from .config import Config
from .main import create_app
from .port import can_listen, find_available_port


def _already_running(port: int) -> bool:
    try:
        import httpx

        response = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=3, trust_env=False)
        return response.status_code == 200 and response.json().get("service") == "lumiere-hoard"
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config = Config.from_env()
    if config.port_strict and not can_listen(config.port):
        # Decide before touching the data dir: a second instance must never
        # rotate anything under the feet of the one that is serving.
        if _already_running(config.port):
            print(f"Lumiere's Hoard is already running on http://127.0.0.1:{config.port}", flush=True)
            raise SystemExit(0)
        print(f"Port {config.port} is taken by another program (PORT_STRICT=1).", flush=True)
        raise SystemExit(1)
    port = config.port if config.port_strict else find_available_port(config.port)
    config.port = port
    app = create_app(config)
    print(f"Lumiere's Hoard listening on http://127.0.0.1:{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
