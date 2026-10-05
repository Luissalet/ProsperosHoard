"""Optional audio.cpp voice service; reuses the Voice studio and family TTS pipeline.

Configure YOVOICE_URL (loopback only), YOVOICE_API_TOKEN or YOVOICE_TOKEN_FILE,
and YOVOICE_MODEL. No model download, host shell, or implicit cloud fallback.
"""
from __future__ import annotations

import io
import os
import re
import time
import uuid
import wave
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .voice_engines import EngineCapabilities, EngineNotInstalled, TTSEngine


class YovoiceEngine(TTSEngine):
    id = "yovoice"
    label = "Yovoice / audio.cpp (local service)"
    capabilities = EngineCapabilities(languages=["en", "es", "fr", "de", "it", "pt", "ja", "zh", "ko", "ru", "ar"], cloning=True, needs_gpu=False)

    def __init__(self, url=None, token=None, model=None, *, transport=None, timeout_s=150):
        self.url = (url if url is not None else os.getenv("YOVOICE_URL", "")).rstrip("/")
        self.token = token if token is not None else os.getenv("YOVOICE_API_TOKEN", "")
        token_file = os.getenv("YOVOICE_TOKEN_FILE", "")
        if not self.token and token_file:
            try:
                self.token = Path(token_file).read_text(encoding="utf-8").strip()
            except OSError:
                pass
        self.model = model if model is not None else os.getenv("YOVOICE_MODEL", "")
        self.transport = transport
        self.timeout_s = timeout_s
        self.capabilities = EngineCapabilities(languages=["en", "es", "fr", "de", "it", "pt", "ja", "zh", "ko", "ru", "ar"],
            cloning=not any(part in self.model for part in ("kokoro", "customvoice", "voicedesign")), needs_gpu=False)

    def _configured(self):
        try:
            parts = urlsplit(self.url)
            return (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")
                    and parts.port is not None and not parts.username and not parts.password
                    and parts.path in ("", "/") and not parts.query and not parts.fragment
                    and len(self.token) >= 32 and bool(re.fullmatch(r"[\w.-]{1,100}", self.model)))
        except ValueError:
            return False

    def is_installed(self):
        # Like package adapters, this is a cheap configuration check. Status distinguishes reachability.
        return bool(self._configured())

    def install_hint(self):
        return "Run the local Yovoice service with an installed model; configure YOVOICE_URL, YOVOICE_API_TOKEN (or token file), and YOVOICE_MODEL"

    def _client(self):
        return httpx.Client(base_url=self.url, headers={"Authorization": "Bearer " + self.token}, timeout=10,
                            follow_redirects=False, trust_env=False, transport=self.transport)

    @staticmethod
    def _check(response):
        # Never surface remote bodies, headers, credentials or arbitrary server file paths.
        if response.status_code >= 300:
            raise EngineNotInstalled("yovoice", f"voice service returned HTTP {response.status_code}; check its status and model")
        return response

    def status(self):
        result = super().status()
        result["model"] = self.model
        result["configured"] = self.is_installed()
        if not result["configured"]:
            return result
        try:
            with self._client() as client:
                status = self._check(client.get("/v1/status")).json()
                models = self._check(client.get("/v1/models")).json()
            installed = {r.get("id") for r in models.get("installed", []) if isinstance(r, dict)}
            result.update(installed=bool(status.get("ready") and self.model in installed),
                          model_installed=self.model in installed, reachable=True,
                          reason="ready" if status.get("ready") and self.model in installed else "runtime or selected model is not ready")
            options = models.get("generationOptions", {})
            result["generation_options"] = options.get(self.model, {}) if isinstance(options, dict) else options
        except (httpx.HTTPError, ValueError, EngineNotInstalled):
            result.update(installed=False, reachable=False, reason="voice service unavailable; check local service and token")
        return result

    def synthesize(self, text, voice_ref=None, speed=None, pitch=None, style=None, sample_path=None, language=None):
        if not self.is_installed():
            raise EngineNotInstalled(self.id, self.install_hint())
        if not text.strip() or len(text) > 20_000:
            raise ValueError("Text must contain 1..20000 characters")
        if pitch is not None:
            raise ValueError("Yovoice has no universal pitch parameter; use an engine preset")
        spec = {"text": text, "modelId": self.model}
        if speed is not None:
            if not 0.5 <= speed <= 2:
                raise ValueError("Speed must be 0.5..2")
            if self.model.startswith("omni"):
                spec["omniSpeed"] = speed
            elif self.model.startswith(("index-", "kokoro")):
                spec["speed"] = speed
            elif speed != 1:
                raise ValueError("Speed is not supported by this selected model adapter")
        if style and len(style) > 500:
            raise ValueError("Voice style is limited to 500 characters")
        if style:
            if self.model.startswith("index-"):
                spec.update(mode="text", emotionText=style)
            elif self.model.startswith("qwen3-") and "voicedesign" in self.model:
                spec["voiceDescription"] = style
            elif self.model.startswith("omni"):
                spec.update(voiceMode="design", voiceDescription=style)
            elif self.model.startswith("voxcpm"):
                spec.update(voxMode="design", voiceDescription=style)
            else:
                raise ValueError("Style is not supported by this model adapter")
        # Language support belongs to the selected model; do not silently invent field names.
        if language and language not in self.capabilities.languages and language != "auto":
            raise ValueError("Language is outside this adapter's supported language set")
        if language:
            if self.model.startswith("index-"):
                if language not in ("auto", "zh", "en", "ja", "es", "ar"):
                    raise ValueError("Index model does not support the requested language")
                spec['language'] = language
            else:
                spec['synthesisLanguage'] = language
        request_id = uuid.uuid4().hex
        with self._client() as client:
            if sample_path:
                sample = Path(sample_path)
                if sample.stat().st_size > 20 << 20:
                    raise ValueError("Reference audio exceeds 20 MiB")
                with sample.open("rb") as audio:
                    reference = self._check(client.post("/v1/voices", params={"name": "Prospero reference"}, content=audio, headers={"Content-Type": "application/octet-stream"})).json()
                spec["voiceId"] = reference["id"]
            elif voice_ref:
                spec["speaker" if self.model.startswith("kokoro-") or "custom" in self.model else "voiceId"] = voice_ref
            state = self._check(client.put("/v1/jobs/" + request_id, json=spec)).json()
            deadline = time.monotonic() + self.timeout_s
            while state.get("status") == "running" and time.monotonic() < deadline:
                time.sleep(0.25)
                state = self._check(client.get("/v1/jobs/" + request_id)).json()
            if state.get("status") == "running":
                self._check(client.delete("/v1/jobs/" + request_id))
                raise EngineNotInstalled(self.id, "voice job timed out; cancellation requested for this job only")
            if state.get("status") != "completed" or not re.fullmatch(r"[\w-]{1,100}", str(state.get("id", ""))):
                raise EngineNotInstalled(self.id, "voice job did not complete; inspect local voice service")
            content = bytearray()
            with client.stream("GET", "/v1/audio/" + state["id"]) as response:
                self._check(response)
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 64 << 20:
                        raise ValueError("Generated audio exceeds 64 MiB")
        try:
            with wave.open(io.BytesIO(content)) as wav:
                if wav.getnframes() < 1 or wav.getframerate() < 1:
                    raise ValueError("Empty generated audio")
        except (wave.Error, EOFError) as exc:
            raise ValueError("Voice service did not return a valid WAV") from exc
        return bytes(content)
