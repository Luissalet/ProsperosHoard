"""Talking to the local model through Hoard Link: JSON answers with one repair attempt."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from pydantic import ValidationError

from .errors import LumiereError, ModelUnavailable
from ..global_model import operation


class GenerationFailed(LumiereError):
    code = "generation_failed"


def _chat(svc: Any, messages: list[dict[str, str]], **kwargs: Any) -> Any:
    """One call to the pinned global model; transport errors are not replayed."""
    # Transport errors, stale pins and schema refusals are never retried against
    # another resolution. JSON repair below remains an explicit semantic step.
    return svc.link_sync.chat(messages, **kwargs)


@operation
def chat_json(svc: Any, messages: list[dict[str, str]], parse: Callable[[Any, bool], Any], *, max_tokens: int,
              effort: Optional[str] = "off") -> tuple[Any, Optional[str]]:
    """Ask for JSON, parse it, retry once with the error. Returns (value, model name)."""

    def ask(msgs: list[dict[str, str]]) -> tuple[str, Optional[str]]:
        try:
            result = _chat(svc, msgs, effort=effort, max_tokens=max_tokens, temperature=0.2)
        except Exception as error:  # noqa: BLE001
            raise ModelUnavailable(f"{type(error).__name__}: {str(error)[:200]}") from error
        return (getattr(result, "text", "") or "").strip(), getattr(result, "model", None)

    text, model = ask(messages)
    try:
        return parse(json_from_text(text), True), model
    except (ValueError, ValidationError) as first:
        repair = messages + [{"role": "assistant", "content": text[:6000]},
                             {"role": "user", "content": f"That answer cannot be used: {_describe(first)}\nReturn only the corrected JSON object."}]
        text2, model2 = ask(repair)
        try:
            return parse(json_from_text(text2), False), model2 or model
        except (ValueError, ValidationError) as second:
            raise GenerationFailed(f"The model did not return usable JSON: {_describe(second)}") from second


def _describe(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in error.errors()[:6])
    return str(error)[:400]


def json_from_text(text: str) -> Any:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S | re.I)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError("no JSON found in the answer")
    start = min(starts)
    closer = "}" if text[start] == "{" else "]"
    end = text.rfind(closer)
    if end <= start:
        raise ValueError("the JSON in the answer is not closed")
    return json.loads(text[start: end + 1])


@operation
def chat_text(svc: Any, messages: list[dict[str, str]], *, max_tokens: int = 1500, effort: Optional[str] = "off") -> tuple[str, Optional[str]]:
    try:
        result = _chat(svc, messages, effort=effort, max_tokens=max_tokens, temperature=0.3)
    except Exception as error:  # noqa: BLE001
        raise ModelUnavailable(f"{type(error).__name__}: {str(error)[:200]}") from error
    return (getattr(result, "text", "") or "").strip(), getattr(result, "model", None)
