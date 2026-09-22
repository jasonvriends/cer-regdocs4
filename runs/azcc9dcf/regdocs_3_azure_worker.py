#!/usr/bin/env python3
"""Internal one-document Azure Stage 3 worker.

Normal operation goes through ``regdocs_3_azure.py``. This worker preserves the
Azure Content Understanding implementation, artifact reconciliation, hash
verification, large-PDF Content-Range recovery, and ledger writes. Automatic
Azure SDK transport retries are disabled because a repeated submission may be
billable.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import mimetypes
import os
import sqlite3
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from azure.ai.contentunderstanding import ContentUnderstandingClient
    from azure.core.credentials import AzureKeyCredential
    from azure.core.exceptions import HttpResponseError, ServiceRequestError
    from azure.identity import DefaultAzureCredential
except ImportError as exc:
    raise SystemExit(
        "Missing Stage 3 Azure dependency. Install with: "
        "python -m pip install -r regdocs_atlas/requirements.txt"
    ) from exc

from regdocs_paths import (
    ANALYZE_LOCK_PATH,
    CONTENT_UNDERSTANDING_DIR,
    DATABASE_PATH,
    DOWNLOAD_FILES_DIR,
    resolve_stored_path,
    stored_path,
)

SCRIPT_VERSION = "3.6.2"
DEFAULT_API_VERSION = "2025-11-01"
PARSER_VERSION = f"azure-content-understanding-{DEFAULT_API_VERSION}"
DEFAULT_ANALYZER_ID = "prebuilt-layout"
DEFAULT_POLLING_INTERVAL = 3
DEFAULT_MAX_ATTEMPTS = 1
DEFAULT_RETRY_BASE_DELAY = 2.0
DEFAULT_RETRY_MAX_DELAY = 30.0
MAX_PAGES_PER_ANALYSIS = 300

SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".heif",
    ".docx", ".xlsx", ".pptx",
    ".txt", ".html", ".htm", ".md", ".rtf",
    ".xml", ".json", ".csv", ".tsv",
    ".eml", ".msg",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def safe_path_component(value: str) -> str:
    """Return a filesystem-safe component while keeping analyzer/API labels readable."""
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in value.strip())
    return cleaned or "unknown"


def _read_lock_pid(path: Path) -> Optional[int]:
    """Return a valid PID from a Stage 3 lock, or None when its state is uncertain."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return pid


def _pid_is_running(pid: int) -> bool:
    """Return False only when the OS positively reports that *pid* does not exist."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


class StageLock:
    """Exclusive lock preventing concurrent billable Stage 3 workers."""

    def __init__(self, path: Path, *, force: bool = False):
        self.path = path
        self.force = force
        self.owned = False

    def __enter__(self) -> "StageLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.force and self.path.exists():
            self.path.unlink()
        elif self.path.exists():
            existing_pid = _read_lock_pid(self.path)
            if existing_pid is not None and not _pid_is_running(existing_pid):
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                else:
                    print(
                        f"Removing stale analyze lock: {self.path} "
                        f"(PID {existing_pid} is not running).",
                        file=sys.stderr,
                    )
        payload = {"pid": os.getpid(), "created_at": utcnow()}
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            detail = ""
            with contextlib.suppress(OSError):
                detail = self.path.read_text(encoding="utf-8")
            raise RuntimeError(
                f"Analyze lock already exists: {self.path}. Confirm no analyzer is running "
                "before using --force-lock."
                + (f"\nLock contents: {detail}" if detail else "")
            ) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        self.owned = True
        return self

    def __exit__(self, *_: Any) -> None:
        if self.owned:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
        self.owned = False


def atomic_write_text(path: Path, text: str) -> None:
    """Write to <name>.partial, fsync it, then atomically replace the final artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    with partial.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(partial, path)


def is_retryable_error_code(code: Optional[str]) -> bool:
    return str(code or "").upper() in {
        "408", "429", "500", "502", "503", "504", "SERVICE_REQUEST_ERROR"
    }


def retry_delay_seconds(exc: Exception, attempt: int, base_delay: float, max_delay: float) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after is not None:
        try:
            return min(max_delay, max(0.0, float(retry_after)))
        except (TypeError, ValueError):
            pass
    return min(max_delay, base_delay * (2 ** max(0, attempt - 1)))


@dataclass(frozen=True)
class Candidate:
    document_id: str
    file_id: int
    db_path: str
    sha256: str
    extension: Optional[str]
    mime_type: Optional[str]
    size_bytes: Optional[int]


@dataclass
class AnalysisOutcome:
    document_id: str
    file_id: int
    status: str
    operation_id: Optional[str] = None
    api_version: Optional[str] = None
    raw_json_path: Optional[str] = None
    markdown_path: Optional[str] = None
    page_count: Optional[int] = None
    table_count: Optional[int] = None
    section_count: Optional[int] = None
    warning_count: Optional[int] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    elapsed_seconds: Optional[float] = None
    attempt_count: int = 0
    artifact_source: Optional[str] = None
    reconciled_at: Optional[str] = None


def open_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    return con


def _create_analyses_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            document_id TEXT NOT NULL,
            file_id INTEGER NOT NULL,
            file_sha256 TEXT NOT NULL,
            analyzer_id TEXT NOT NULL,
            api_version TEXT NOT NULL,
            operation_id TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            started_at TEXT,
            finished_at TEXT,
            raw_json_path TEXT,
            markdown_path TEXT,
            page_count INTEGER,
            table_count INTEGER,
            section_count INTEGER,
            warning_count INTEGER,
            elapsed_seconds REAL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            artifact_source TEXT,
            reconciled_at TEXT,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(file_id, file_sha256, analyzer_id, api_version),
            FOREIGN KEY (run_id) REFERENCES runs(id),
            FOREIGN KEY (document_id) REFERENCES documents(id),
            FOREIGN KEY (file_id) REFERENCES files(id)
        )
        """
    )


def _create_analyses_indexes(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_analyses_document
            ON analyses(document_id);

        CREATE INDEX IF NOT EXISTS idx_analyses_status
            ON analyses(status);

        CREATE INDEX IF NOT EXISTS idx_analyses_file_analyzer
            ON analyses(file_id, analyzer_id, api_version);
        """
    )


def _has_analysis_identity_index(con: sqlite3.Connection) -> bool:
    wanted = ["file_id", "file_sha256", "analyzer_id", "api_version"]
    for row in con.execute("PRAGMA index_list(analyses)").fetchall():
        if not bool(row[2]):
            continue
        index_name = str(row[1]).replace("'", "''")
        cols = [r[2] for r in con.execute(f"PRAGMA index_info('{index_name}')").fetchall()]
        if cols == wanted:
            return True
    return False


def _rebuild_analyses_table(con: sqlite3.Connection) -> None:
    columns = {str(r[1]) for r in con.execute("PRAGMA table_info(analyses)").fetchall()}
    required_legacy = {
        "id", "run_id", "document_id", "file_id", "file_sha256", "analyzer_id",
        "operation_id", "status", "started_at", "finished_at", "raw_json_path",
        "markdown_path", "page_count", "table_count", "section_count", "warning_count",
        "error_code", "error_message", "created_at", "updated_at",
    }
    missing = sorted(required_legacy - columns)
    if missing:
        raise RuntimeError(
            "Existing analyses table has an unknown/incomplete schema; "
            f"cannot safely migrate automatically. Missing columns: {', '.join(missing)}"
        )

    legacy_name = "analyses__legacy_3_2"
    if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (legacy_name,)
    ).fetchone():
        raise RuntimeError(
            f"Refusing schema migration because temporary table {legacy_name!r} already exists."
        )

    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(f"ALTER TABLE analyses RENAME TO {legacy_name}")
        _create_analyses_table(con)

        api_expr = (
            f"COALESCE(api_version, '{DEFAULT_API_VERSION}')"
            if "api_version" in columns
            else f"'{DEFAULT_API_VERSION}'"
        )
        elapsed_expr = "elapsed_seconds" if "elapsed_seconds" in columns else "NULL"
        attempts_expr = "attempt_count" if "attempt_count" in columns else "0"
        source_expr = "artifact_source" if "artifact_source" in columns else "NULL"
        reconciled_expr = "reconciled_at" if "reconciled_at" in columns else "NULL"

        con.execute(
            f"""
            INSERT OR IGNORE INTO analyses (
                id, run_id, document_id, file_id, file_sha256, analyzer_id, api_version,
                operation_id, status, started_at, finished_at, raw_json_path, markdown_path,
                page_count, table_count, section_count, warning_count, elapsed_seconds,
                attempt_count, artifact_source, reconciled_at, error_code, error_message,
                created_at, updated_at
            )
            SELECT
                id, run_id, document_id, file_id, file_sha256, analyzer_id, {api_expr},
                operation_id, status, started_at, finished_at, raw_json_path, markdown_path,
                page_count, table_count, section_count, warning_count, {elapsed_expr},
                {attempts_expr}, {source_expr}, {reconciled_expr}, error_code, error_message,
                created_at, updated_at
            FROM {legacy_name}
            """
        )
        con.execute(f"DROP TABLE {legacy_name}")
        _create_analyses_indexes(con)
        con.commit()
    except Exception:
        con.rollback()
        raise


