"""Check tracked text for the account and private DNS suffix of this installation."""

from pathlib import Path
from urllib.parse import quote
import json
import os
import re
import subprocess

import pytest


_ROOT = Path(__file__).resolve().parents[1]


def _private_dns_suffix():
    configured = os.environ.get("HOARD_TEST_PRIVATE_DNS_SUFFIX")
    if configured:
        return configured.strip().rstrip(".")
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, check=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        status = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    tailnet = status.get("CurrentTailnet") or {}
    return (tailnet.get("MagicDNSSuffix") or status.get("MagicDNSSuffix") or "").rstrip(".")


def _patterns(account, suffix):
    patterns = {}
    if account:
        names = "|".join(re.escape(name) for name in dict.fromkeys((account, quote(account, safe=""))))
        patterns["installation account"] = re.compile(rf"(?<![^\W_])(?:{names})(?![^\W_])", re.I)
    if suffix:
        patterns["private network"] = re.compile(rf"(?<![\w-]){re.escape(suffix)}(?![\w-])", re.I)
    return patterns


def _findings(text, patterns):
    for number, line in enumerate(text.splitlines(), 1):
        for kind, pattern in patterns.items():
            matches = list(pattern.finditer(line))
            if matches:
                yield number, kind, len(matches)


def _tracked_findings(root, patterns):
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
    for entry in filter(None, paths.split(b"\0")):
        name = os.fsdecode(entry)
        safe_name = name
        for pattern in patterns.values():
            safe_name = pattern.sub("[redacted]", safe_name)
        yield from (f"{safe_name}: path contains {kind}" for _, kind, _ in _findings(name, patterns))

        path = root / name
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16", errors="replace")
        elif b"\0" in data:
            continue
        else:
            text = data.decode("utf-8", errors="replace")
        for number, kind, count in _findings(text, patterns):
            yield f"{safe_name}:{number}: {kind} ({count})"


def test_tracked_text_does_not_identify_the_installation():
    if not (_ROOT / ".git").exists():
        pytest.skip("requires a Git worktree")
    account = os.environ.get("USERNAME", "") if os.name == "nt" else ""
    patterns = _patterns(account, _private_dns_suffix())
    if not patterns:
        pytest.skip("Windows account and private DNS suffix are unavailable")
    findings = list(_tracked_findings(_ROOT, patterns))
    assert not findings, "\n" + "\n".join(findings)


def test_installation_identifiers_are_detected_without_reporting_their_values(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / "receipt.txt"
    path.write_text(
        "file:///C:/Users/Example%20Account/file\n"
        "pytest-of-example account and EXAMPLE ACCOUNT@example.invalid\n"
        "https://worker.sample-private.ts.net:8443\n"
        "https://worker.not-sample-private.ts.net\n",
        encoding="utf-16",
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "receipt.txt"], check=True)
    (tmp_path / "untracked.txt").write_text("Example Account", encoding="utf-8")
    assert list(_tracked_findings(tmp_path, _patterns("Example Account", "sample-private.ts.net"))) == [
        "receipt.txt:1: installation account (1)",
        "receipt.txt:2: installation account (2)",
        "receipt.txt:3: private network (1)",
    ]
