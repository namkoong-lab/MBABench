"""Derive the agent identity from the behavior-determining fields in cfg.

`agent.model_name` / `agent.agent_folder` used to be free-form yaml strings,
which let operators flip a model knob (e.g. `claude_excel_agent.model`)
without updating the DB label — two functionally different runs ended up
under the same `task_attempts.agent_model_name`. This module makes the
identity a pure function of the fields that actually change agent output,
so drift is impossible.

To add a new mode: add an entry to the relevant `_*_IDENTITIES` table.
Unknown combinations raise `UnknownAgentCombination`, which forces a
naming decision before an unclassified label reaches the DB.

----------------------------------------------------------------------------
Historical convention (preserved deliberately)
----------------------------------------------------------------------------
Existing Excel attempts in the BizbenchV1 `task_attempts` table use:
    agent_model_type = "gui"
    agent_model_name = "claude_excel_agent" | "chatgpt_excel_agent" | "tabai"

There is no historical bifurcation by model (Claude Opus 4.6 vs Sonnet 4.6,
ChatGPT thinking_effort=Heavy vs Standard, etc.). To keep `infra/run.py`
attempts in the same name-space as legacy uploads (so DB analyses don't
have to UNION across two label conventions), this module returns a single
identity per agent_type, regardless of model.

If/when finer-grained labels are wanted (e.g. to separate Opus 4.6 runs
from Sonnet 4.6 runs in analyses), extend the relevant `_*_IDENTITIES`
table to key on `(model,)` like the gui-agents-master donor does, and
backfill historical rows in a separate migration before merging.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace


@dataclass(frozen=True)
class AgentIdentity:
    model_name: str  # → task_attempts.agent_model_name
    agent_folder: str  # → S3 prefix segment
    agent_model_type: str = "gui"  # → task_attempts.agent_model_type


class UnknownAgentCombination(ValueError):
    pass


# Single identity per agent_type — preserves historical "gui" + agent-type
# naming. See module docstring for the rationale.
_CLAUDE_EXCEL_IDENTITY = AgentIdentity(
    model_name="claude_excel_agent",
    agent_folder="claude_excel_agent",
    agent_model_type="gui",
)
_CHATGPT_EXCEL_IDENTITY = AgentIdentity(
    model_name="chatgpt_excel_agent",
    agent_folder="chatgpt_excel_agent",
    agent_model_type="gui",
)
_TABAI_IDENTITY = AgentIdentity(
    model_name="tabai",
    agent_folder="tabai",
    agent_model_type="gui",
)


def resolve_agent_identity(cfg: SimpleNamespace) -> AgentIdentity:
    provider = getattr(getattr(cfg, "provider", None), "kind", None)
    if provider == "claude_excel_agent":
        return _CLAUDE_EXCEL_IDENTITY
    if provider == "chatgpt_excel_agent":
        return _CHATGPT_EXCEL_IDENTITY
    if provider == "tabai":
        return _TABAI_IDENTITY
    raise UnknownAgentCombination(
        f"provider.kind={provider!r} has no identity resolver. "
        f"Known providers: claude_excel_agent, chatgpt_excel_agent, tabai. "
        f"Add an entry in infra/configs/agent_identity.py."
    )
