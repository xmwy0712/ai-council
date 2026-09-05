"""Engine core.

This package is the whole brain of AI Council. It must never import from
``council.api`` or the web frontend: the engine has to run head-less under a
plain CLI, and every UI surface talks to it through events and protocols only.
"""

from __future__ import annotations

__all__ = [
    "config",
    "contracts",
    "errors",
    "events",
    "machine",
    "orchestrator",
    "paths",
    "prompts",
    "schema",
    "state",
    "store",
]
