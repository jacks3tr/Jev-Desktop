"""Jev host-desktop testing plugin.

Calling coding model: test intent, fixtures, acceptance criteria, interpretation.
Jev policy: next permitted operation and compatible observed target.
Runtime: step ordering, budgets, pauses, assertions, resumability.
Native driver: host observation, reference resolution, input, state validation.
Verifier: evidence-backed assertions; never accepts a model completion choice as proof.
Broker: cross-client desktop ownership, authorization, cancellation, worker lifecycle.
"""

from .contracts import SCHEMA_VERSION

__all__ = ["SCHEMA_VERSION"]
