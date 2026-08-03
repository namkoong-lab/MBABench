# Recovered pv9 benchmark prompts (BizbenchV1 GUI wave)

Byte-exact prompt payloads used on the 206 in-scope tasks (fmwc/modeloff/wsp),
recovered from `task_attempts.prompt_files` in S3 on 2026-07-21 and verified
task-invariant (identical SHA across sampled attempts in all three sources).

Every agent sent **exactly one prompt** per task. Each full text =
`SHARED_rubric_preamble.txt` (11,971 chars) + one closing.

| File | Used by | Chars | Closing |
|---|---|---|---|
| `chatgpt_pro_FULL.txt`   | `chatgpt_web_pro` | 13,589 | A — single uninterrupted pass |
| `chatgpt_agent_FULL.txt` | `chatgpt_agent`   | 13,275 | B — three steps |
| `claude_web_FULL.txt`    | `claude_web`      | 13,275 | B (byte-identical to chatgpt_agent) |

DB labels these `prompt_version: 9`; the payloads carry `prompt_version: 8`
internally (relabel, not rewrite). Both match the v1 dispatcher templates
`infra/dispatcher/config_templates/{chatgpt_pro,chatgpt_agent}.yaml` byte-for-byte.

**Claude caveat:** in-scope (Apr 19-23) claude prompt files are truncated to
exactly 500 bytes in S3. The full text above comes from the `jp_final` batch
(Apr 27, same agent + same prompt_version); its first 500 bytes match the
truncated in-scope files exactly. Both variants share those first 500 bytes,
so the truncated files alone cannot discriminate — the identification rests on
prompt_version continuity within the wave.