def ensure_schema(con: sqlite3.Connection) -> None:
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analyses'"
    ).fetchone()

    if not exists:
        _create_analyses_table(con)
        _create_analyses_indexes(con)
        con.commit()
        return

    columns = {str(r[1]) for r in con.execute("PRAGMA table_info(analyses)").fetchall()}
    if "api_version" not in columns or not _has_analysis_identity_index(con):
        _rebuild_analyses_table(con)
        return

    if "elapsed_seconds" not in columns:
        con.execute("ALTER TABLE analyses ADD COLUMN elapsed_seconds REAL")
    if "attempt_count" not in columns:
        con.execute("ALTER TABLE analyses ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
    if "artifact_source" not in columns:
        con.execute("ALTER TABLE analyses ADD COLUMN artifact_source TEXT")
    if "reconciled_at" not in columns:
        con.execute("ALTER TABLE analyses ADD COLUMN reconciled_at TEXT")

    _create_analyses_indexes(con)
    con.commit()


def create_run(con: sqlite3.Connection, args: argparse.Namespace) -> int:
    now = utcnow()
    params = {
        "db": stored_path(args.db),
        "download_dir": stored_path(args.download_dir) if args.download_dir else None,
        "output_dir": stored_path(args.output_dir),
        "lock_file": stored_path(args.lock_file),
        "endpoint": args.endpoint,
        "auth_mode": "key" if args.key else "default_azure_credential",
        "api_version": args.api_version,
        "polling_interval": args.polling_interval,
        "max_attempts": args.max_attempts,
        "retry_base_delay": args.retry_base_delay,
        "retry_max_delay": args.retry_max_delay,
        "analyzer_id": args.analyzer_id,
        "all": args.all_candidates,
        "limit": args.limit,
        "document_id": args.document_id,
        "force": args.force,
        "reconcile_artifacts": not args.no_reconcile_artifacts,
    }
    cur = con.execute(
        """
        INSERT INTO runs (
            stage, status, started_at, parameters_json, summary_json,
            script_version, parser_version, current_phase, heartbeat_at,
            completed_units, total_units, progress_message
        ) VALUES (?, ?, ?, ?, '{}', ?, ?, ?, ?, 0, NULL, ?)
        """,
        (
            "analyze",
            "RUNNING",
            now,
            json.dumps(params),
            SCRIPT_VERSION,
            f"azure-content-understanding-{args.api_version}",
            "selecting_candidates",
            now,
            "Selecting downloaded files for Azure Content Understanding",
        ),
    )
    con.commit()
    return int(cur.lastrowid)


def finish_run(
    con: sqlite3.Connection,
    run_id: int,
    status: str,
    completed: int,
    total: int,
    succeeded: int,
    failed: int,
    skipped: int,
    pages_succeeded: int,
    analysis_attempts: int,
    elapsed: float,
) -> None:
    now = utcnow()
    summary = {
        "status": status,
        "documents_total": total,
        "documents_completed": completed,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "pages_succeeded": pages_succeeded,
        "analysis_attempts": analysis_attempts,
        "elapsed_seconds": round(elapsed, 2),
    }
    con.execute(
        """
        UPDATE runs
        SET status=?, finished_at=?, summary_json=?, current_phase=?, heartbeat_at=?,
            completed_units=?, total_units=?, progress_message=?,
            successful_requests=?, failed_requests=?
        WHERE id=?
        """,
        (
            status,
            now,
            json.dumps(summary),
            status.lower(),
            now,
            completed,
            total,
            (
                f"Analyze {status}: {succeeded} succeeded, {failed} failed, "
                f"{skipped} skipped, {pages_succeeded} pages"
            ),
            succeeded,
            failed,
            run_id,
        ),
    )
    con.commit()


def update_run_progress(
    con: sqlite3.Connection,
    run_id: int,
    completed: int,
    total: int,
    succeeded: int,
    failed: int,
    skipped: int,
    pages_succeeded: int,
    analysis_attempts: int,
    message: str,
) -> None:
    live_summary = {
        "documents_total": total,
        "documents_completed": completed,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "pages_succeeded": pages_succeeded,
        "analysis_attempts": analysis_attempts,
    }
    con.execute(
        """
        UPDATE runs
        SET heartbeat_at=?, current_phase='analyzing', completed_units=?, total_units=?,
            progress_message=?, successful_requests=?, failed_requests=?, summary_json=?
        WHERE id=?
        """,
        (
            utcnow(), completed, total, message, succeeded, failed,
            json.dumps(live_summary), run_id,
        ),
    )
    con.commit()


def select_candidates(
    con: sqlite3.Connection,
    analyzer_id: str,
    api_version: str,
    limit: Optional[int],
    document_id: Optional[str],
    force: bool,
) -> list[Candidate]:
    where = ["f.is_current = 1"]
    params: list[Any] = []

    if document_id:
        where.append("d.id = ?")
        params.append(document_id)

    if not force:
        where.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM analyses a
                WHERE a.file_id = f.id
                  AND a.file_sha256 = f.sha256
                  AND a.analyzer_id = ?
                  AND a.api_version = ?
                  AND a.status = 'SUCCEEDED'
            )
            """
        )
        params.extend([analyzer_id, api_version])

    sql = f"""
        SELECT
            d.id AS document_id,
            f.id AS file_id,
            f.path AS db_path,
            f.sha256,
            f.extension,
            f.mime_type,
            f.size_bytes
        FROM files f
        JOIN documents d ON d.id = f.document_id
        WHERE {' AND '.join(where)}
        ORDER BY CAST(d.id AS INTEGER), d.id
    """

    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    rows = con.execute(sql, params).fetchall()
    return [
        Candidate(
            document_id=str(r["document_id"]),
            file_id=int(r["file_id"]),
            db_path=str(r["db_path"]),
            sha256=str(r["sha256"]),
            extension=r["extension"],
            mime_type=r["mime_type"],
            size_bytes=r["size_bytes"],
        )
        for r in rows
    ]


def canonical_artifact_paths(
    output_dir: Path,
    analyzer_id: str,
    api_version: str,
    candidate: Candidate,
) -> tuple[Path, Path]:
    analyzer_component = safe_path_component(analyzer_id)
    api_component = safe_path_component(api_version)
    identity = candidate.sha256.lower()
    raw_path = (
        output_dir / "raw" / analyzer_component / api_component /
        candidate.document_id / f"{identity}.json"
    )
    md_path = (
        output_dir / "markdown" / analyzer_component / api_component /
        candidate.document_id / f"{identity}.md"
    )
    return raw_path, md_path


def legacy_artifact_paths(output_dir: Path, candidate: Candidate) -> tuple[Path, Path]:
    identity = candidate.sha256.lower()
    return (
        output_dir / "raw" / candidate.document_id / f"{identity}.json",
        output_dir / "markdown" / candidate.document_id / f"{identity}.md",
    )


def _existing_path(value: Optional[str]) -> Optional[Path]:
    if not value:
        return None
    p = resolve_stored_path(value)
    try:
        p = p.resolve()
    except OSError:
        pass
    return p if p.is_file() else None


def _load_and_validate_result_json_payload(
    data: Any,
    analyzer_id: str,
    api_version: str,
    expected_page_count: Optional[int] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError("top-level Azure result must be a JSON object")

    actual_analyzer = data.get("analyzerId") or data.get("analyzer_id")
    actual_api = data.get("apiVersion") or data.get("api_version")
    if actual_analyzer != analyzer_id:
        raise ValueError(
            f"analyzer mismatch: artifact={actual_analyzer!r}, expected={analyzer_id!r}"
        )
    if actual_api != api_version:
        raise ValueError(
            f"API version mismatch: artifact={actual_api!r}, expected={api_version!r}"
        )

    contents = data.get("contents")
    if not isinstance(contents, list):
        raise ValueError("contents is not a list")
    if not contents:
        raise ValueError("contents is empty")

    markdown_parts: list[str] = []
    page_count = 0
    table_count = 0
    section_count = 0
    for content in contents:
        if not isinstance(content, dict):
            continue
        markdown = content.get("markdown")
        if isinstance(markdown, str) and markdown:
            markdown_parts.append(markdown)
        pages = content.get("pages") or []
        tables = content.get("tables") or []
        sections = content.get("sections") or []
        if isinstance(pages, list):
            page_count += len(pages)
        if isinstance(tables, list):
            table_count += len(tables)
        if isinstance(sections, list):
            section_count += len(sections)

    warnings = data.get("warnings") or []
    warning_count = len(warnings) if isinstance(warnings, list) else 0
    if expected_page_count is not None and page_count != expected_page_count:
        raise ValueError(
            f"page count mismatch: source={expected_page_count}, result={page_count}"
        )
    metadata = {
        "page_count": page_count,
        "table_count": table_count,
        "section_count": section_count,
        "warning_count": warning_count,
        "markdown": "\n\n".join(markdown_parts),
    }
    return data, metadata


def _load_and_validate_result_json(
    raw_path: Path,
    analyzer_id: str,
    api_version: str,
    expected_page_count: Optional[int] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        data = json.loads(raw_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    return _load_and_validate_result_json_payload(
        data,
        analyzer_id,
        api_version,
        expected_page_count,
    )


def find_reconcilable_artifacts(
    con: sqlite3.Connection,
    candidate: Candidate,
    output_dir: Path,
    analyzer_id: str,
    api_version: str,
) -> tuple[Optional[AnalysisOutcome], Optional[str]]:
    row = con.execute(
        """
        SELECT * FROM analyses
        WHERE file_id=? AND file_sha256=? AND analyzer_id=? AND api_version=?
        """,
        (candidate.file_id, candidate.sha256, analyzer_id, api_version),
    ).fetchone()

    canonical_raw, canonical_md = canonical_artifact_paths(
        output_dir, analyzer_id, api_version, candidate
    )
    legacy_raw, legacy_md = legacy_artifact_paths(output_dir, candidate)

    raw_candidates: list[tuple[str, Path]] = []
    if row is not None:
        db_raw = _existing_path(row["raw_json_path"])
        if db_raw is not None:
            raw_candidates.append(("db_path", db_raw))
    raw_candidates.extend([
        ("canonical", canonical_raw),
        ("legacy", legacy_raw),
    ])

    seen: set[Path] = set()
    validation_errors: list[str] = []
    expected_page_count: Optional[int] = None
    source_path = _existing_path(candidate.db_path)
    source_extension = (candidate.extension or Path(candidate.db_path).suffix).lower()
    if source_path is not None and source_extension == ".pdf":
        try:
            expected_page_count = pdf_page_count(source_path)
        except Exception as exc:
            validation_errors.append(
                f"{source_path}: could not read source PDF page count: {exc}"
            )
    for source_kind, raw_path in raw_candidates:
        try:
            resolved = raw_path.expanduser().resolve()
        except OSError:
            resolved = raw_path.expanduser()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)

        try:
            _, metadata = _load_and_validate_result_json(
                resolved, analyzer_id, api_version, expected_page_count
            )
        except ValueError as exc:
            validation_errors.append(f"{resolved}: {exc}")
            continue

        if resolved != canonical_raw.resolve():
            atomic_write_text(canonical_raw, resolved.read_text(encoding="utf-8"))

        md_source: Optional[Path] = None
        if row is not None:
            db_md = _existing_path(row["markdown_path"])
            if db_md is not None:
                md_source = db_md
        if md_source is None and canonical_md.is_file():
            md_source = canonical_md
        if md_source is None and legacy_md.is_file():
            md_source = legacy_md

        if md_source is not None:
            try:
                md_resolved = md_source.expanduser().resolve()
            except OSError:
                md_resolved = md_source.expanduser()
            if md_resolved != canonical_md.resolve():
                atomic_write_text(canonical_md, md_resolved.read_text(encoding="utf-8"))
        else:
            atomic_write_text(canonical_md, metadata["markdown"])
            source_kind = source_kind + "+markdown_from_json"

        operation_id = str(row["operation_id"]) if row is not None and row["operation_id"] else None
        outcome = AnalysisOutcome(
            document_id=candidate.document_id,
            file_id=candidate.file_id,
            status="SUCCEEDED",
            operation_id=operation_id,
            api_version=api_version,
            raw_json_path=stored_path(canonical_raw),
            markdown_path=stored_path(canonical_md),
            page_count=int(metadata["page_count"]),
            table_count=int(metadata["table_count"]),
            section_count=int(metadata["section_count"]),
            warning_count=int(metadata["warning_count"]),
            elapsed_seconds=0.0,
            attempt_count=0,
            artifact_source=f"reconciled_{source_kind}",
            reconciled_at=utcnow(),
        )
        return outcome, None

    if validation_errors:
        return None, "; ".join(validation_errors)[:4000]
    return None, None


def audit_succeeded_artifacts(
    con: sqlite3.Connection,
    output_dir: Path,
    analyzer_id: str,
    api_version: str,
    document_id: Optional[str],
) -> tuple[int, int]:
    params: list[Any] = [analyzer_id, api_version]
    doc_clause = ""
    if document_id:
        doc_clause = " AND d.id = ?"
        params.append(document_id)

    rows = con.execute(
        f"""
        SELECT
            d.id AS document_id, f.id AS file_id, f.path AS db_path, f.sha256,
            f.extension, f.mime_type, f.size_bytes
        FROM analyses a
        JOIN files f ON f.id = a.file_id
        JOIN documents d ON d.id = f.document_id
        WHERE f.is_current=1
          AND a.analyzer_id=? AND a.api_version=? AND a.status='SUCCEEDED'
          {doc_clause}
        """,
        params,
    ).fetchall()

    verified = 0
    stale = 0
    for r in rows:
        candidate = Candidate(
            document_id=str(r["document_id"]),
            file_id=int(r["file_id"]),
            db_path=str(r["db_path"]),
            sha256=str(r["sha256"]),
            extension=r["extension"],
            mime_type=r["mime_type"],
            size_bytes=r["size_bytes"],
        )
        outcome, validation_error = find_reconcilable_artifacts(
            con, candidate, output_dir, analyzer_id, api_version
        )
        if outcome is not None:
            con.execute(
                """
                UPDATE analyses
                SET raw_json_path=?, markdown_path=?, page_count=?, table_count=?,
                    section_count=?, warning_count=?,
                    artifact_source=CASE
                        WHEN raw_json_path IS NULL OR raw_json_path <> ?
                            THEN ?
                        ELSE artifact_source
                    END,
                    reconciled_at=CASE
                        WHEN raw_json_path IS NULL OR raw_json_path <> ?
                            THEN ?
                        ELSE reconciled_at
                    END,
                    error_code=NULL, error_message=NULL, updated_at=?
                WHERE file_id=? AND file_sha256=? AND analyzer_id=? AND api_version=?
                """,
                (
                    outcome.raw_json_path, outcome.markdown_path, outcome.page_count,
                    outcome.table_count, outcome.section_count, outcome.warning_count,
                    outcome.raw_json_path, outcome.artifact_source,
                    outcome.raw_json_path, outcome.reconciled_at, utcnow(),
                    candidate.file_id, candidate.sha256, analyzer_id, api_version,
                ),
            )
            verified += 1
            continue

        stale += 1
        con.execute(
            """
            UPDATE analyses
            SET status='STALE_ARTIFACTS', error_code='ARTIFACT_MISSING_OR_INVALID',
                error_message=?, updated_at=?
            WHERE file_id=? AND file_sha256=? AND analyzer_id=? AND api_version=?
            """,
            (
                validation_error or "Successful DB row has no valid matching JSON artifact",
                utcnow(), candidate.file_id, candidate.sha256, analyzer_id, api_version,
            ),
        )
    con.commit()
    return verified, stale


def resolve_file_path(candidate: Candidate, db_path: Path, download_dir: Optional[Path]) -> Path:
    p = Path(candidate.db_path)
    attempts: list[Path] = [
        resolve_stored_path(candidate.db_path, legacy_base=db_path.parent)
    ]

    if p.is_absolute():
        attempts.append(p)
    else:
        if download_dir:
            attempts.append(download_dir / p)
        attempts.append(db_path.parent / p)
        attempts.append(p)

    if download_dir:
        ext = candidate.extension or Path(candidate.db_path).suffix
        if ext:
            if not str(ext).startswith("."):
                ext = "." + str(ext)
            attempts.append(download_dir / f"{candidate.document_id}{ext}")
        attempts.append(download_dir / f"{candidate.document_id}.pdf")

    seen = set()
    for attempt in attempts:
        resolved = attempt.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved

    raise FileNotFoundError(
        f"Downloaded file not found for document {candidate.document_id}; "
        f"DB path={candidate.db_path!r}; tried: "
        + ", ".join(stored_path(x) for x in attempts)
    )


def detect_mime(path: Path, candidate: Candidate) -> str:
    if candidate.mime_type and candidate.mime_type != "application/octet-stream":
        return candidate.mime_type.split(";", 1)[0].strip()
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def upsert_analysis_start(
    con: sqlite3.Connection,
    run_id: int,
    candidate: Candidate,
    analyzer_id: str,
    api_version: str,
) -> None:
    now = utcnow()
    con.execute(
        """
        INSERT INTO analyses (
            run_id, document_id, file_id, file_sha256, analyzer_id, api_version,
            status, started_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?, ?)
        ON CONFLICT(file_id, file_sha256, analyzer_id, api_version) DO UPDATE SET
            run_id=excluded.run_id,
            status='RUNNING',
            started_at=excluded.started_at,
            finished_at=NULL,
            operation_id=NULL,
            raw_json_path=NULL,
            markdown_path=NULL,
            page_count=NULL,
            table_count=NULL,
            section_count=NULL,
            warning_count=NULL,
            elapsed_seconds=NULL,
            attempt_count=0,
            artifact_source=NULL,
            reconciled_at=NULL,
            error_code=NULL,
            error_message=NULL,
            updated_at=excluded.updated_at
        """,
        (
            run_id,
            candidate.document_id,
            candidate.file_id,
            candidate.sha256,
            analyzer_id,
            api_version,
            now,
            now,
            now,
        ),
    )
    con.commit()


def store_outcome(
    con: sqlite3.Connection,
    candidate: Candidate,
    analyzer_id: str,
    api_version: str,
    outcome: AnalysisOutcome,
) -> None:
    now = utcnow()
    con.execute(
        """
        UPDATE analyses
        SET status=?, operation_id=?, finished_at=?,
            raw_json_path=?, markdown_path=?, page_count=?, table_count=?,
            section_count=?, warning_count=?, elapsed_seconds=?, attempt_count=?,
            artifact_source=?, reconciled_at=?, error_code=?, error_message=?, updated_at=?
        WHERE file_id=? AND file_sha256=? AND analyzer_id=? AND api_version=?
        """,
        (
            outcome.status,
            outcome.operation_id,
            now,
            outcome.raw_json_path,
            outcome.markdown_path,
            outcome.page_count,
            outcome.table_count,
            outcome.section_count,
            outcome.warning_count,
            outcome.elapsed_seconds,
            outcome.attempt_count,
            outcome.artifact_source,
            outcome.reconciled_at,
            outcome.error_code,
            outcome.error_message,
            now,
            candidate.file_id,
            candidate.sha256,
            analyzer_id,
            api_version,
        ),
    )
    if outcome.status == "SUCCEEDED":
        con.execute(
            """
            UPDATE errors
            SET resolved_at=?
            WHERE stage='analyze' AND document_id=? AND resolved_at IS NULL
            """,
            (now, candidate.document_id),
        )
    con.commit()


def record_error(
    con: sqlite3.Connection,
    run_id: int,
    document_id: str,
    code: str,
    message: str,
    retryable: bool,
    context: dict[str, Any],
) -> None:
    con.execute(
        """
        INSERT INTO errors (
            run_id, document_id, stage, code, severity, message,
            retryable, context_json, created_at
        ) VALUES (?, ?, 'analyze', ?, 'ERROR', ?, ?, ?, ?)
        """,
        (
            run_id,
            document_id,
            code,
            message[:4000],
            1 if retryable else 0,
            json.dumps(context, default=str),
            utcnow(),
        ),
    )
    con.commit()


def make_client(args: argparse.Namespace) -> tuple[ContentUnderstandingClient, Any]:
    if not args.endpoint:
        raise RuntimeError(
            "Azure Content Understanding endpoint is required. "
            "Pass --endpoint or set CONTENTUNDERSTANDING_ENDPOINT."
        )

    if args.key:
        credential = AzureKeyCredential(args.key)
    else:
        credential = DefaultAzureCredential()

    client = ContentUnderstandingClient(
        endpoint=args.endpoint,
        credential=credential,
        api_version=args.api_version,
        polling_interval=args.polling_interval,
        retry_total=0,
        retry_connect=0,
        retry_read=0,
        retry_status=0,
    )
    return client, credential


def analyze_one(
    client: ContentUnderstandingClient,
    candidate: Candidate,
    file_path: Path,
    output_dir: Path,
    analyzer_id: str,
    api_version: str,
    verify_hash: bool,
    max_attempts: int,
    retry_base_delay: float,
    retry_max_delay: float,
    expected_page_count: Optional[int] = None,
) -> AnalysisOutcome:
    start = time.monotonic()
    operation_id: Optional[str] = None

    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return AnalysisOutcome(
            document_id=candidate.document_id,
            file_id=candidate.file_id,
            status="SKIPPED_UNSUPPORTED",
            error_code="UNSUPPORTED_EXTENSION",
            error_message=f"Unsupported extension: {file_path.suffix}",
            elapsed_seconds=time.monotonic() - start,
            attempt_count=0,
        )

    if verify_hash:
        actual_hash = sha256_file(file_path)
        if actual_hash.lower() != candidate.sha256.lower():
            return AnalysisOutcome(
                document_id=candidate.document_id,
                file_id=candidate.file_id,
                status="FAILED",
                error_code="HASH_MISMATCH",
                error_message=(
                    f"DB sha256={candidate.sha256}, disk sha256={actual_hash}, "
                    f"path={stored_path(file_path)}"
                ),
                elapsed_seconds=time.monotonic() - start,
                attempt_count=0,
            )

    mime_type = detect_mime(file_path, candidate)
    data = file_path.read_bytes()

    for attempt in range(1, max_attempts + 1):
        poller = None
        try:
            poller = client.begin_analyze_binary(
                analyzer_id=analyzer_id,
                binary_input=data,
                content_type=mime_type,
            )
            operation_id = getattr(poller, "operation_id", None)
            result = poller.result()

            result_dict = result.as_dict()
            result_api_version = getattr(result, "api_version", None)

            try:
                _, metadata = _load_and_validate_result_json_payload(
                    result_dict,
                    analyzer_id,
                    api_version,
                    expected_page_count,
                )
            except ValueError as exc:
                error_code = (
                    "PAGE_COUNT_MISMATCH"
                    if str(exc).startswith("page count mismatch:")
                    else "INVALID_ANALYSIS_RESULT"
                )
                return AnalysisOutcome(
                    document_id=candidate.document_id,
                    file_id=candidate.file_id,
                    status="FAILED",
                    operation_id=operation_id,
                    api_version=result_api_version,
                    error_code=error_code,
                    error_message=str(exc),
                    elapsed_seconds=time.monotonic() - start,
                    attempt_count=attempt,
                )

            raw_path, md_path = canonical_artifact_paths(
                output_dir, analyzer_id, api_version, candidate
            )

            atomic_write_text(raw_path, json_dumps(result_dict))
            atomic_write_text(md_path, metadata["markdown"])

            return AnalysisOutcome(
                document_id=candidate.document_id,
                file_id=candidate.file_id,
                status="SUCCEEDED",
                operation_id=operation_id,
                api_version=result_api_version,
                raw_json_path=stored_path(raw_path),
                markdown_path=stored_path(md_path),
                page_count=int(metadata["page_count"]),
                table_count=int(metadata["table_count"]),
                section_count=int(metadata["section_count"]),
                warning_count=int(metadata["warning_count"]),
                elapsed_seconds=time.monotonic() - start,
                attempt_count=attempt,
                artifact_source="azure",
            )

        except HttpResponseError as exc:
            error_obj = getattr(exc, "error", None)
            code = getattr(error_obj, "code", None) or getattr(exc, "status_code", None) or "HTTP_ERROR"
            code = str(code)
            can_resubmit = poller is None and is_retryable_error_code(code) and attempt < max_attempts
            if can_resubmit:
                delay = retry_delay_seconds(exc, attempt, retry_base_delay, retry_max_delay)
                print(
                    f"          RETRY {attempt}/{max_attempts - 1} after {delay:g}s "
                    f"({code}: {str(exc)[:180]})"
                )
                time.sleep(delay)
                continue
            return AnalysisOutcome(
                document_id=candidate.document_id,
                file_id=candidate.file_id,
                status="FAILED",
                operation_id=operation_id,
                error_code=code,
                error_message=str(exc),
                elapsed_seconds=time.monotonic() - start,
                attempt_count=attempt,
            )

        except ServiceRequestError as exc:
            can_resubmit = poller is None and attempt < max_attempts
            if can_resubmit:
                delay = retry_delay_seconds(exc, attempt, retry_base_delay, retry_max_delay)
                print(
                    f"          RETRY {attempt}/{max_attempts - 1} after {delay:g}s "
                    f"(SERVICE_REQUEST_ERROR: {str(exc)[:180]})"
                )
                time.sleep(delay)
                continue
            return AnalysisOutcome(
                document_id=candidate.document_id,
                file_id=candidate.file_id,
                status="FAILED",
                operation_id=operation_id,
                error_code="SERVICE_REQUEST_ERROR",
                error_message=str(exc),
                elapsed_seconds=time.monotonic() - start,
                attempt_count=attempt,
            )

        except Exception as exc:
            return AnalysisOutcome(
                document_id=candidate.document_id,
                file_id=candidate.file_id,
                status="FAILED",
                operation_id=operation_id,
                error_code=type(exc).__name__,
                error_message=str(exc),
                elapsed_seconds=time.monotonic() - start,
                attempt_count=attempt,
            )

    raise AssertionError("unreachable")


_analyze_one_single_request = analyze_one


def pdf_page_count(path: Path) -> int:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "Missing PDF page-count dependency. Install with: "
            "python -m pip install -r regdocs_atlas/requirements.txt"
        ) from exc
    return len(PdfReader(str(path)).pages)


def page_ranges(
    page_count: int,
    chunk_size: int = MAX_PAGES_PER_ANALYSIS,
) -> list[tuple[int, int]]:
    if page_count < 1:
        return []
    return [
        (start, min(start + chunk_size - 1, page_count))
        for start in range(1, page_count + 1, chunk_size)
    ]


def range_artifact_paths(
    output_dir: Path,
    analyzer_id: str,
    api_version: str,
    candidate: Candidate,
    start_page: int,
    end_page: int,
) -> tuple[Path, Path]:
    raw_path, _ = canonical_artifact_paths(output_dir, analyzer_id, api_version, candidate)
    parts_dir = raw_path.with_suffix(".parts")
    stem = f"pages-{start_page:04d}-{end_page:04d}"
    return parts_dir / f"{stem}.json", parts_dir / f"{stem}.meta.json"


def _result_counts(
    result_dict: dict[str, Any],
) -> tuple[int, int, int, int, list[str]]:
    pages = 0
    tables = 0
    sections = 0
    markdown_parts: list[str] = []
    for content in result_dict.get("contents") or []:
        if not isinstance(content, dict):
            continue
        content_pages = content.get("pages") or []
        content_tables = content.get("tables") or []
        content_sections = content.get("sections") or []
        if isinstance(content_pages, list):
            pages += len(content_pages)
        if isinstance(content_tables, list):
            tables += len(content_tables)
        if isinstance(content_sections, list):
            sections += len(content_sections)
        markdown = content.get("markdown")
        if isinstance(markdown, str) and markdown:
            markdown_parts.append(markdown)
    warnings = result_dict.get("warnings") or []
    warning_count = len(warnings) if isinstance(warnings, list) else 0
    return pages, tables, sections, warning_count, markdown_parts


def _reuse_range_artifacts_enabled() -> bool:
    return not any(
        arg in {"--force", "--no-reconcile-artifacts"}
        for arg in sys.argv[1:]
    )


def load_reusable_range_artifact(
    raw_path: Path,
    meta_path: Path,
    candidate: Candidate,
    analyzer_id: str,
    api_version: str,
    content_range: str,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    if not raw_path.is_file() or not meta_path.is_file():
        return None, None
    try:
        result_dict, _ = _load_and_validate_result_json(raw_path, analyzer_id, api_version)
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            return None, None
        if metadata.get("document_id") != candidate.document_id:
            return None, None
        if str(metadata.get("file_sha256") or "").lower() != candidate.sha256.lower():
            return None, None
        if metadata.get("analyzer_id") != analyzer_id:
            return None, None
        if metadata.get("api_version") != api_version:
            return None, None
        if metadata.get("content_range") != content_range:
            return None, None
        return result_dict, metadata.get("operation_id")
    except (OSError, ValueError, json.JSONDecodeError):
        return None, None


def analyze_one(
    client: ContentUnderstandingClient,
    candidate: Candidate,
    file_path: Path,
    output_dir: Path,
    analyzer_id: str,
    api_version: str,
    verify_hash: bool,
    max_attempts: int,
    retry_base_delay: float,
    retry_max_delay: float,
) -> AnalysisOutcome:
    if file_path.suffix.lower() != ".pdf":
        return _analyze_one_single_request(
            client,
            candidate,
            file_path,
            output_dir,
            analyzer_id,
            api_version,
            verify_hash,
            max_attempts,
            retry_base_delay,
            retry_max_delay,
            None,
        )

    start_time = time.monotonic()

    if verify_hash:
        actual_hash = sha256_file(file_path)
        if actual_hash.lower() != candidate.sha256.lower():
            return AnalysisOutcome(
                document_id=candidate.document_id,
                file_id=candidate.file_id,
                status="FAILED",
                error_code="HASH_MISMATCH",
                error_message=(
                    f"DB sha256={candidate.sha256}, disk sha256={actual_hash}, "
                    f"path={stored_path(file_path)}"
                ),
                elapsed_seconds=time.monotonic() - start_time,
                attempt_count=0,
            )

    try:
        page_count = pdf_page_count(file_path)
    except Exception as exc:
        return AnalysisOutcome(
            document_id=candidate.document_id,
            file_id=candidate.file_id,
            status="FAILED",
            error_code="PDF_PAGE_COUNT_FAILED",
            error_message=f"Could not read PDF page count: {exc}",
            elapsed_seconds=time.monotonic() - start_time,
            attempt_count=0,
        )

    if page_count < 1:
        return AnalysisOutcome(
            document_id=candidate.document_id,
            file_id=candidate.file_id,
            status="FAILED",
            error_code="PDF_PAGE_COUNT_FAILED",
            error_message="PDF has no pages",
            elapsed_seconds=time.monotonic() - start_time,
            attempt_count=0,
        )

    if page_count <= MAX_PAGES_PER_ANALYSIS:
        return _analyze_one_single_request(
            client,
            candidate,
            file_path,
            output_dir,
            analyzer_id,
            api_version,
            False,
            max_attempts,
            retry_base_delay,
            retry_max_delay,
            page_count,
        )

    ranges = page_ranges(page_count)
    print(
        f"          PDF pages={page_count}; splitting into {len(ranges)} Azure analyses "
        f"of at most {MAX_PAGES_PER_ANALYSIS} pages"
    )

    mime_type = detect_mime(file_path, candidate)
    data = file_path.read_bytes()
    range_results: list[dict[str, Any]] = []
    operation_ids: list[str] = []
    total_attempts = 0
    reuse_range_parts = _reuse_range_artifacts_enabled()

    for chunk_index, (start_page, end_page) in enumerate(ranges, start=1):
        content_range = f"{start_page}-{end_page}"
        expected_pages = end_page - start_page + 1
        part_raw, part_meta = range_artifact_paths(
            output_dir,
            analyzer_id,
            api_version,
            candidate,
            start_page,
            end_page,
        )

        if reuse_range_parts:
            reused, reused_operation_id = load_reusable_range_artifact(
                part_raw,
                part_meta,
                candidate,
                analyzer_id,
                api_version,
                content_range,
            )
            if reused is not None:
                reused_pages, _, _, _, _ = _result_counts(reused)
                if reused_pages == expected_pages:
                    range_results.append(reused)
                    if reused_operation_id:
                        operation_ids.append(str(reused_operation_id))
                    print(
                        f"          [{chunk_index}/{len(ranges)}] pages {content_range} "
                        "RECOVERED locally; no Azure call"
                    )
                    continue
                print(
                    f"          [{chunk_index}/{len(ranges)}] pages {content_range} "
                    f"cached range rejected: expected {expected_pages} pages, got {reused_pages}"
                )

        print(f"          [{chunk_index}/{len(ranges)}] pages {content_range}")
        operation_id: Optional[str] = None
        completed_result: Optional[dict[str, Any]] = None

        for attempt in range(1, max_attempts + 1):
            total_attempts += 1
            poller = None
            try:
                poller = client.begin_analyze_binary(
                    analyzer_id=analyzer_id,
                    binary_input=data,
                    content_type=mime_type,
                    content_range=content_range,
                )
                operation_id = getattr(poller, "operation_id", None)
                result = poller.result()
                result_dict = result.as_dict()
                returned_pages, _, _, _, _ = _result_counts(result_dict)
                if returned_pages != expected_pages:
                    return AnalysisOutcome(
                        document_id=candidate.document_id,
                        file_id=candidate.file_id,
                        status="FAILED",
                        operation_id=operation_id,
                        error_code="RANGE_PAGE_COUNT_MISMATCH",
                        error_message=(
                            f"pages {content_range}: expected {expected_pages} pages "
                            f"but Azure returned {returned_pages}; range artifact not committed"
                        ),
                        elapsed_seconds=time.monotonic() - start_time,
                        attempt_count=total_attempts,
                    )

                completed_result = result_dict

                atomic_write_text(part_raw, json_dumps(completed_result))
                atomic_write_text(
                    part_meta,
                    json_dumps(
                        {
                            "document_id": candidate.document_id,
                            "file_sha256": candidate.sha256,
                            "analyzer_id": analyzer_id,
                            "api_version": api_version,
                            "content_range": content_range,
                            "page_count": returned_pages,
                            "operation_id": operation_id,
                            "completed_at": utcnow(),
                        }
                    ),
                )
                break

            except HttpResponseError as exc:
                error_obj = getattr(exc, "error", None)
                code = (
                    getattr(error_obj, "code", None)
                    or getattr(exc, "status_code", None)
                    or "HTTP_ERROR"
                )
                code = str(code)
                can_resubmit = (
                    poller is None
                    and is_retryable_error_code(code)
                    and attempt < max_attempts
                )
                if can_resubmit:
                    delay = retry_delay_seconds(exc, attempt, retry_base_delay, retry_max_delay)
                    print(
                        f"              RETRY {attempt}/{max_attempts - 1} after {delay:g}s "
                        f"({code}: {str(exc)[:180]})"
                    )
                    time.sleep(delay)
                    continue
                return AnalysisOutcome(
                    document_id=candidate.document_id,
                    file_id=candidate.file_id,
                    status="FAILED",
                    operation_id=operation_id,
                    error_code=code,
                    error_message=f"pages {content_range}: {exc}",
                    elapsed_seconds=time.monotonic() - start_time,
                    attempt_count=total_attempts,
                )

            except ServiceRequestError as exc:
                can_resubmit = poller is None and attempt < max_attempts
                if can_resubmit:
                    delay = retry_delay_seconds(exc, attempt, retry_base_delay, retry_max_delay)
                    print(
                        f"              RETRY {attempt}/{max_attempts - 1} after {delay:g}s "
                        f"(SERVICE_REQUEST_ERROR: {str(exc)[:180]})"
                    )
                    time.sleep(delay)
                    continue
                return AnalysisOutcome(
                    document_id=candidate.document_id,
                    file_id=candidate.file_id,
                    status="FAILED",
                    operation_id=operation_id,
                    error_code="SERVICE_REQUEST_ERROR",
                    error_message=f"pages {content_range}: {exc}",
                    elapsed_seconds=time.monotonic() - start_time,
                    attempt_count=total_attempts,
                )

            except Exception as exc:
                return AnalysisOutcome(
                    document_id=candidate.document_id,
                    file_id=candidate.file_id,
                    status="FAILED",
                    operation_id=operation_id,
                    error_code=type(exc).__name__,
                    error_message=f"pages {content_range}: {exc}",
                    elapsed_seconds=time.monotonic() - start_time,
                    attempt_count=total_attempts,
                )

        if completed_result is None:
            return AnalysisOutcome(
                document_id=candidate.document_id,
                file_id=candidate.file_id,
                status="FAILED",
                operation_id=operation_id,
                error_code="RANGE_ANALYSIS_INCOMPLETE",
                error_message=f"pages {content_range}: no completed Azure result",
                elapsed_seconds=time.monotonic() - start_time,
                attempt_count=total_attempts,
            )

        range_results.append(completed_result)
        if operation_id:
            operation_ids.append(str(operation_id))

    first = dict(range_results[0])
    combined_contents: list[Any] = []
    combined_warnings: list[Any] = []
    markdown_parts: list[str] = []
    total_pages = 0
    total_tables = 0
    total_sections = 0

    for result_dict in range_results:
        contents = result_dict.get("contents") or []
        if isinstance(contents, list):
            combined_contents.extend(contents)
        warnings = result_dict.get("warnings") or []
        if isinstance(warnings, list):
            combined_warnings.extend(warnings)

        pages, tables, sections, _, part_markdown = _result_counts(result_dict)
        total_pages += pages
        total_tables += tables
        total_sections += sections
        markdown_parts.extend(part_markdown)

    if total_pages != page_count:
        return AnalysisOutcome(
            document_id=candidate.document_id,
            file_id=candidate.file_id,
            status="FAILED",
            operation_id=",".join(operation_ids) or None,
            api_version=api_version,
            page_count=total_pages,
            table_count=total_tables,
            section_count=total_sections,
            warning_count=len(combined_warnings),
            error_code="RANGE_PAGE_COUNT_MISMATCH",
            error_message=(
                f"PDF has {page_count} pages but combined Content-Range results contain "
                f"{total_pages}; canonical artifact not published"
            ),
            elapsed_seconds=time.monotonic() - start_time,
            attempt_count=total_attempts,
        )

    first["contents"] = combined_contents
    first["warnings"] = combined_warnings
    first["regdocsChunking"] = {
        "strategy": "content_range",
        "sourcePageCount": page_count,
        "validatedPageCount": total_pages,
        "maxPagesPerRequest": MAX_PAGES_PER_ANALYSIS,
        "rangeCount": len(ranges),
        "parts": [
            {
                "range": f"{start_page}-{end_page}",
                "rawJsonPath": stored_path(
                    range_artifact_paths(
                        output_dir,
                        analyzer_id,
                        api_version,
                        candidate,
                        start_page,
                        end_page,
                    )[0]
                ),
                "metaJsonPath": stored_path(
                    range_artifact_paths(
                        output_dir,
                        analyzer_id,
                        api_version,
                        candidate,
                        start_page,
                        end_page,
                    )[1]
                ),
            }
            for start_page, end_page in ranges
        ],
    }

    raw_path, md_path = canonical_artifact_paths(output_dir, analyzer_id, api_version, candidate)
    atomic_write_text(raw_path, json_dumps(first))
    atomic_write_text(md_path, "\n\n".join(markdown_parts))

    print(
        f"          COMBINED pages={total_pages} tables={total_tables} "
        f"sections={total_sections} ranges={len(ranges)}"
    )

    return AnalysisOutcome(
        document_id=candidate.document_id,
        file_id=candidate.file_id,
        status="SUCCEEDED",
        operation_id=",".join(operation_ids) or None,
        api_version=api_version,
        raw_json_path=stored_path(raw_path),
        markdown_path=stored_path(md_path),
        page_count=total_pages,
        table_count=total_tables,
        section_count=total_sections,
        warning_count=len(combined_warnings),
        elapsed_seconds=time.monotonic() - start_time,
        attempt_count=total_attempts,
        artifact_source="azure_content_range",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="REGDOCS Stage 3 Azure internal worker")
    p.add_argument(
        "--db",
        type=Path,
        default=stored_path(DATABASE_PATH),
        help=(
            "Path to the REGDOCS SQLite ledger "
            f"(default: {stored_path(DATABASE_PATH)})"
        ),
    )
    p.add_argument(
        "--endpoint",
        default=os.environ.get("CONTENTUNDERSTANDING_ENDPOINT"),
        help=(
            "Azure Content Understanding endpoint. "
            "Defaults to CONTENTUNDERSTANDING_ENDPOINT."
        ),
    )
    p.add_argument(
        "--key",
        default=os.environ.get("CONTENTUNDERSTANDING_KEY"),
        help=(
            "Azure Content Understanding API key. "
            "Defaults to CONTENTUNDERSTANDING_KEY. If omitted, DefaultAzureCredential is used. "
            "The key is not stored in SQLite."
        ),
    )
    p.add_argument(
        "--api-version",
        default=os.environ.get("CONTENTUNDERSTANDING_API_VERSION", DEFAULT_API_VERSION),
        help=(
            f"Azure Content Understanding API version (default: {DEFAULT_API_VERSION}; "
            "env: CONTENTUNDERSTANDING_API_VERSION)"
        ),
    )
    p.add_argument(
        "--polling-interval",
        type=float,
        default=float(os.environ.get("CONTENTUNDERSTANDING_POLLING_INTERVAL", DEFAULT_POLLING_INTERVAL)),
        help=(
            f"Seconds between long-running-operation polls when Azure does not provide Retry-After "
            f"(default: {DEFAULT_POLLING_INTERVAL})"
        ),
    )
    p.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help="Azure submission attempts. The public supervisor always supplies 1.",
    )
    p.add_argument(
        "--retry-base-delay",
        type=float,
        default=DEFAULT_RETRY_BASE_DELAY,
    )
    p.add_argument(
        "--retry-max-delay",
        type=float,
        default=DEFAULT_RETRY_MAX_DELAY,
    )
    p.add_argument(
        "--download-dir",
        type=Path,
        default=stored_path(DOWNLOAD_FILES_DIR),
        help="Root download directory used to resolve relative file paths / filename fallback",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=stored_path(CONTENT_UNDERSTANDING_DIR),
        help="Directory for raw JSON and Markdown artifacts",
    )
    p.add_argument(
        "--lock-file",
        type=Path,
        default=stored_path(ANALYZE_LOCK_PATH),
        help="Exclusive Stage 3 writer lock",
    )
    p.add_argument(
        "--force-lock",
        action="store_true",
        help="Remove an existing lock only after confirming no analyzer is running",
    )
    p.add_argument(
        "--analyzer-id",
        default=os.environ.get("CONTENTUNDERSTANDING_ANALYZER_ID", DEFAULT_ANALYZER_ID),
        help=(
            f"Azure Content Understanding analyzer ID (default: {DEFAULT_ANALYZER_ID}; "
            "env: CONTENTUNDERSTANDING_ANALYZER_ID)"
        ),
    )
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--limit", type=int, help="Process at most N candidates")
    scope.add_argument("--document-id", help="Analyze one REGDOCS document ID")
    scope.add_argument(
        "--all",
        dest="all_candidates",
        action="store_true",
        help="Explicitly acknowledge processing every currently eligible candidate",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Force a new Azure analysis; bypass DB/artifact reconciliation for selected candidates",
    )
    p.add_argument(
        "--no-reconcile-artifacts",
        action="store_true",
        help="Disable discovery/backfill of existing Azure JSON/Markdown artifacts",
    )
    p.add_argument(
        "--no-verify-hash",
        action="store_true",
        help="Skip SHA-256 verification against files.sha256",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show candidates and resolved files without calling Azure",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    args.db = resolve_stored_path(args.db)
    args.output_dir = resolve_stored_path(args.output_dir)
    args.lock_file = resolve_stored_path(args.lock_file)
    if args.download_dir:
        args.download_dir = resolve_stored_path(args.download_dir)

    if not args.db.is_file():
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 2

    if args.polling_interval <= 0:
        print("ERROR: --polling-interval must be greater than 0", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 1:
        print("ERROR: --limit must be at least 1", file=sys.stderr)
        return 2
    if args.max_attempts != 1:
        print("ERROR: Azure worker requires --max-attempts 1; retry on a later supervisor run", file=sys.stderr)
        return 2
    if args.retry_base_delay < 0 or args.retry_max_delay < 0:
        print("ERROR: retry delays cannot be negative", file=sys.stderr)
        return 2
    if args.retry_max_delay < args.retry_base_delay:
        print("ERROR: --retry-max-delay must be >= --retry-base-delay", file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)

    con = open_db(args.db)
    ensure_schema(con)
    stage_lock = StageLock(args.lock_file, force=args.force_lock)
    try:
        stage_lock.__enter__()
    except RuntimeError as exc:
        con.close()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        run_id = create_run(con, args)
    except Exception:
        con.close()
        stage_lock.__exit__()
        raise
    started = time.monotonic()

    client = None
    credential = None

    try:
        verified_existing = 0
        stale_existing = 0
        if not args.force and not args.no_reconcile_artifacts:
            verified_existing, stale_existing = audit_succeeded_artifacts(
                con, args.output_dir, args.analyzer_id, args.api_version, args.document_id
            )

        candidates = select_candidates(
            con,
            analyzer_id=args.analyzer_id,
            api_version=args.api_version,
            limit=args.limit,
            document_id=args.document_id,
            force=args.force,
        )
        total = len(candidates)

        con.execute(
            "UPDATE runs SET total_units=?, current_phase='analyzing', heartbeat_at=? WHERE id=?",
            (total, utcnow(), run_id),
        )
        con.commit()

        print(f"Run {run_id}: {total} candidate file(s)")
        print(f"Endpoint: {args.endpoint or '[not configured]'}")
        print(f"Auth:     {'API key' if args.key else 'DefaultAzureCredential'}")
        print(f"API:      {args.api_version}")
        print(f"Analyzer: {args.analyzer_id}")
        print(f"Polling:  {args.polling_interval:g}s")
        print("Retries:  disabled")
        print(f"Output:   {args.output_dir}")
        if not args.force and not args.no_reconcile_artifacts:
            print(
                f"Reconcile: enabled; {verified_existing} existing success row(s) verified, "
                f"{stale_existing} stale row(s) queued for repair"
            )
        else:
            print("Reconcile: disabled")

        if total == 0:
            finish_run(con, run_id, "SUCCEEDED", 0, 0, 0, 0, 0, 0, 0, time.monotonic() - started)
            return 0

        succeeded = 0
        failed = 0
        skipped = 0
        pages_succeeded = 0
        analysis_attempts = 0
        reconciled = 0

        for i, candidate in enumerate(candidates, start=1):
            if not args.force and not args.no_reconcile_artifacts:
                recovered, validation_error = find_reconcilable_artifacts(
                    con, candidate, args.output_dir, args.analyzer_id, args.api_version
                )
                if recovered is not None:
                    upsert_analysis_start(
                        con, run_id, candidate, args.analyzer_id, args.api_version
                    )
                    store_outcome(
                        con, candidate, args.analyzer_id, args.api_version, recovered
                    )
                    succeeded += 1
                    reconciled += 1
                    pages_succeeded += recovered.page_count or 0
                    print(f"[{i}/{total}] {candidate.document_id}  RECOVERED locally; no Azure call")
                    print(
                        f"          pages={recovered.page_count} tables={recovered.table_count} "
                        f"sections={recovered.section_count} source={recovered.artifact_source}"
                    )
                    print(f"          JSON: {recovered.raw_json_path}")
                    print(f"          MD:   {recovered.markdown_path}")
                    update_run_progress(
                        con, run_id, i, total, succeeded, failed, skipped,
                        pages_succeeded, analysis_attempts,
                        f"Recovered {candidate.document_id} locally; no Azure call",
                    )
                    continue
                elif validation_error:
                    print(
                        f"[{i}/{total}] {candidate.document_id}  existing artifact rejected: "
                        f"{validation_error[:300]}"
                    )

            try:
                file_path = resolve_file_path(candidate, args.db, args.download_dir)
            except FileNotFoundError as exc:
                outcome = AnalysisOutcome(
                    document_id=candidate.document_id,
                    file_id=candidate.file_id,
                    status="FAILED",
                    error_code="FILE_NOT_FOUND",
                    error_message=str(exc),
                )
                upsert_analysis_start(con, run_id, candidate, args.analyzer_id, args.api_version)
                store_outcome(con, candidate, args.analyzer_id, args.api_version, outcome)
                record_error(
                    con, run_id, candidate.document_id,
                    "FILE_NOT_FOUND", str(exc), False,
                    {"file_id": candidate.file_id, "db_path": candidate.db_path},
                )
                failed += 1
                print(f"[{i}/{total}] FAILED {candidate.document_id}: file not found")
                update_run_progress(
                    con, run_id, i, total, succeeded, failed, skipped,
                    pages_succeeded, analysis_attempts, str(exc)
                )
                continue

            print(f"[{i}/{total}] {candidate.document_id}  {file_path}")

            if args.dry_run:
                skipped += 1
                update_run_progress(
                    con, run_id, i, total, succeeded, failed, skipped,
                    pages_succeeded, analysis_attempts, f"Dry run: {candidate.document_id}"
                )
                continue

            if client is None:
                if not args.endpoint:
                    msg = (
                        "No valid existing artifact was found and Azure is required, but no endpoint "
                        "is configured. Pass --endpoint or set CONTENTUNDERSTANDING_ENDPOINT."
                    )
                    finish_run(
                        con,
                        run_id,
                        "FAILED",
                        i - 1,
                        total,
                        succeeded,
                        failed,
                        skipped,
                        pages_succeeded,
                        analysis_attempts,
                        time.monotonic() - started,
                    )
                    print(f"ERROR: {msg}", file=sys.stderr)
                    return 2
                client, credential = make_client(args)

            upsert_analysis_start(con, run_id, candidate, args.analyzer_id, args.api_version)
            outcome = analyze_one(
                client=client,
                candidate=candidate,
                file_path=file_path,
                output_dir=args.output_dir,
                analyzer_id=args.analyzer_id,
                api_version=args.api_version,
                verify_hash=not args.no_verify_hash,
                max_attempts=args.max_attempts,
                retry_base_delay=args.retry_base_delay,
                retry_max_delay=args.retry_max_delay,
            )
            store_outcome(con, candidate, args.analyzer_id, args.api_version, outcome)
            analysis_attempts += outcome.attempt_count

            if outcome.status == "SUCCEEDED":
                succeeded += 1
                pages_succeeded += outcome.page_count or 0
                print(
                    f"          SUCCEEDED pages={outcome.page_count} tables={outcome.table_count} "
                    f"sections={outcome.section_count} elapsed={outcome.elapsed_seconds:.1f}s "
                    f"attempts={outcome.attempt_count}"
                )
                print(f"          JSON: {outcome.raw_json_path}")
                print(f"          MD:   {outcome.markdown_path}")
            elif outcome.status.startswith("SKIPPED"):
                skipped += 1
                print(f"          {outcome.status}: {outcome.error_message}")
            else:
                failed += 1
                retryable = is_retryable_error_code(outcome.error_code)
                record_error(
                    con,
                    run_id,
                    candidate.document_id,
                    outcome.error_code or "ANALYZE_FAILED",
                    outcome.error_message or "Azure analysis failed",
                    retryable,
                    {
                        "file_id": candidate.file_id,
                        "file_path": stored_path(file_path),
                        "operation_id": outcome.operation_id,
                        "analyzer_id": args.analyzer_id,
                        "api_version": args.api_version,
                        "attempt_count": outcome.attempt_count,
                    },
                )
                print(f"          FAILED {outcome.error_code}: {outcome.error_message}")

            update_run_progress(
                con,
                run_id,
                i,
                total,
                succeeded,
                failed,
                skipped,
                pages_succeeded,
                analysis_attempts,
                f"Analyzed {candidate.document_id}: {outcome.status}; pages={pages_succeeded}",
            )

        final_status = "SUCCEEDED" if failed == 0 else "COMPLETED_WITH_ERRORS"
        finish_run(
            con,
            run_id,
            final_status,
            total,
            total,
            succeeded,
            failed,
            skipped,
            pages_succeeded,
            analysis_attempts,
            time.monotonic() - started,
        )

        print()
        print(
            f"Run {run_id} {final_status}: "
            f"{succeeded} succeeded ({reconciled} reconciled locally), "
            f"{failed} failed, {skipped} skipped, {pages_succeeded} pages, "
            f"{analysis_attempts} Azure submission attempt(s)"
        )
        return 0 if failed == 0 else 1

    except KeyboardInterrupt:
        con.execute(
            "UPDATE runs SET status='INTERRUPTED', finished_at=?, heartbeat_at=?, progress_message=? WHERE id=?",
            (utcnow(), utcnow(), "Interrupted by user", run_id),
        )
        con.commit()
        print("\nInterrupted. Completed analyses remain committed and will be skipped on restart.")
        return 130

    except Exception as exc:
        con.execute(
            "UPDATE runs SET status='FAILED', finished_at=?, heartbeat_at=?, progress_message=? WHERE id=?",
            (utcnow(), utcnow(), str(exc)[:1000], run_id),
        )
        con.commit()
        print(f"FATAL: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1

    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if credential is not None and hasattr(credential, "close"):
            try:
                credential.close()
            except Exception:
                pass
        con.close()
        stage_lock.__exit__()


if __name__ == "__main__":
    raise SystemExit(main())
