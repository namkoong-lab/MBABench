"""Config loader for gui-agents infra.

Merges `configs.default.yaml` (schema + defaults, checked in) with an optional
sibling `configs.yaml` (user overrides, gitignored). User values win.

Each leaf in configs.default.yaml has the form `{value: <default>, required: <bool>}`.
In configs.yaml, leaves can be overridden either with the same full form
(`key: {value: X}`) or with a raw scalar/list (`key: X`).

After merge:
  - every `required: true` leaf must resolve to a truthy value, else ConfigError
  - the `required` metadata is dropped and the tree collapses to just values
  - the result is returned as a nested SimpleNamespace for dot access

Typical usage:

    from infra.configs import load_configs, resolve_agent_identity
    cfg = load_configs()
    print(cfg.source.kind)                  # "yaml"
    print(resolve_agent_identity(cfg))      # AgentIdentity(model_name=..., ...)
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

CONFIGS_DIR = Path(__file__).resolve().parent
DEFAULT_PATH = CONFIGS_DIR / "configs.default.yaml"
OVERRIDE_PATH = CONFIGS_DIR / "configs.yaml"


class ConfigError(ValueError):
    pass


def load_configs(
    default_path: Path = DEFAULT_PATH,
    override_path: Path = OVERRIDE_PATH,
    run_config_path: Path | None = None,
    run_config_data: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """Merge defaults + configs.yaml + optional run-config; later wins.

    Pass either `run_config_path` (read from disk) or `run_config_data`
    (pre-parsed dict — used when the caller needs to strip keys, e.g. the
    reserved task fields from a task-shaped run-config).
    """
    if run_config_path is not None and run_config_data is not None:
        raise ValueError(
            "load_configs: pass run_config_path OR run_config_data, not both"
        )

    defaults = _read_yaml(default_path)
    if not defaults:
        raise ConfigError(f"Default config is empty or missing: {default_path}")

    overrides = _read_yaml(override_path) or {}
    if run_config_data is not None:
        run_overrides: dict[str, Any] = run_config_data
        run_src: Path | str | None = "run_config (in-memory)"
    elif run_config_path is not None:
        run_overrides = _read_yaml(run_config_path) or {}
        run_src = run_config_path
    else:
        run_overrides = {}
        run_src = None

    for src, data in (
        (override_path, overrides),
        (run_src, run_overrides),
    ):
        if not data:
            continue
        shape_errors = _validate_override_shape(defaults, data, path=())
        if shape_errors:
            bullets = "\n  - ".join(shape_errors)
            raise ConfigError(
                f"Issues in {src}:\n  - {bullets}\n"
                f"Check against {default_path.name}."
            )

    combined = _deep_merge_dicts(overrides, run_overrides)
    merged = _merge_leaves(defaults, combined)

    missing = _collect_missing_required(merged, path=())
    if missing:
        bullets = "\n  - ".join(".".join(p) for p in missing)
        raise ConfigError(
            f"Required config values are empty:\n  - {bullets}\n"
            f"Set them in {override_path}."
        )

    return _to_namespace(_extract_values(merged))


# --- internals --------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with open(path) as f:
        return yaml.safe_load(f)


def _is_leaf(node: Any) -> bool:
    return isinstance(node, dict) and "value" in node and "required" in node


def _deep_merge_dicts(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge `top` onto `base` (top wins). Non-dict values replace."""
    if not top:
        return dict(base)
    out: dict[str, Any] = dict(base)
    for k, v in top.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_dicts(out[k], v)
        else:
            out[k] = v
    return out


