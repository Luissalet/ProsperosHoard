from __future__ import annotations

import datetime as _dt
import os
import time
from pathlib import Path


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def replace_with_retry(src: "str | os.PathLike[str]", dst: "str | os.PathLike[str]", *,
                       attempts: int = 40, delay: float = 0.05) -> None:
    """os.replace that survives Windows sharing violations.

    On Windows, replacing a file fails with WinError 5/32 while any other
    handle has it open (a poll reading state.json, the indexer, an antivirus
    scan). Those holds last milliseconds, so retry with a short backoff
    (about 2 s in total) before giving up; the temp file is removed if the
    replace never lands.
    """
    last: "OSError | None" = None
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:  # WinError 5 / 32 map here
            last = exc
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33):
                raise
            last = exc
        time.sleep(min(0.25, delay * (1 + i)))
    try:
        Path(src).unlink()
    except OSError:
        pass
    assert last is not None
    raise last


def write_text_atomic(path: "str | os.PathLike[str]", text: str, *, encoding: str = "utf-8",
                      tmp: "str | os.PathLike[str] | None" = None) -> None:
    """Write to a sibling temp file and replace `path` with it (retrying on Windows locks)."""
    import threading
    target = Path(path)
    tmp_path = Path(tmp) if tmp is not None else target.with_name(
        f"{target.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp_path.write_text(text, encoding=encoding)
    replace_with_retry(tmp_path, target)
