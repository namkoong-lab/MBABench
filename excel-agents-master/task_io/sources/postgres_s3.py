"""Postgres + S3 task source.

* `PostgresS3TaskSource` — generic. Knows only about one tasks table:
  where it lives (db_url), its shape (TaskSchema: table + id / name /
  files columns, plus any extra columns to surface in metadata), and an
  optional primary-key filter (`task_ids`). Subclasses extend behavior
  through three hooks:
      _extra_where()         -> add WHERE clauses
      _starting_files_dir()  -> customize scratch layout
      _metadata_for()        -> customize TaskSpec metadata

* `BizbenchPostgresS3TaskSource` — Bizbench-wired subclass. Adds the
  `task_sources` partition filter, the `skip_deprecated` soft-delete
  filter, and the `skip_already_attempted` join against `task_attempts`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import boto3
import botocore.exceptions
import psycopg2
import psycopg2.extras
from psycopg2 import sql

from ..base import TaskSpec

logger = logging.getLogger(__name__)


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Not an s3:// URI: {uri!r}")
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not bucket or not key:
        raise ValueError(f"Malformed s3 URI: {uri!r}")
    return bucket, key


@dataclass(frozen=True)
class TaskSchema:
    """Describes the tasks table this source reads from.

    `extra_cols` are SELECTed and exposed verbatim in `TaskSpec.metadata`.
    Columns referenced only in WHERE clauses (subclass `_extra_where()`)
    don't need to be listed here — WHERE can reference any table column.
    """

    table: str
    id_col: str
    name_col: str
    files_col: str
    extra_cols: tuple[str, ...] = ()


class PostgresS3TaskSource:
    def __init__(
        self,
        *,
        db_url: str,
        scratch_dir: Path | str,
        task_schema: TaskSchema,
        task_ids: list[int] | None = None,
        aws_region: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
    ):
        if not db_url:
            raise ValueError(
                "PostgresS3TaskSource: db_url is empty. Set database.url in "
                "configs.yaml or export the env var named in database.url_env."
            )
        self.db_url = db_url
        self.scratch_dir = Path(scratch_dir)
        self.task_schema = task_schema
        self.task_ids = list(task_ids or [])

        self._conn: psycopg2.extensions.connection | None = None
        # Any unset kwarg drops out — the Bizbench subclass enforces that
        # creds are explicitly provided, so there is no silent fallback
        # to boto3's default credential chain in the normal path.
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
        """Verify AWS creds work before any task download. Logs the caller
        identity so operators can confirm which AWS account they're running
        against. The source doesn't know which bucket a task will pull
        from (task rows carry `s3://.../` URIs), so we can't HEAD-check a
        bucket here — just validate the credentials themselves.

        Raises ValueError with an actionable message on failure — the
        runner catches ValueError at build_source() and exits cleanly."""
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

    # --- extension points --------------------------------------------------

    def _starting_files_dir(self, task_id: Any) -> Path:
        """Directory for one task's downloaded S3 files."""
        return self.scratch_dir / f"task_id={task_id}" / "starting_files"

    def _extra_where(self) -> list[tuple[sql.Composable, list]]:
        """Hook: return (clause, params) pairs appended to the WHERE clause.
        Each clause may reference the tasks-table alias `t`."""
        return []

    def _metadata_for(self, row: dict) -> dict:
        ts = self.task_schema
        meta: dict[str, Any] = {
            "source_kind": "postgres_s3",
            "db_task_id": row[ts.id_col],
            "overrides": {},
        }
        for col in ts.extra_cols:
            meta[col] = row.get(col)
        return meta

    # --- internals ---------------------------------------------------------

    def _connect(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.db_url)
        return self._conn

    def _select_columns(self) -> list[str]:
        ts = self.task_schema
        cols = [ts.id_col, ts.name_col, ts.files_col]
        for c in ts.extra_cols:
            if c not in cols:
                cols.append(c)
        return cols

    def _build_query(self) -> tuple[sql.Composed, list]:
        ts = self.task_schema
        ident = sql.Identifier

        select_cols = sql.SQL(", ").join(
            sql.SQL("t.") + ident(c) for c in self._select_columns()
        )
        where_parts: list[sql.Composable] = []
        params: list[Any] = []

        if self.task_ids:
            where_parts.append(
                sql.SQL("t.{col} = ANY(%s)").format(col=ident(ts.id_col))
            )
            params.append(self.task_ids)

        for clause, extra_params in self._extra_where():
            where_parts.append(clause)
            params.extend(extra_params)

        query = sql.SQL("SELECT {cols} FROM {tbl} t").format(
            cols=select_cols, tbl=ident(ts.table)
        )
        if where_parts:
            query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where_parts)
        query += sql.SQL(" ORDER BY t.{col}").format(col=ident(ts.id_col))
        return query, params

    def _select_tasks(self) -> list[dict]:
        query, params = self._build_query()
        conn = self._connect()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return list(cur.fetchall())

    def _download_starting_files(self, task_id, uris) -> list[Path]:
        task_dir = self._starting_files_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        resolved: list[Path] = []
        for uri in uris or []:
            bucket, key = _parse_s3_uri(uri)
            dest = task_dir / Path(key).name
            if dest.exists():
                logger.info(f"S3 download skipped (cached): {dest}")
            else:
                logger.info(f"S3 download s3://{bucket}/{key} -> {dest}")
                self._s3.download_file(bucket, key, str(dest))
            resolved.append(dest)
        return resolved

    # --- public API --------------------------------------------------------

    def iter_tasks(self) -> Iterator[TaskSpec]:
        rows = self._select_tasks()
        logger.info(
            f"{type(self).__name__} matched {len(rows)} task(s) "
            f"(ids={self.task_ids or 'any'})"
        )
        ts = self.task_schema
        for row in rows:
            tid = row[ts.id_col]
            uris = row.get(ts.files_col) or []
            if not uris:
                logger.warning(
                    f"Task id={tid} name={row[ts.name_col]!r} has no "
                    f"{ts.files_col}; skipping."
                )
                continue
            local_files = self._download_starting_files(tid, uris)
            yield TaskSpec(
                task_id=str(tid),
                task_name=row[ts.name_col],
                upload_files=local_files,
                solution_name=None,
                metadata=self._metadata_for(row),
            )

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None


