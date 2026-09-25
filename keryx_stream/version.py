"""Plugin version, the Hermes floor it needs, and the feature list
/keryx/health advertises.

The feature list is DERIVED, never hard-coded: a feature is listed only when
the running Hermes provides what it needs (a hook registered, a payload field
seen, a config knob on) or when something actually fed it through
``POST /keryx/publish``. The app reads the list to tell "this install cannot do
that" from "that is broken"; advertising a feature the host cannot feed would
turn the first into the second.
"""
from __future__ import annotations

__version__ = "0.4.0"

# Oldest Hermes whose plugin surface this release relies on: the streaming
# observer hooks with ``turn_id``/``iteration`` in their payloads
# (agent/stream_delivery.py ``_stream_hook_base_payload``), ``post_llm_call``
# with ``turn_id`` (agent/turn_finalizer.py ``_apply_output_hooks``),
# ``post_api_request`` with ``usage``, ``subagent_start``/``subagent_stop``,
# ``pre/post_auxiliary_call`` and ``ctx.register_middleware("llm_request")``.
# Checked against 0.21.3 and 0.21.5 trees; older releases are untested.
REQUIRES_HERMES = ">=0.21.3"

# Always served by server.py itself.
CORE_FEATURES = ("stream", "stream.chat_key", "publish", "toolsets")


def features(*, panel_features=(), proxy: bool = False, probe=None) -> list[str]:
    """The feature list for /keryx/health.

    ``panel_features`` is what ``panels.register_panel_routes`` actually mounted
    (empty when the panels could not load); ``probe`` is the process's
    :class:`keryx_stream.probe.Probe`, which knows which hooks registered, which
    payload fields have been seen and which events were fed.
    """
    out = list(CORE_FEATURES)
    if probe is not None:
        out += [f for f in probe.stream_features() if f not in out]
    out += [f for f in panel_features if f not in out]
    if proxy:
        out.append("proxy")
    return out
