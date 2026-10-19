"""Domain errors. The API and the agent route map them to HTTP codes (400 / 404 / 403 / 409)."""

from __future__ import annotations


class LumiereError(ValueError):
    """Invalid input or a violated rule (HTTP 400). ``code`` is an optional machine-readable reason."""

    code: str | None = None

    def __init__(self, message: str = "", code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class NotFound(LookupError):
    """The record does not exist (HTTP 404)."""


class Refused(LumiereError):
    """Blocked by a safety rule (HTTP 403)."""


class Conflict(LumiereError):
    """The project changed since the caller read it (HTTP 409)."""

    code = "version_conflict"


class FfmpegMissing(LumiereError):
    """ffmpeg / ffprobe cannot be found."""

    code = "ffmpeg_missing"


class FfmpegFailed(LumiereError):
    """ffmpeg ran and failed; the message carries the tail of its log."""

    code = "ffmpeg_failed"


class ModelUnavailable(LumiereError):
    """The operation needs a model and none is reachable."""

    code = "model_unavailable"


class TranscriberUnavailable(LumiereError):
    """No speech-to-text engine is installed or reachable."""

    code = "transcriber_unavailable"