# ----- Bizbench-specific subclass -------------------------------------------

BIZBENCH_TASK_SCHEMA = TaskSchema(
    table="tasks",
    id_col="id",
    name_col="task_name",
    files_col="task_starting_files",
    extra_cols=("task_source", "old_id"),
)

BIZBENCH_ATTEMPTS_TABLE = "task_attempts"
BIZBENCH_ATTEMPTS_TASK_ID_COL = "task_id"
BIZBENCH_ATTEMPTS_AGENT_COL = "agent_model_name"
BIZBENCH_ATTEMPTS_PV_COL = "prompt_version"
BIZBENCH_ATTEMPTS_FAILED_COL = "agent_failed"
BIZBENCH_ATTEMPTS_DEPRECATED_COL = "deprecated"
BIZBENCH_TASKS_DEPRECATED_COL = "deprecated"
BIZBENCH_TASKS_SOURCE_COL = "task_source"


class BizbenchPostgresS3TaskSource(PostgresS3TaskSource):
    """Bizbench-wired source.

    Adds three filters via `_extra_where()`:
      * `task_sources`           — WHERE `tasks.task_source = ANY(%s)`
      * `skip_deprecated`        — WHERE `tasks.deprecated IS NOT TRUE`
      * `skip_already_attempted` — NOT EXISTS against `task_attempts` on
        (agent_model_name, prompt_version) with agent_failed=FALSE,
        deprecated=FALSE.

    Also overrides `_starting_files_dir` to use the
    `{scratch_dir}/gui/task_id={id}/starting_files/` layout.
    """

    def __init__(
        self,
        *,
        db_url: str,
        scratch_dir: Path | str,
        agent_model_name: str,
        prompt_version: int | str | None,
        task_ids: list[int] | None = None,
        task_sources: list[str] | None = None,
        skip_deprecated: bool = True,
        skip_already_attempted: bool = True,
        aws_region: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
    ):
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
                "BizbenchPostgresS3TaskSource: aws.access_key_id and "
                "aws.secret_access_key are required. Set them in "
                "configs.yaml, or export the env vars named by "
                "aws.access_key_id_env / aws.secret_access_key_env. The "
                "boto3 default credential chain (~/.aws/credentials, IAM "
                "role, etc.) is intentionally NOT consulted."
            )
        super().__init__(
            db_url=db_url,
            scratch_dir=scratch_dir,
            task_schema=BIZBENCH_TASK_SCHEMA,
            task_ids=task_ids,
            aws_region=aws_region,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
        )
        self.agent_model_name = agent_model_name
        self.prompt_version = prompt_version
        self.task_sources = list(task_sources or [])
        self.skip_deprecated = skip_deprecated
        self.skip_already_attempted = skip_already_attempted

    @staticmethod
    def _offer_db_url_scaffolding() -> bool:
        """Ask infra.configs to warn about missing `database.url` and offer
        to scaffold a null entry. Returns True iff the user accepted and
        the caller should abort so they can fill it in.

        Degrades to False if infra.configs isn't on sys.path (e.g., the
        source is being used outside the gui-agents project)."""
        try:
            from infra.configs import ensure_overrides_present
        except ImportError:
            return False
        return ensure_overrides_present(
            ["database.url"],
            context=(
                "BizbenchPostgresS3TaskSource needs a DB connection, but "
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
                "BizbenchPostgresS3TaskSource needs AWS credentials, "
                "but aws.access_key_id / aws.secret_access_key are empty "
                "and the env vars named in aws.access_key_id_env / "
                "aws.secret_access_key_env are not set. The boto3 default "
                "credential chain (~/.aws/credentials, IAM role, etc.) "
                "is intentionally NOT consulted"
            ),
        )

    def _starting_files_dir(self, task_id) -> Path:
        return self.scratch_dir / "gui" / f"task_id={task_id}" / "starting_files"

    def _extra_where(self) -> list[tuple[sql.Composable, list]]:
        ident = sql.Identifier
        out: list[tuple[sql.Composable, list]] = []

        if self.skip_deprecated:
            out.append(
                (
                    sql.SQL("(t.{col} IS NULL OR t.{col} = FALSE)").format(
                        col=ident(BIZBENCH_TASKS_DEPRECATED_COL)
                    ),
                    [],
                )
            )

        if self.task_sources:
            out.append(
                (
                    sql.SQL("t.{col} = ANY(%s)").format(
                        col=ident(BIZBENCH_TASKS_SOURCE_COL)
                    ),
                    [self.task_sources],
                )
            )

        if self.skip_already_attempted:
            clauses: list[sql.Composable] = [
                sql.SQL("a.{c} = t.{tc}").format(
                    c=ident(BIZBENCH_ATTEMPTS_TASK_ID_COL),
                    tc=ident(self.task_schema.id_col),
                ),
                sql.SQL("a.{c} = %s").format(c=ident(BIZBENCH_ATTEMPTS_AGENT_COL)),
                sql.SQL("a.{c} = %s").format(c=ident(BIZBENCH_ATTEMPTS_PV_COL)),
                sql.SQL("a.{c} = FALSE").format(c=ident(BIZBENCH_ATTEMPTS_FAILED_COL)),
                sql.SQL("a.{c} = FALSE").format(
                    c=ident(BIZBENCH_ATTEMPTS_DEPRECATED_COL)
                ),
            ]
            clause = sql.SQL("NOT EXISTS (SELECT 1 FROM {tbl} a WHERE {cs})").format(
                tbl=ident(BIZBENCH_ATTEMPTS_TABLE),
                cs=sql.SQL(" AND ").join(clauses),
            )
            out.append((clause, [self.agent_model_name, self.prompt_version]))

        return out
