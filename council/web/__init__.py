"""Web UI for AI Council (M5).

A thin FastAPI layer over the same :class:`council.core.orchestrator.CouncilEngine`
the CLI uses. The engine keeps running in-process so that pause / resume /
intervention round-trips stay cheap and the event log is the single source of
truth for anything the UI shows.

No model output is ever written to disk here except through the ordinary event
store (application data dir) — the browser downloads exports, the server never
touches an arbitrary output path.
"""

from __future__ import annotations

__all__: list[str] = []
