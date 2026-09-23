"""The ComfyUI wait is generous per output kind and overridable, so a
render on a shared GPU is not failed while it is still running."""
from prosperos_hoard import engine


def test_default_timeouts_per_kind(monkeypatch):
    monkeypatch.delenv("PROSPERO_COMFY_TIMEOUT_S", raising=False)
    assert engine.comfy_timeout_s("video") == 3600.0
    assert engine.comfy_timeout_s("audio") == 1800.0
    assert engine.comfy_timeout_s("image") == 1200.0
    assert engine.comfy_timeout_s(None) == 1200.0


def test_env_override_wins_for_every_kind(monkeypatch):
    monkeypatch.setenv("PROSPERO_COMFY_TIMEOUT_S", "7200")
    assert engine.comfy_timeout_s("video") == 7200.0
    assert engine.comfy_timeout_s("image") == 7200.0


def test_bad_or_non_positive_override_is_ignored(monkeypatch):
    for raw in ("abc", "0", "-5", "  "):
        monkeypatch.setenv("PROSPERO_COMFY_TIMEOUT_S", raw)
        assert engine.comfy_timeout_s("video") == 3600.0
