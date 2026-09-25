"""Thinking dial for local OpenAI-compatible brains, as ``llm_request`` middleware.

Local brains (provider ``custom`` / ``custom:*`` — vLLM, SGLang, llama.cpp)
switch thinking with a chat-template kwarg, not the OpenRouter-style
``reasoning`` field. This maps Hermes' reasoning setting onto
``extra_body.chat_template_kwargs`` so ``/reasoning none`` ⇄ any effort level is
a real on/off, and graded templates (GLM-5.3, DeepSeek-V4.1) get the rung they
accept.

Where the reasoning setting comes from: the ``llm_request`` middleware context
(agent/turn_api_request.py ``apply_llm_request_middleware(...)``) carries
session/model/provider but NOT ``reasoning_config``. What it does carry is the
provider request itself, and Hermes' custom-provider profile already encodes the
effective reasoning config there as top-level ``reasoning_effort`` — ``"none"``
when thinking is off, the clamped effort word otherwise, absent when the route
has no reasoning field (plugins/model-providers/custom/__init__.py
``CustomProfile.build_api_kwargs_extras``). That value already folds in the
session's ``/reasoning`` override, the profile default and the global config, so
it is read back from the request instead of re-deriving it from config.yaml.
The plugin's own ``PUT /keryx/reasoning`` writes ``agent.reasoning_effort``,
which reaches the request by the same route on the next session.

Mistral-native exception: vLLM's ``--tokenizer-mode mistral`` rejects any
request carrying ``chat_template_kwargs`` (HTTP 400); its dial is the top-level
``reasoning_effort`` restricted to ``none``/``high``.

Kill switch: ``KERYX_THINKING_KWARGS=off`` in the environment, or
``keryx_stream.thinking_kwargs: false`` in config.yaml.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("keryx_stream.thinking")


def is_mistral_native(model: str) -> bool:
    """Models served via vLLM's ``--tokenizer-mode mistral`` (name heuristic)."""
    normalized = (model or "").strip().lower()
    return normalized.startswith("mistral") or "/mistral" in normalized


def is_glm53(model: str) -> bool:
    m = (model or "").strip().lower()
    return any(t in m for t in ("glm-5.3", "glm-5-3", "glm-5p3", "glm53"))


def is_dsv41(model: str) -> bool:
    m = (model or "").strip().lower()
    return any(t in m for t in ("deepseek-v4.1", "deepseek-v41", "deepseek_v4.1", "deepseek_v41",
                                "deepseek-v4-1", "dsv41"))


# Local templates with a GRADED ``reasoning_effort`` kwarg, keyed by family. Each
# ladder is what the served template accepts, measured — not the model card:
#   glm53  low / high; anything else is served as max (2026-09-11).
#   dsv41  low / high / xhigh / max; ``medium`` is HTTP 400 (2026-09-13), so the
#          generic medium MUST be clamped (nearest weaker → low), never sent.
# ``enable_thinking: false`` is the real off switch on both. Adding a family here
# teaches both the wire and the app's ladder (panels reads the same table).
GLM53_LOCAL_EFFORTS = ("low", "high", "max")
GLM53_LOCAL_OVERRIDES = {"xhigh": "max", "ultra": "max"}
LOCAL_TEMPLATE_EFFORTS: dict[str, tuple] = {
    "glm53": GLM53_LOCAL_EFFORTS,
    "dsv41": ("low", "high", "xhigh", "max"),
}
LOCAL_TEMPLATE_OVERRIDES: dict[str, dict[str, str]] = {
    "glm53": GLM53_LOCAL_OVERRIDES,
    "dsv41": {"ultra": "max"},
}


def local_template_family(model: str) -> str | None:
    if is_glm53(model):
        return "glm53"
    if is_dsv41(model):
        return "dsv41"
    return None


def local_wire_effort(effort: str, family: str) -> str | None:
    """The template rung for a Hermes effort word on ``family``; None = send nothing."""
    e = (effort or "").strip().lower()
    if not e:
        return None
    supported = LOCAL_TEMPLATE_EFFORTS.get(family) or ()
    overrides = LOCAL_TEMPLATE_OVERRIDES.get(family) or {}
    try:
        from agent.reasoning_effort import clamp_effort

        return clamp_effort(e, supported, overrides)
    except Exception:
        return overrides.get(e, e if e in supported else "low")


def is_local_provider(provider: str) -> bool:
    p = (provider or "").strip().lower()
    return p == "custom" or p.startswith("custom:")


def map_request(request: dict[str, Any], model: str) -> dict[str, Any] | None:
    """The rewritten provider kwargs, or None to leave the request untouched.

    Pure: never mutates ``request``. Acts only when the request carries Hermes'
    own ``reasoning_effort`` (i.e. reasoning is configured for this route) —
    a stock request with no reasoning field is passed through unchanged.
    """
    if not isinstance(request, dict):
        return None
    raw = request.get("reasoning_effort")
    if raw is None:
        return None
    effort = str(raw).strip().lower()
    enabled = effort not in ("none", "")
    out = dict(request)
    extra = dict(out.get("extra_body") or {})
    if is_mistral_native(model):
        extra.pop("chat_template_kwargs", None)  # a Mistral tokenizer 400s on it
        if extra:
            out["extra_body"] = extra
        else:
            out.pop("extra_body", None)
        out["reasoning_effort"] = "high" if enabled else "none"
        return out
    ctk = dict(extra.get("chat_template_kwargs") or {})
    ctk["enable_thinking"] = enabled
    family = local_template_family(model)
    if family:
        wire = local_wire_effort(effort, family) if enabled else None
        if wire:
            ctk["reasoning_effort"] = wire
            # Keep the top-level field on the same rung: a template that also reads
            # the request-level effort must never see a word it rejects (dsv41 medium).
            out["reasoning_effort"] = wire
        else:
            ctk.pop("reasoning_effort", None)
    extra["chat_template_kwargs"] = ctk
    out["extra_body"] = extra
    return out


def killed(config_value: Any = None) -> bool:
    if os.environ.get("KERYX_THINKING_KWARGS", "").strip().lower() in {"0", "off", "false", "no"}:
        return True
    return config_value is False or str(config_value).strip().lower() in {"off", "false", "no", "0"}


def make_llm_request_middleware(config_value: Any = None):
    """The ``llm_request`` middleware callback (hermes_cli/middleware.py:
    returning ``{"request": {...}}`` replaces the provider kwargs for this attempt)."""

    def keryx_thinking_kwargs(*, request=None, provider="", model="", **_):
        try:
            if killed(config_value) or not is_local_provider(str(provider or "")):
                return None
            mapped = map_request(request, str(model or (request or {}).get("model") or ""))
            if mapped is None or mapped == request:
                return None
            return {"request": mapped, "source": "keryx-stream", "reason": "thinking chat_template_kwargs"}
        except Exception:
            logger.debug("keryx-stream: thinking middleware failed", exc_info=True)
            return None

    return keryx_thinking_kwargs