def _merge_leaves(default: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    override_is_dict = isinstance(override, dict)
    for key, d_val in default.items():
        has_override = override_is_dict and key in override
        o_val = override.get(key) if has_override else None

        if _is_leaf(d_val):
            leaf = dict(d_val)
            if has_override:
                if isinstance(o_val, dict) and "value" in o_val:
                    leaf["value"] = o_val["value"]
                elif d_val.get("free_form") and isinstance(o_val, dict):
                    # free_form leaves accept arbitrary nested dicts verbatim
                    leaf["value"] = o_val
                else:
                    # raw scalar/list/null — including explicit None
                    leaf["value"] = o_val
            result[key] = leaf
        elif isinstance(d_val, dict):
            sub = o_val if isinstance(o_val, dict) else {}
            result[key] = _merge_leaves(d_val, sub)
        else:
            result[key] = d_val
    return result


def _validate_override_shape(
    default: dict[str, Any], override: Any, path: tuple[str, ...]
) -> list[str]:
    """Catch typos: unknown keys, or non-`value` keys inside a leaf override."""
    if not isinstance(override, dict):
        return []
    errors: list[str] = []
    for key, o_val in override.items():
        cur = path + (key,)
        dotted = ".".join(cur)
        if key not in default:
            errors.append(f"unknown key '{dotted}'")
            continue
        d_val = default[key]
        if _is_leaf(d_val):
            if d_val.get("free_form"):
                continue  # arbitrary shape allowed
            if isinstance(o_val, dict):
                extra = set(o_val) - {"value"}
                if extra:
                    errors.append(
                        f"leaf '{dotted}' override has unexpected keys: {sorted(extra)}"
                    )
        elif isinstance(d_val, dict):
            errors.extend(_validate_override_shape(d_val, o_val, cur))
    return errors


def _collect_missing_required(
    node: dict[str, Any], path: tuple[str, ...]
) -> list[tuple[str, ...]]:
    missing: list[tuple[str, ...]] = []
    for key, val in node.items():
        cur = path + (key,)
        if _is_leaf(val):
            if val.get("required") and not val.get("value"):
                missing.append(cur)
        elif isinstance(val, dict):
            missing.extend(_collect_missing_required(val, cur))
    return missing


def _extract_values(node: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in node.items():
        if _is_leaf(val):
            out[key] = val["value"]
        elif isinstance(val, dict):
            out[key] = _extract_values(val)
        else:
            out[key] = val
    return out


def _to_namespace(d: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        **{k: _to_namespace(v) if isinstance(v, dict) else v for k, v in d.items()}
    )


# --- scaffolding ------------------------------------------------------------


def ensure_overrides_present(
    required: list[str | tuple[str, ...]] | dict[str, Any],
    *,
    context: str = "",
    override_path: Path = OVERRIDE_PATH,
) -> bool:
    """For every required path not yet present in configs.yaml: print a
    warning, prompt y/N, and on yes write explicit null entries. Returns
    True iff entries were written — caller should abort so the user can
    fill them in before re-running.

    `required` accepts either form (or a mix in a list):
      - a list of paths, dotted strings or tuples:
            ["claude_web.model", ("chatgpt_web", "project_id")]
      - a dict mirroring the config layout (any non-dict leaf is a path):
            {"claude_web": {"model": None}}

    `context` is prepended to the warning header (e.g. "Preflight for
    provider 'chatgpt'") so the user knows *why* these keys matter.
    """
    paths = _normalize_required(required)
    seen: list[tuple[str, ...]] = []
    for p in paths:
        if p not in seen:
            seen.append(p)
    missing = [p for p in seen if not _is_override_set(p, override_path)]
    if not missing:
        return False

    prefix = f"{context}: " if context else ""
    print(
        f"\n{prefix}{len(missing)} required override(s) not set in "
        f"{override_path.name}:"
    )
    for p in missing:
        print(f"  - {'.'.join(p)}")
    try:
        answer = (
            input(
                f"\nAdd these as null entries to {override_path.name} so you can "
                f"fill them in? [y/N]: "
            )
            .strip()
            .lower()
        )
    except EOFError:
        # Non-interactive — skip silently rather than edit a file unprompted.
        return False
    if answer not in {"y", "yes"}:
        return False
    _write_null_overrides(missing, override_path)
    print(
        f"Added {len(missing)} null entries to {override_path}. Edit the "
        f"file to set real values, then re-run."
    )
    return True


def _normalize_required(
    spec: list[str | tuple[str, ...]] | dict[str, Any],
) -> list[tuple[str, ...]]:
    if isinstance(spec, dict):
        return list(_walk_dict_paths(spec, ()))
    if isinstance(spec, list):
        out: list[tuple[str, ...]] = []
        for item in spec:
            if isinstance(item, str):
                out.append(tuple(item.split(".")))
            elif isinstance(item, tuple) and all(isinstance(x, str) for x in item):
                out.append(item)
            else:
                raise TypeError(
                    f"required list items must be 'a.b' strings or tuples of "
                    f"strings; got {item!r}"
                )
        return out
    raise TypeError(f"required must be a list or dict; got {type(spec).__name__}")


def _walk_dict_paths(node: dict[str, Any], path: tuple[str, ...]):
    for key, val in node.items():
        cur = path + (key,)
        if isinstance(val, dict):
            yield from _walk_dict_paths(val, cur)
        else:
            yield cur


def _is_override_set(path: tuple[str, ...], override_path: Path) -> bool:
    """Is this nested key already present (at any value, including null)
    in configs.yaml?"""
    node: Any = _read_yaml(override_path) or {}
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True


def _write_null_overrides(paths: list[tuple[str, ...]], override_path: Path) -> None:
    """Add explicit null entries at the given nested key paths to
    configs.yaml. The leading comment block of the file is preserved;
    everything past it is rewritten from the parsed structure, so any
    inline comments below the header will be lost."""
    if not paths:
        return
    existing = _read_yaml(override_path) or {}
    for p in paths:
        node = existing
        for key in p[:-1]:
            if not isinstance(node.get(key), dict):
                node[key] = {}
            node = node[key]
        leaf = p[-1]
        if leaf not in node:
            node[leaf] = None

    header = _extract_leading_comments(override_path)
    body = (
        yaml.safe_dump(existing, default_flow_style=False, sort_keys=False)
        if existing
        else ""
    )
    with open(override_path, "w") as f:
        if header:
            f.write(header)
            if not header.endswith("\n"):
                f.write("\n")
        f.write(body)


def _extract_leading_comments(path: Path) -> str:
    if not path.exists():
        return ""
    header: list[str] = []
    for line in path.read_text().splitlines(keepends=True):
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            header.append(line)
        else:
            break
    return "".join(header)


if __name__ == "__main__":
    import json

    cfg = load_configs()

    def _dump(ns: SimpleNamespace) -> dict[str, Any]:
        return {
            k: _dump(v) if isinstance(v, SimpleNamespace) else v
            for k, v in vars(ns).items()
        }

    print(json.dumps(_dump(cfg), indent=2, default=str))
