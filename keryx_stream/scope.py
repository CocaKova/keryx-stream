"""Profile secret scope for the plugin's own server.

On a multiplexed (multi-profile) gateway Hermes refuses any credential read
made outside a profile scope — reading ``os.environ`` there could leak another
profile's key. The native API server enters the default profile's scope per
request; this server is a separate thread with no such wrapper, so helpers that
probe for keys (``_toolset_has_keys`` → ``FIRECRAWL_API_KEY`` …) raised
``UnscopedSecretError`` and the route answered 500.

The middleware below enters the process profile's scope for each request. The
handlers hop to worker threads with ``asyncio.to_thread``, which carries
contextvars, so the scope follows the work. Single-profile gateways and
Hermes releases without ``agent.secret_scope`` are a no-op.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("keryx_stream.scope")


def _enter():
    """Token to reset with, or None when no scope is needed/available."""
    try:
        from agent import secret_scope
        from hermes_constants import get_hermes_home

        if not secret_scope.is_multiplex_active():
            return None
        secrets = secret_scope.build_profile_secret_scope(get_hermes_home())
        return secret_scope, secret_scope.set_secret_scope(secrets)
    except Exception:
        logger.debug("keryx-stream: no profile secret scope entered", exc_info=True)
        return None


def make_scope_middleware():
    from aiohttp import web

    @web.middleware
    async def profile_scope(request, handler):
        entered = _enter()
        try:
            return await handler(request)
        finally:
            if entered is not None:
                module, token = entered
                try:
                    module.reset_secret_scope(token)
                except Exception:
                    logger.debug("keryx-stream: secret scope reset failed", exc_info=True)

    return profile_scope
