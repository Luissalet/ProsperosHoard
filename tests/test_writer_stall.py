"""The music-video planner's chat call must not hang when the language model stalls."""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from prosperos_hoard import mv_planner


class _StallServer(ThreadingHTTPServer):
    """A model server whose handlers can all be told to stop. A handler that is still sleeping or streaming when its client
    has given up would otherwise outlive the test and print a BrokenPipe traceback to stderr from a daemon thread -
    during interpreter shutdown that aborts the whole run on Windows."""

    daemon_threads = False  # server_close() joins every handler thread
    block_on_close = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stopping = threading.Event()

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], OSError):  # a client that gave up is the point of these tests: say nothing
            super().handle_error(request, client_address)

    def finish(self) -> None:
        self.stopping.set()  # wake the handlers (they wait on it instead of sleeping), then drop the listener and join them
        self.shutdown()
        self.server_close()


def _server(behaviour):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                behaviour(self)
            except OSError:  # the client hung up (BrokenPipe / ConnectionAborted)
                pass

    srv = _StallServer(("127.0.0.1", 0), Handler)
    Handler.stopping = srv.stopping
    threading.Thread(target=srv.serve_forever, name="test-stall-server", daemon=True).start()
    return srv


def _backend(port, api="openai"):
    res = SimpleNamespace(resolved=True, url=f"http://127.0.0.1:{port}", api=api, model="test-model",
                          details={}, reason="")
    return SimpleNamespace(link=SimpleNamespace(sync=SimpleNamespace(resolve=lambda cap: res)))


def _sse(handler, pieces, pause=0.0):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.end_headers()
    for p in pieces:
        handler.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}\n\n".encode())
        handler.wfile.flush()
        if handler.stopping.wait(pause):
            return
    handler.wfile.write(b"data: [DONE]\n\n")


def test_streamed_reply_is_joined():
    srv = _server(lambda h: _sse(h, ['{"title": ', '"Disco"}']))
    try:
        chat = mv_planner.writer_chat(_backend(srv.server_port))
        assert chat([{"role": "user", "content": "x"}], 50, 0.2) == '{"title": "Disco"}'
    finally:
        srv.finish()


def test_ollama_stream_is_joined():
    def ndjson(h):
        h.send_response(200)
        h.end_headers()
        for p, done in (("hola ", False), ("mundo", False), ("", True)):
            h.wfile.write((json.dumps({"message": {"content": p}, "done": done}) + "\n").encode())
    srv = _server(ndjson)
    try:
        chat = mv_planner.writer_chat(_backend(srv.server_port, api="ollama"))
        assert chat([{"role": "user", "content": "x"}], 50, 0.2) == "hola mundo"
    finally:
        srv.finish()


def test_a_model_that_never_starts_answering_is_reported_not_waited_on():
    srv = _server(lambda h: h.stopping.wait(9))  # prompt processing stuck: not a byte
    try:
        chat = mv_planner.writer_chat(_backend(srv.server_port), first_token_s=0.5, stall_s=0.5,
                                      busy=lambda: "ComfyUI :8189 is rendering on GPU 1.")
        started = time.monotonic()
        with pytest.raises(mv_planner.WriterStalled) as err:
            chat([{"role": "user", "content": "x"}], 50, 0.2)
        assert time.monotonic() - started < 10  # the watchdog floor is 5 s
        assert err.value.code == "llm_stalled" and "did not start answering" in err.value.message
        assert "ComfyUI :8189 is rendering on GPU 1." in err.value.message
    finally:
        srv.finish()


def test_keepalives_without_text_do_not_count_as_progress():
    srv = _server(lambda h: _sse(h, [""] * 8, pause=0.3))
    try:
        chat = mv_planner.writer_chat(_backend(srv.server_port), first_token_s=1.0, stall_s=1.0)
        with pytest.raises(mv_planner.WriterStalled):
            chat([{"role": "user", "content": "x"}], 50, 0.2)
    finally:
        srv.finish()
