"""The LLM half of the system: it reasons, proposes, and touches nothing.

Everything in this package is read-only by construction. The agent's single
output is an :class:`~fpl_buddy.decisions.schema.AgentProposal`; deterministic
code in ``decisions/`` decides whether that ever becomes an HTTP POST.
"""

from .build import build_agent, build_model, run_agent

__all__ = ["build_agent", "build_model", "run_agent"]
