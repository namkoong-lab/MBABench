"""Shared prompt versioning config — single source of truth for all batch runners."""
import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

PROMPT_VERSIONS = {
    "v1": {"system": "system_prompt_v1.txt", "fmwc": "task_template_fmwc_v1.txt", "wsp": "task_template_wsp_v0.txt"},
    "v2": {"system": "system_prompt_v2.txt", "fmwc": "task_template_fmwc_v2.txt", "wsp": "task_template_wsp_v1.txt"},
    "v3": {"system": "system_prompt_v3.txt", "fmwc": "task_template_fmwc_v3.txt", "wsp": "task_template_wsp_v1.txt"},
    "v4": {"system": "system_prompt_v4.txt", "fmwc": "task_template_fmwc_v4.txt", "wsp": "task_template_wsp_v1.txt"},
    "v5": {"system": "system_prompt_v5.txt", "fmwc": "task_template_fmwc_v4.txt", "wsp": "task_template_wsp_v1.txt"},
    "v6": {"system": "system_prompt_v6.txt", "fmwc": "task_template_fmwc_v4.txt", "wsp": "task_template_wsp_v1.txt"},
    "v7": {"system": "system_prompt_v7.txt", "fmwc": "task_template_fmwc_v4.txt", "wsp": "task_template_wsp_v1.txt"},
    "v8": {"system": "system_prompt_v8.txt", "fmwc": "task_template_fmwc_v4.txt", "wsp": "task_template_wsp_v1.txt"},
    "v9": {"system": "system_prompt_v9.txt", "fmwc": "task_template_fmwc_v4.txt", "wsp": "task_template_wsp_v1.txt"},
    "v10": {"system": "system_prompt_v10.txt", "fmwc": "task_template_fmwc_v4.txt", "wsp": "task_template_wsp_v1.txt"},
    # v11: byte-exact prompts of the pv1105 benchmark wave (system v11 + fmwc/wsp
    # templates v5), recovered from S3 task_attempts.prompt_files. Frozen — do not edit.
    "v11": {"system": "system_prompt_v11.txt", "fmwc": "task_template_fmwc_v5.txt", "wsp": "task_template_wsp_v5.txt"},
}
DEFAULT_PROMPT_VERSION = "v10"


def parse_prompt_version(system_path: Path, template_path: Path) -> int:
    """Compute prompt_version: system_v{X} * 100 + template_v{Y}."""
    sys_match = re.search(r'_v(\d+)\.txt$', system_path.name)
    tpl_match = re.search(r'_v(\d+)\.txt$', template_path.name)
    sys_ver = int(sys_match.group(1)) if sys_match else 0
    tpl_ver = int(tpl_match.group(1)) if tpl_match else 0
    return sys_ver * 100 + tpl_ver
