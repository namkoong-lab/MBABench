"""Postgres + S3 attempt sink.

Symmetric with `task_io/sources/postgres_s3.py`:

* `PostgresS3AttemptSink` — generic. Uploads an AttemptResult's files to
  S3 and inserts one row into an attempts table. Knows only about one
  schema (AttemptSchema) and two hooks subclasses override:
      _s3_base_key(result, timestamp) -> str
      _attempt_values(result, uris)   -> dict[col -> value]

* `BizbenchPostgresS3AttemptSink` — Bizbench-wired subclass. Hardcodes
  the `task_attempts` schema and the Hive-style S3 layout used by
  cli-agents' `auto_batch_runner.py`. `cost` is always NULL (GUI runs
  are subscription-based); failed/timeout runs are still inserted with
  `agent_failed=true`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
import botocore.exceptions
import psycopg2
import psycopg2.extras
from psycopg2 import sql

from ..base import AttemptResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttemptSchema:
    """Describes the attempts table this sink writes to.

    `columns` is the INSERT column list; `json_columns` names the subset
    that must be wrapped in psycopg2.extras.Json so list/dict values are
    serialized to the JSON/JSONB column type rather than pg arrays.
    """

    table: str
    columns: tuple[str, ...]
    json_columns: frozenset[str] = frozenset()


class PostgresS3AttemptSink:
    def __init__(
        self,
        *,
        db_url: str,
        s3_bucket: str,
        s3_prefix: str,
        attempt_schema: AttemptSchema,
        aws_region: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
    ):
        if not db_url:
            raise ValueError(
                "PostgresS3AttemptSink: db_url is empty. Set database.url "
                "in configs.yaml or export the env var named in "
                "database.url_env."
            )
        if not s3_bucket:
            raise ValueError("PostgresS3AttemptSink: s3_bucket is empty.")
        self.db_url = db_url
        self.s3_bucket = s3_bucket
        self.s3_prefix = (s3_prefix or "").rstrip("/")
        self.attempt_schema = attempt_schema

        self._conn: psycopg2.extensions.connection | None = None
        client_kwargs: dict[str, Any] = {}
        if aws_region:
            client_kwargs["region_name"] = aws_region
        if aws_access_key_id:
            client_kwargs["aws_access_key_id"] = aws_access_key_id
        if aws_secret_access_key:
            client_kwargs["aws_secret_access_key"] = aws_secret_access_key
        if aws_session_token:
            client_kwargs["aws_session_token"] = aws_session_token
        self._s3 = boto3.client("s3", **client_kwargs)
        self._sts = boto3.client("sts", **client_kwargs)

        self._preflight_aws()

    # --- preflight ---------------------------------------------------------

    def _preflight_aws(self) -> None:
        """Verify AWS creds work and the target bucket is reachable BEFORE
        any engine work happens. Logs the caller identity so operators can
        confirm which AWS account they're running against.

        Raises ValueError with an actionable message on failure — the
        runner catches ValueError at build_sink() and exits cleanly."""
        try:
            ident = self._sts.get_caller_identity()
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ) as e:
            raise ValueError(
                f"AWS preflight failed: sts.get_caller_identity() errored "
                f"({type(e).__name__}: {e}). Check aws.access_key_id / "
                f"aws.secret_access_key in configs.yaml."
            ) from e
        logger.info(
            f"AWS identity: account={ident.get('Account')} " f"arn={ident.get('Arn')}"
        )
        try:
            self._s3.head_bucket(Bucket=self.s3_bucket)
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ) as e:
            raise ValueError(
                f"AWS preflight failed: head_bucket({self.s3_bucket!r}) "
                f"errored ({type(e).__name__}: {e}). Confirm the IAM user "
                f"has s3:ListBucket on {self.s3_bucket!r}, the bucket "
                f"exists, and aws.region matches the bucket's region."
            ) from e

    # --- extension points --------------------------------------------------

    def _s3_base_key(self, result: AttemptResult, timestamp: str) -> str:
        """Key prefix (under `s3_bucket`) for this attempt's uploaded files.
        Default layout: `{s3_prefix}/task_id={id}/{timestamp}`."""
        return f"{self.s3_prefix}/task_id={result.task_id}/{timestamp}"

    def _attempt_values(
        self,
        result: AttemptResult,
        attempt_file_uris: list[str],
        prompt_file_uris: list[str],
    ) -> dict[str, Any]:
        """Map an AttemptResult to column-value pairs. Keys must cover every
        column in `self.attempt_schema.columns`. Subclasses must override."""
        raise NotImplementedError

    # --- internals ---------------------------------------------------------

    def _connect(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.db_url)
        return self._conn

    def _attempt_files_to_upload(self, result: AttemptResult) -> list[Path]:
        out: list[Path] = []
        for p in (result.solution_file, result.log_file):
            if p is None:
                continue
            p = Path(p)
            if not p.exists():
                logger.warning(f"Sink: skipping missing file {p}")
                continue
            out.append(p)
        return out

    def _prompt_files_to_upload(self, result: AttemptResult) -> list[Path]:
        out: list[Path] = []
        for p in result.prompt_files or []:
            p = Path(p)
            if not p.exists():
                logger.warning(f"Sink: skipping missing prompt file {p}")
                continue
            out.append(p)
        return out

    def _upload_files(self, local_files: list[Path], base_key: str) -> list[str]:
        uris: list[str] = []
        for local in local_files:
            key = f"{base_key}_{local.name}"
            logger.info(f"S3 upload {local} -> s3://{self.s3_bucket}/{key}")
            self._s3.upload_file(str(local), self.s3_bucket, key)
            uris.append(f"s3://{self.s3_bucket}/{key}")
        return uris

    def _insert_row(self, values: dict[str, Any]) -> None:
        schema = self.attempt_schema
        missing = [c for c in schema.columns if c not in values]
        if missing:
            raise ValueError(f"_attempt_values missing required columns: {missing}")
        ident = sql.Identifier
        stmt = sql.SQL("INSERT INTO {tbl} ({cols}) VALUES ({ph})").format(
            tbl=ident(schema.table),
            cols=sql.SQL(", ").join(ident(c) for c in schema.columns),
            ph=sql.SQL(", ").join(sql.Placeholder() for _ in schema.columns),
        )
        params = [
            (
                psycopg2.extras.Json(values[c])
                if c in schema.json_columns and values[c] is not None
                else values[c]
            )
            for c in schema.columns
        ]
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(stmt, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # --- public API --------------------------------------------------------

    def publish(self, result: AttemptResult) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = self._s3_base_key(result, timestamp)
        attempt_uris = self._upload_files(self._attempt_files_to_upload(result), base)
        prompt_uris = self._upload_files(self._prompt_files_to_upload(result), base)
        values = self._attempt_values(result, attempt_uris, prompt_uris)
        self._insert_row(values)
        logger.info(
            f"Sink: recorded attempt for task_id={result.task_id} "
            f"status={result.status} attempt_files={len(attempt_uris)} "
            f"prompt_files={len(prompt_uris)}"
        )

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None


# ----- Bizbench-specific subclass -------------------------------------------

BIZBENCH_ATTEMPT_SCHEMA = AttemptSchema(
    table="task_attempts",
    columns=(
        "task_id",
        "agent_model_name",
        "agent_model_type",
        "attempt_files",
        "prompt_files",
        "start_time",
        "end_time",
        "time_taken_min",
        "cost",
        "prompt_version",
        "agent_failed",
        "agent_failed_reason",
        "deprecated",
    ),
    json_columns=frozenset({"attempt_files", "prompt_files"}),
)


class BizbenchPostgresS3AttemptSink(PostgresS3AttemptSink):
    """Bizbench-wired sink.

    S3 layout (mirrors cli-agents-master/auto_batch_runner.py):
        {s3_prefix}/{agent_folder}/task_source={src}/task_id={id}/{ts}_{name}

    The source (`BizbenchPostgresS3TaskSource`) populates
    `spec.metadata["task_source"]` and `spec.metadata["db_task_id"]`; the
    runner threads those through as `result.extra["task_metadata"]` so
    they land here.
    """

    def __init__(
        self,
        *,
        db_url: str,
        s3_bucket: str,
        s3_prefix: str,
        agent_folder: str,
        agent_model_name: str,
        agent_model_type: str = "gui",
        prompt_version: int | str | None,
        aws_region: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
    ):
        if not agent_folder:
            raise ValueError(
                "BizbenchPostgresS3AttemptSink: agent_folder is required. "
                "This is derived from resolve_agent_identity(cfg) — "
                "an empty value means the resolver returned an invalid "
                "AgentIdentity (check infra/configs/agent_identity.py)."
            )
        if not agent_model_name:
            raise ValueError(
                "BizbenchPostgresS3AttemptSink: agent_model_name is required. "
                "This is derived from resolve_agent_identity(cfg) — "
                "an empty value means the resolver returned an invalid "
                "AgentIdentity (check infra/configs/agent_identity.py)."
            )
        if not db_url and self._offer_db_url_scaffolding():
            raise ValueError(
                "database.url was scaffolded as null in configs.yaml — "
                "fill it in (or export the env var named in database.url_env) "
                "and re-run."
            )
        if not (aws_access_key_id and aws_secret_access_key):
            if self._offer_aws_creds_scaffolding():
                raise ValueError(
                    "aws credentials were scaffolded as null in "
                    "configs.yaml — fill them in (or export the env vars "
                    "named in aws.access_key_id_env / "
                    "aws.secret_access_key_env) and re-run."
                )
            raise ValueError(
                "BizbenchPostgresS3AttemptSink: aws.access_key_id and "
                "aws.secret_access_key are required. Set them in "
                "configs.yaml, or export the env vars named by "
                "aws.access_key_id_env / aws.secret_access_key_env. The "
                "boto3 default credential chain (~/.aws/credentials, IAM "
                "role, etc.) is intentionally NOT consulted."
            )
        super().__init__(
            db_url=db_url,
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix,
            attempt_schema=BIZBENCH_ATTEMPT_SCHEMA,
            aws_region=aws_region,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
        )
        self.agent_folder = agent_folder
        self.agent_model_name = agent_model_name
        self.agent_model_type = agent_model_type
        self.prompt_version = prompt_version

    @staticmethod
    def _offer_db_url_scaffolding() -> bool:
        try:
            from infra.configs import ensure_overrides_present
        except ImportError:
            return False
        return ensure_overrides_present(
            ["database.url"],
            context=(
                "BizbenchPostgresS3AttemptSink needs a DB connection, but "
                "database.url is empty and the env var named in "
                "database.url_env is not set"
            ),
        )

    @staticmethod
    def _offer_aws_creds_scaffolding() -> bool:
        """Prompt to scaffold aws.access_key_id / aws.secret_access_key in
        configs.yaml when neither direct values nor the env vars named by
        aws.*_env yielded credentials.

        We intentionally do NOT fall back to boto3's default credential
        chain: credentials must come from configs.yaml or the explicitly
        named env vars. Returns True iff entries were scaffolded and the
        caller should abort."""
        try:
            from infra.configs import ensure_overrides_present
        except ImportError:
            return False
        return ensure_overrides_present(
            ["aws.access_key_id", "aws.secret_access_key"],
            context=(
                "BizbenchPostgresS3AttemptSink needs AWS credentials, "
                "but aws.access_key_id / aws.secret_access_key are empty "
                "and the env vars named in aws.access_key_id_env / "
                "aws.secret_access_key_env are not set. The boto3 default "
                "credential chain (~/.aws/credentials, IAM role, etc.) "
                "is intentionally NOT consulted"
            ),
        )

    def _task_metadata(self, result: AttemptResult) -> dict:
        extra = result.extra or {}
        meta = extra.get("task_metadata")
        return meta if isinstance(meta, dict) else {}

    def _s3_base_key(self, result: AttemptResult, timestamp: str) -> str:
        task_source = self._task_metadata(result).get("task_source") or "unknown"
        return (
            f"{self.s3_prefix}/{self.agent_folder}"
            f"/task_source={task_source}/task_id={result.task_id}/{timestamp}"
        )

    def _attempt_values(
        self,
        result: AttemptResult,
        attempt_file_uris: list[str],
        prompt_file_uris: list[str],
    ) -> dict[str, Any]:
        meta = self._task_metadata(result)
        db_task_id = meta.get("db_task_id")
        if db_task_id is None:
            try:
                db_task_id = int(result.task_id)
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"BizbenchPostgresS3AttemptSink: task_id must resolve to "
                    f"an int, got {result.task_id!r}. Ensure the source "
                    f"populates spec.metadata['db_task_id'] or yields numeric "
                    f"task_ids (this sink writes to task_attempts.task_id "
                    f"which is INT NOT NULL)."
                ) from e

        start_dt = datetime.fromisoformat(result.started_at)
        end_dt = datetime.fromisoformat(result.finished_at)
        time_taken_min = (result.duration_seconds or 0.0) / 60.0

        agent_failed = result.status != "success"
        agent_failed_reason: str | None = None
        if agent_failed:
            extra = result.extra or {}
            agent_failed_reason = (
                extra.get("error") or extra.get("failure_reason") or result.status
            )

        return {
            "task_id": db_task_id,
            "agent_model_name": self.agent_model_name,
            "agent_model_type": self.agent_model_type,
            "attempt_files": attempt_file_uris,
            "prompt_files": prompt_file_uris,
            "start_time": start_dt,
            "end_time": end_dt,
            "time_taken_min": time_taken_min,
            "cost": None,
            "prompt_version": self.prompt_version,
            "agent_failed": agent_failed,
            "agent_failed_reason": agent_failed_reason,
            "deprecated": False,
        }
