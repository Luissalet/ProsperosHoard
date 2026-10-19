"""Owned HTML frame renderer; its caller owns cancellation and process lifetime."""
import base64
import json
from pathlib import Path
import sys

from .errors import LumiereError
from .html_motion import HtmlMotion


def send(value):
    print(json.dumps(value), flush=True)


def main():
    try:
        manifest = json.loads(sys.stdin.readline())
        store = HtmlMotion(Path(manifest["source_path"]).parents[4])
        with store.page(manifest) as (page, errors):
            send({"ready": True})
            for line in sys.stdin:
                command = json.loads(line)
                data = store.capture(page, errors, command["at"])
                send({"png": base64.b64encode(data).decode("ascii")})
    except LumiereError as exc:
        send({"error": str(exc), "code": exc.code})
    except Exception as exc:
        send({"error": str(exc), "code": "html_motion_browser"})


if __name__ == "__main__":
    main()
