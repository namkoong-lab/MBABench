"""Derive the agent identity from the behavior-determining fields in cfg.

`agent.model_name` / `agent.agent_folder` used to be free-form yaml strings,
which let operators flip `chatgpt_web.agent_mode` or `chatgpt_web.model`
without updating the DB label — two functionally different runs ended up
under the same `task_attempts.agent_model_name`. This module makes the
identity a pure function of the fields that actually change agent output,
so drift is impossible.

To add a new mode: add an entry to the relevant `_*_IDENTITIES` table.
Unknown combinations raise `UnknownAgentCombination`, which forces a
naming decision before an unclassified label reaches the DB.
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


# Signature: (claude_web.mode, claude_web.model, claude_web.effort). The
# claude.ai UI (2026-07) exposes reasoning effort AND a Chat/Cowork mode
# toggle as first-class controls that change agent output, so both
# bifurcate the DB label. mode defaults to "chat"; effort=None entries are
# the pre-effort-era runs, kept for DB continuity.
_CLAUDE_IDENTITIES: dict[tuple, AgentIdentity] = {
    ("chat", "sonnet_4_6", None): AgentIdentity("claude_web", "claude_web"),
    ("chat", "opus_4_6", None): AgentIdentity(
        "claude_opus_4_6", "claude_opus_4_6"
    ),
    ("chat", "haiku_4_5", None): AgentIdentity(
        "claude_haiku_4_5", "claude_haiku_4_5"
    ),
    # 2026-07 benchmark refresh (names collision-checked against
    # mbabench/attempts/, mbabench/BizbenchV1/attempts/, and DB
    # agent_model_name on 2026-07-21; signed off 2026-07-21):
    ("chat", "fable_5", "max"): AgentIdentity(
        "claude_web_chat_fable5_max", "claude_web_chat_fable5_max"
    ),
    ("cowork", "fable_5", "max"): AgentIdentity(
        "claude_web_cowork_fable5_max", "claude_web_cowork_fable5_max"
    ),
    # 2026-07-24 grading-variance experiment (chat-mode Opus 4.8 at max
    # effort; collision-checked against DB agent_model_name + S3 prefixes;
    # _var suffix keeps it separate from any future production Opus 4.8 wave):
    ("chat", "opus_4_8", "max"): AgentIdentity(
        "claude_web_chat_opus4.8_max_var", "claude_web_chat_opus4.8_max_var"
    ),
}


# Agent mode no longer exists in the ChatGPT UI (removed ~mid-2026).
# agent_mode=True is refused at identity-resolution time rather than
# silently collapsed to the historical chatgpt_agent label — the historical
# label described a different backend that can no longer be invoked.

# Signatures (mode defaults to "chat"):
#   chat: ("chat", chatgpt_web.model, chatgpt_web.intelligence)
#   work: ("work", chatgpt_web.model, chatgpt_web.effort, chatgpt_web.speed)
# The chat picker is model (submenu) + intelligence (radios); the work
# picker is model + effort + speed under the pill's Advanced section.
# intelligence=None entries are the one-axis-era runs (model carried the
# legacy instant/thinking/pro values), kept for DB continuity.
_CHATGPT_IDENTITIES: dict[tuple, AgentIdentity] = {
    ("chat", None, None): AgentIdentity("chatgpt_web", "chatgpt_web"),
    ("chat", "instant", None): AgentIdentity(
        "chatgpt_instant", "chatgpt_instant"
    ),
    ("chat", "thinking", None): AgentIdentity(
        "chatgpt_thinking", "chatgpt_thinking"
    ),
    ("chat", "pro", None): AgentIdentity("chatgpt_web_pro", "chatgpt_web_pro"),
    # 2026-07 benchmark refresh (collision-checked + signed off 2026-07-21):
    ("chat", "gpt_5_6_sol", "pro"): AgentIdentity(
        "chatgpt_web_chat_gpt5.6_sol_pro", "chatgpt_web_chat_gpt5.6_sol_pro"
    ),
    ("work", "gpt_5_6_sol", "ultra", "standard"): AgentIdentity(
        "chatgpt_web_work_gpt5.6_sol_ultra", "chatgpt_web_work_gpt5.6_sol_ultra"
    ),
    # 2026-07-24 grading-variance experiment (chat-mode GPT-5.5 at Pro
    # intelligence; collision-checked against DB agent_model_name + S3
    # prefixes; _var suffix separates it from any future production wave):
    ("chat", "gpt_5_5", "pro"): AgentIdentity(
        "chatgpt_web_chat_gpt5.5_pro_var", "chatgpt_web_chat_gpt5.5_pro_var"
    ),
}


def resolve_agent_identity(cfg: SimpleNamespace) -> AgentIdentity:
    provider = getattr(getattr(cfg, "provider", None), "kind", None)
    if provider == "claude":
        return _resolve_claude(cfg)
    if provider == "chatgpt":
        return _resolve_chatgpt(cfg)
    raise UnknownAgentCombination(
        f"provider.kind={provider!r} has no identity resolver. "
        f"Add one in infra/configs/agent_identity.py."
    )


def _resolve_claude(cfg: SimpleNamespace) -> AgentIdentity:
    block = getattr(cfg, "claude_web", None)
    if block is None:
        raise UnknownAgentCombination(
            "provider=claude but cfg.claude_web block is missing."
        )
    mode = (getattr(block, "mode", None) or "chat").lower()
    model = getattr(block, "model", None)
    effort = getattr(block, "effort", None)
    key = (mode, model, effort)
    try:
        return _CLAUDE_IDENTITIES[key]
    except KeyError:
        raise UnknownAgentCombination(
            f"No Claude identity for (claude_web.mode, claude_web.model, "
            f"claude_web.effort)={key!r}. Known: {list(_CLAUDE_IDENTITIES)}. "
            f"Add an entry in infra/configs/agent_identity.py "
            f"if this is a real combination."
        )


def _resolve_chatgpt(cfg: SimpleNamespace) -> AgentIdentity:
    block = getattr(cfg, "chatgpt_web", None)
    if block is None:
        raise UnknownAgentCombination(
            "provider=chatgpt but cfg.chatgpt_web block is missing."
        )
    if bool(getattr(block, "agent_mode", False)):
        raise UnknownAgentCombination(
            "chatgpt_web.agent_mode=true, but Agent mode no longer exists "
            "in the ChatGPT UI (removed ~mid-2026) — the run would silently "
            "execute as a non-agent chat under the wrong DB label. Set "
            "agent_mode: false and pick chatgpt_web.model + "
            "chatgpt_web.intelligence instead."
        )
    mode = (getattr(block, "mode", None) or "chat").lower()
    model = getattr(block, "model", None)
    if mode == "work":
        effort = getattr(block, "effort", None)
        speed = getattr(block, "speed", None) or "standard"
        key = (mode, model, effort, speed)
        axes = "(mode, model, effort, speed)"
    else:
        intelligence = getattr(block, "intelligence", None)
        key = (mode, model, intelligence)
        axes = "(mode, model, intelligence)"
    try:
        return _CHATGPT_IDENTITIES[key]
    except KeyError:
        raise UnknownAgentCombination(
            f"No ChatGPT identity for chatgpt_web {axes}={key!r}. "
            f"Known: {list(_CHATGPT_IDENTITIES)}. "
            f"Add an entry in infra/configs/agent_identity.py "
            f"if this is a real combination."
        )
