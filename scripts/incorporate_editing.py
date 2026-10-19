"""Incorporate pinned public Git objects, never user data or a retired runtime."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

REVISION = "2fe8ea524998bca5bfcd522bd3daaefcfaa763ea"
ROOT = Path(__file__).resolve().parents[1]


def main():
    source = Path(sys.argv[1]).resolve()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args])
    records = []
    for name in git("ls-tree", "-r", "--name-only", REVISION).decode().splitlines():
        target = None
        if name.startswith("lumiere_hoard/"):
            target = "prosperos_hoard/editing/" + name.removeprefix("lumiere_hoard/")
        elif name.startswith("client/"):
            target = "editing_frontend/" + name.removeprefix("client/")
        elif name.startswith("native/"):
            target = "native/editing/" + name.removeprefix("native/")
        elif name in {"LICENSE", "README.md", "README.es.md", "requirements.txt", "requirements-gpu.txt", "requirements-speakers.txt"}:
            target = "docs/editing-origin/" + name
        elif name in {"package.json", "package-lock.json", "vite.config.js"}:
            target = "editing_frontend/" + name
        elif name.startswith("tests/"):
            target = "editing_tests/" + name.removeprefix("tests/")
        if target is None:
            continue
        data = git("show", REVISION + ":" + name)
        destination = ROOT / target
        if destination.exists():
            raise SystemExit(f"Refusing to overwrite {target}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        records.append({"source": name, "destination": target, "source_sha256": hashlib.sha256(data).hexdigest()})
    (ROOT / "docs" / "editing-origin" / "PROVENANCE.json").write_text(json.dumps({
        "repository": "https://github.com/Luissalet/LumieresHoard", "revision": REVISION,
        "licence": "MIT; additional native licences retained in native/editing", "files": records,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Incorporated {len(records)} public files; no ignored files or data read.")


if __name__ == "__main__":
    main()
