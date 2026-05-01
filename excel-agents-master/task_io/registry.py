"""Build TaskSource / AttemptSink instances from a loaded cfg namespace.

Both builders take the full `cfg` (not just the `source`/`sink` subtree) so
backends like `postgres_s3` can reach other top-level blocks (`database`,
`paths`, `agent`, `aws`) without re-plumbing arguments.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from infra.configs import resolve_agent_identity

from .base import AttemptSink, TaskSource

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return (_REPO_ROOT / p).resolve()


def _resolve_db_url(database_cfg: SimpleNamespace | None) -> str:
    if database_cfg is None:
        return ""
    direct = getattr(database_cfg, "url", "") or ""
    if direct:
        return direct
    env_name = getattr(database_cfg, "url_env", "") or ""
    if env_name:
        return os.environ.get(env_name, "") or ""
    return ""


def _resolve_from_value_or_env(
    cfg: SimpleNamespace | None, value_key: str, env_key: str
) -> str | None:
    """Direct `value_key` wins; otherwise read env var named by `env_key`.
    Returns None if neither yields a non-empty string, so boto3 can fall
    back to its default credential chain."""
    if cfg is None:
        return None
    direct = getattr(cfg, value_key, "") or ""
    if direct:
        return direct
    env_name = getattr(cfg, env_key, "") or ""
    if env_name:
        v = os.environ.get(env_name, "") or ""
        if v:
            return v
    return None


# Valid (kind, schema) combinations. `None` schema means the kind doesn't
# use the schema slot (an explicit schema will raise). Kinds that need a
# schema enumerate every accepted value — listing them here lets the error
# messages on typos point at real alternatives rather than a dump of
# unrelated backends.
_VALID_SOURCE_SCHEMAS: dict[str, set[str | None]] = {
    "yaml": {None},
    "postgres_s3": {"bizbench"},
}
_VALID_SINK_SCHEMAS: dict[str, set[str | None]] = {
    "local": {None},
    "postgres_s3": {"bizbench"},
}


def _validate_kind_schema(
    kind_name: str,  # "source.kind" / "sink.kind"
    kind: str,
    schema: str | None,
    valid: dict[str, set[str | None]],
) -> None:
    """Enforce that (kind, schema) is a recognized combination.

    Three failure modes, each with an actionable message:
    1. Unknown kind entirely → list the kinds we know about.
    2. Kind doesn't take a schema but one was set → name the slot.
    3. Kind requires a schema but the given one is unknown/missing →
       list the schemas this kind accepts.
    """
    if kind not in valid:
        raise ValueError(
            f"Unknown {kind_name}: {kind!r}. " f"Available: {sorted(valid.keys())}"
        )
    accepted = valid[kind]
    if accepted == {None}:
        if schema not in (None, ""):
            slot = kind_name.replace(".kind", ".schema")
            raise ValueError(
                f"{slot} is not applicable when {kind_name}={kind!r}; "
                f"got schema={schema!r}. Omit it or set to null."
            )
        return
    if schema in (None, ""):
        slot = kind_name.replace(".kind", ".schema")
        non_none = sorted(s for s in accepted if s is not None)
        raise ValueError(
            f"{slot} is required when {kind_name}={kind!r}. " f"Available: {non_none}"
        )
    if schema not in accepted:
        slot = kind_name.replace(".kind", ".schema")
        non_none = sorted(s for s in accepted if s is not None)
        raise ValueError(
            f"Unknown {slot} {schema!r} for {kind_name}={kind!r}. "
            f"Available: {non_none}"
        )


def build_source(cfg: SimpleNamespace) -> TaskSource:
    kind = cfg.source.kind
    schema = getattr(cfg.source, "schema", None)
    _validate_kind_schema("source.kind", kind, schema, _VALID_SOURCE_SCHEMAS)

    if kind == "yaml":
        from .sources.yaml_source import YamlTaskSource

        return YamlTaskSource(yaml_path=_resolve(cfg.source.yaml_path))

    if kind == "postgres_s3" and schema == "bizbench":
        from .sources.postgres_s3 import BizbenchPostgresS3TaskSource

        db_url = _resolve_db_url(getattr(cfg, "database", None))
        scratch_dir = _resolve(cfg.paths.scratch_dir)
        filters = cfg.source.filters
        aws_cfg = getattr(cfg, "aws", None)
        region = getattr(aws_cfg, "region", None) if aws_cfg is not None else None
        access_key = _resolve_from_value_or_env(
            aws_cfg, "access_key_id", "access_key_id_env"
        )
        secret_key = _resolve_from_value_or_env(
            aws_cfg, "secret_access_key", "secret_access_key_env"
        )
        session_token = _resolve_from_value_or_env(
            aws_cfg, "session_token", "session_token_env"
        )
        identity = resolve_agent_identity(cfg)
        return BizbenchPostgresS3TaskSource(
            db_url=db_url,
            scratch_dir=scratch_dir,
            agent_model_name=identity.model_name,
            prompt_version=cfg.agent.prompt_version,
            task_ids=list(getattr(filters, "task_ids", []) or []),
            task_sources=list(getattr(filters, "task_sources", []) or []),
            skip_deprecated=bool(getattr(filters, "skip_deprecated", True)),
            skip_already_attempted=bool(
                getattr(filters, "skip_already_attempted", True)
            ),
            aws_region=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )

    # _validate_kind_schema already covered every rejection path; reaching
    # here means the dispatch table is out of sync with the if-ladder.
    raise AssertionError(
        f"Unhandled (source.kind, source.schema) = ({kind!r}, {schema!r})"
    )


def build_sink(cfg: SimpleNamespace) -> AttemptSink:
    kind = cfg.sink.kind
    schema = getattr(cfg.sink, "schema", None)
    _validate_kind_schema("sink.kind", kind, schema, _VALID_SINK_SCHEMAS)

    if kind == "local":
        from .sinks.local_sink import LocalAttemptSink

        return LocalAttemptSink(output_dir=_resolve(cfg.sink.output_dir))

    if kind == "postgres_s3" and schema == "bizbench":
        from .sinks.postgres_s3 import BizbenchPostgresS3AttemptSink

        db_url = _resolve_db_url(getattr(cfg, "database", None))
        aws_cfg = getattr(cfg, "aws", None)
        region = getattr(aws_cfg, "region", None) if aws_cfg is not None else None
        access_key = _resolve_from_value_or_env(
            aws_cfg, "access_key_id", "access_key_id_env"
        )
        secret_key = _resolve_from_value_or_env(
            aws_cfg, "secret_access_key", "secret_access_key_env"
        )
        session_token = _resolve_from_value_or_env(
            aws_cfg, "session_token", "session_token_env"
        )
        s3_bucket = getattr(aws_cfg, "s3_bucket", None) or "biz-bench"
        s3_prefix = getattr(aws_cfg, "s3_prefix", None) or "BizbenchV1/attempts"
        identity = resolve_agent_identity(cfg)
        return BizbenchPostgresS3AttemptSink(
            db_url=db_url,
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix,
            agent_folder=identity.agent_folder,
            agent_model_name=identity.model_name,
            agent_model_type=identity.agent_model_type,
            prompt_version=cfg.agent.prompt_version,
            aws_region=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )

    raise AssertionError(f"Unhandled (sink.kind, sink.schema) = ({kind!r}, {schema!r})")
