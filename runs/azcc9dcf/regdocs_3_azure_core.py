#!/usr/bin/env python3
"""Single-threaded crash-resilient REGDOCS Azure analyzer.

The public Stage 3 Azure command is the run owner. One invocation creates one
``runs`` row, then launches exactly one isolated child process per selected
document. Children attach their ``analyses`` and ``errors`` rows to the parent
run and are not allowed to create or finish pipeline runs themselves.

Azure retries are intentionally conservative because a repeated submission can
be billable. Each document gets one worker launch per supervisor run and each
Azure request/range gets one application-level submission attempt. A handled
failure, segfault, OOM kill, or other child crash is recorded and the supervisor
advances. A later normal Stage 3 Azure run is the retry boundary.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from regdocs_paths import (
    ANALYZE_LOCK_PATH,
    CONTENT_UNDERSTANDING_DIR,
    DATABASE_PATH,
    DOWNLOAD_FILES_DIR,
    resolve_stored_path,
    stored_path,
)

SCRIPT_VERSION = "3.7.1"
DEFAULT_API_VERSION = "2025-11-01"
DEFAULT_ANALYZER_ID = "prebuilt-layout"
DEFAULT_POLLING_INTERVAL = 3
DEFAULT_WORKER_SLEEP_SECONDS = 0.25
DEFAULT_STATE_FILE = CONTENT_UNDERSTANDING_DIR / "supervisor-state.json"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_path_component(value: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in value.strip())
    return cleaned or "unknown"


def _read_lock_pid(path: Path) -> Optional[int]:
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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


class StageLock:
    """Exclusive lock held by the durable supervisor for the whole Stage 3 run."""

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

        payload = {
            "pid": os.getpid(),
            "created_at": utcnow(),
            "role": "azure_supervisor",
            "script_version": SCRIPT_VERSION,
        }
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


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    with partial.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(partial, path)


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "schema_version": 2,
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "documents": {},
        }
    with path.open("r", encoding="utf-8") as stream:
        state = json.load(stream)
    if not isinstance(state, dict) or not isinstance(state.get("documents", {}), dict):
        raise ValueError(f"Invalid Azure supervisor state: {path}")
    state.setdefault("schema_version", 2)
    state.setdefault("created_at", utcnow())
    state.setdefault("documents", {})
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["schema_version"] = 2
    state["updated_at"] = utcnow()
    atomic_write_json(path, state)


def open_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    return con


def canonical_artifact_paths(
    output_dir: Path,
    analyzer_id: str,
    api_version: str,
    document_id: str,
    sha256: str,
) -> tuple[Path, Path]:
    analyzer_component = safe_path_component(analyzer_id)
    api_component = safe_path_component(api_version)
    identity = sha256.lower()
    raw_path = (
        output_dir / "raw" / analyzer_component / api_component /
        document_id / f"{identity}.json"
    )
    md_path = (
        output_dir / "markdown" / analyzer_component / api_component /
        document_id / f"{identity}.md"
    )
    return raw_path, md_path


def canonical_success_is_usable(
    output_dir: Path,
    analyzer_id: str,
    api_version: str,
    document_id: str,
    sha256: str,
) -> bool:
    raw_path, md_path = canonical_artifact_paths(
        output_dir, analyzer_id, api_version, document_id, sha256
    )
    if not raw_path.is_file() or not md_path.is_file():
        return False
    try:
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    actual_analyzer = payload.get("analyzerId") or payload.get("analyzer_id")
    actual_api = payload.get("apiVersion") or payload.get("api_version")
    contents = payload.get("contents")
    return (
        actual_analyzer == analyzer_id
        and actual_api == api_version
        and isinstance(contents, list)
        and bool(contents)
    )


def _analysis_columns(con: sqlite3.Connection) -> set[str]:
    try:
        return {str(r[1]) for r in con.execute("PRAGMA table_info(analyses)").fetchall()}
    except sqlite3.DatabaseError:
        return set()


def current_files(con: sqlite3.Connection, document_id: Optional[str]) -> list[sqlite3.Row]:
    params: list[Any] = []
    where = "WHERE f.is_current=1"
    if document_id is not None:
        where += " AND d.id=?"
        params.append(document_id)
    rows = con.execute(
        f"""
        SELECT d.id AS document_id, f.id AS file_id, f.sha256
        FROM files f
        JOIN documents d ON d.id=f.document_id
        {where}
        """,
        params,
    ).fetchall()
    return sorted(
        rows,
        key=lambda r: (
            0, int(str(r["document_id"])), str(r["document_id"])
        ) if str(r["document_id"]).isdigit() else (
            1, str(r["document_id"]).casefold(), str(r["document_id"])
        ),
    )


def matching_analysis_status(
    con: sqlite3.Connection,
    file_id: int,
    sha256: str,
    analyzer_id: str,
    api_version: str,
) -> Optional[sqlite3.Row]:
    needed = {"file_id", "file_sha256", "analyzer_id", "api_version", "status"}
    if not needed.issubset(_analysis_columns(con)):
        return None
    return con.execute(
        """
        SELECT status, error_code, error_message, page_count
        FROM analyses
        WHERE file_id=? AND file_sha256=? AND analyzer_id=? AND api_version=?
        """,
        (file_id, sha256, analyzer_id, api_version),
    ).fetchone()


def select_documents(
    con: sqlite3.Connection,
    state: dict[str, Any],
    output_dir: Path,
    analyzer_id: str,
    api_version: str,
    document_id: Optional[str],
    limit: Optional[int],
    force: bool,
) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for row in current_files(con, document_id):
        doc_id = str(row["document_id"])
        if doc_id in seen:
            continue
        seen.add(doc_id)

        sha256 = str(row["sha256"])
        info = state["documents"].setdefault(doc_id, {})
        if not isinstance(info, dict):
            info = {}
            state["documents"][doc_id] = info

        if info.pop("quarantined", False):
            info["legacy_quarantine_cleared_at"] = utcnow()
        info.pop("quarantined_at", None)
        info.pop("crash_attempts", None)

        info["file_sha256"] = sha256
        info["analyzer_id"] = analyzer_id
        info["api_version"] = api_version

        if not force:
            analysis = matching_analysis_status(
                con,
                int(row["file_id"]),
                sha256,
                analyzer_id,
                api_version,
            )
            if analysis is not None and analysis["status"] == "SUCCEEDED":
                if canonical_success_is_usable(
                    output_dir,
                    analyzer_id,
                    api_version,
                    doc_id,
                    sha256,
                ):
                    continue

        selected.append(doc_id)
        if limit is not None and len(selected) >= limit:
            break
    return selected


def create_supervisor_run(con: sqlite3.Connection, args: argparse.Namespace, total: int) -> int:
    now = utcnow()
    params = {
        "provider": "azure",
        "db": stored_path(args.db),
        "download_dir": stored_path(args.download_dir),
        "output_dir": stored_path(args.output_dir),
        "analyzer_id": args.analyzer_id,
        "api_version": args.api_version,
        "document_id": args.document_id,
        "limit": args.limit,
        "all": args.all_candidates,
        "force": args.force,
        "reconcile_artifacts": not args.no_reconcile_artifacts,
        "verify_hash": not args.no_verify_hash,
        "concurrency": 1,
        "worker_launches_per_document": 1,
        "azure_submission_attempts_per_request": 1,
        "retry_boundary": "next_supervisor_run",
    }
    cur = con.execute(
        """
        INSERT INTO runs (
            stage, status, started_at, parameters_json, summary_json,
            script_version, parser_version, current_phase, heartbeat_at,
            completed_units, total_units, progress_message
        ) VALUES (?, 'RUNNING', ?, ?, '{}', ?, ?, 'analyzing', ?, 0, ?, ?)
        """,
        (
            "analyze",
            now,
            json.dumps(params, sort_keys=True),
            SCRIPT_VERSION,
            f"azure-content-understanding-{args.api_version}",
            now,
            total,
            f"Azure supervisor selected {total} document(s)",
        ),
    )
    con.commit()
    return int(cur.lastrowid)


def update_supervisor_run(
    con: sqlite3.Connection,
    run_id: int,
    total: int,
    launched: int,
    succeeded: int,
    failed: int,
    crashed: int,
    skipped: int,
    pages: int,
    *,
    status: Optional[str] = None,
    message: Optional[str] = None,
) -> None:
    now = utcnow()
    summary = {
        "provider": "azure",
        "documents_total": total,
        "documents_completed": launched,
        "succeeded": succeeded,
        "failed": failed,
        "crashed": crashed,
        "skipped": skipped,
        "pages_succeeded": pages,
        "worker_launches": launched,
        "concurrency": 1,
        "same_run_retries": 0,
    }
    progress = message or (
        f"Azure {launched}/{total}: {succeeded} succeeded, {failed} failed, "
        f"{crashed} crashed, {skipped} skipped"
    )
    if status is None:
        con.execute(
            """
            UPDATE runs
            SET heartbeat_at=?, completed_units=?, total_units=?, summary_json=?,
                progress_message=?, successful_requests=?, failed_requests=?
            WHERE id=?
            """,
            (
                now,
                launched,
                total,
                json.dumps(summary, sort_keys=True),
                progress,
                succeeded,
                failed + crashed,
                run_id,
            ),
        )
    else:
        con.execute(
            """
            UPDATE runs
            SET status=?, finished_at=?, heartbeat_at=?, current_phase='finished',
                completed_units=?, total_units=?, summary_json=?, progress_message=?,
                successful_requests=?, failed_requests=?
            WHERE id=?
            """,
            (
                status,
                now,
                now,
                launched,
                total,
                json.dumps(summary, sort_keys=True),
                progress,
                succeeded,
                failed + crashed,
                run_id,
            ),
        )
    con.commit()


def worker_lock_path(supervisor_lock: Path) -> Path:
    return supervisor_lock.with_name(supervisor_lock.name + ".worker")


def child_bootstrap(worker: Path) -> str:
    """Return code that binds the legacy worker to the supervisor-owned run.

    The worker remains directly executable for development, but public supervisor
    execution replaces its run-lifecycle helpers so it cannot allocate or finish
    a second ``runs`` row. Analyses and errors still receive the returned parent
    run ID normally.
    """
    return f"""
import sys
sys.path.insert(0, {str(worker.parent)!r})
import regdocs_3_azure_worker as worker
parent_run_id = int(sys.argv[1])
worker.create_run = lambda con, args: parent_run_id
worker.update_run_progress = lambda *args, **kwargs: None
worker.finish_run = lambda *args, **kwargs: None
sys.argv = [worker.__file__] + sys.argv[2:]
raise SystemExit(worker.main())
""".strip()


def child_command(args: argparse.Namespace, document_id: str, run_id: int) -> list[str]:
    worker = Path(__file__).with_name("regdocs_3_azure_worker.py")
    cmd = [
        sys.executable,
        "-c",
        child_bootstrap(worker),
        str(run_id),
        "--db", str(args.db),
        "--api-version", args.api_version,
        "--polling-interval", str(args.polling_interval),
        "--max-attempts", "1",
        "--download-dir", str(args.download_dir),
        "--output-dir", str(args.output_dir),
        "--lock-file", str(worker_lock_path(args.lock_file)),
        "--analyzer-id", args.analyzer_id,
        "--document-id", document_id,
    ]
    if args.force:
        cmd.append("--force")
    if args.no_reconcile_artifacts:
        cmd.append("--no-reconcile-artifacts")
    if args.no_verify_hash:
        cmd.append("--no-verify-hash")
    if args.dry_run:
        cmd.append("--dry-run")
    return cmd


def child_environment(args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()
    if args.endpoint:
        env["CONTENTUNDERSTANDING_ENDPOINT"] = args.endpoint
    if args.key:
        env["CONTENTUNDERSTANDING_KEY"] = args.key
    env["CONTENTUNDERSTANDING_API_VERSION"] = args.api_version
    env["CONTENTUNDERSTANDING_ANALYZER_ID"] = args.analyzer_id
    env.pop("CONTENTUNDERSTANDING_MAX_ATTEMPTS", None)
    return env


def current_document_analysis(
    con: sqlite3.Connection,
    document_id: str,
    analyzer_id: str,
    api_version: str,
) -> Optional[sqlite3.Row]:
    needed = {"file_id", "file_sha256", "analyzer_id", "api_version", "status"}
    if not needed.issubset(_analysis_columns(con)):
        return None
    return con.execute(
        """
        SELECT
            a.status,
            a.error_code,
            a.error_message,
            a.page_count,
            a.table_count,
            a.section_count,
            a.elapsed_seconds
        FROM files f
        JOIN analyses a ON a.file_id=f.id AND a.file_sha256=f.sha256
        WHERE f.is_current=1 AND f.document_id=?
          AND a.analyzer_id=? AND a.api_version=?
        ORDER BY a.id DESC
        LIMIT 1
        """,
        (document_id, analyzer_id, api_version),
    ).fetchone()


def mark_worker_crash(
    con: sqlite3.Connection,
    document_id: str,
    analyzer_id: str,
    api_version: str,
    message: str,
) -> None:
    needed = {
        "file_id", "file_sha256", "analyzer_id", "api_version", "status",
        "finished_at", "error_code", "error_message", "updated_at",
    }
    if not needed.issubset(_analysis_columns(con)):
        return
    now = utcnow()
    con.execute(
        """
        UPDATE analyses
        SET status='FAILED', finished_at=?, error_code='WORKER_CRASH',
            error_message=?, updated_at=?
        WHERE id=(
            SELECT a.id
            FROM files f
            JOIN analyses a ON a.file_id=f.id AND a.file_sha256=f.sha256
            WHERE f.is_current=1 AND f.document_id=?
              AND a.analyzer_id=? AND a.api_version=?
            ORDER BY a.id DESC
            LIMIT 1
        )
        """,
        (now, message[:4000], now, document_id, analyzer_id, api_version),
    )
    con.commit()


def returncode_signal(returncode: int) -> Optional[str]:
    if returncode >= 0:
        return None
    try:
        return signal.Signals(-returncode).name
    except ValueError:
        return str(-returncode)


def print_worker_diagnostics(stdout: str, stderr: str) -> None:
    blocks = []
    if stdout.strip():
        blocks.append(stdout.rstrip())
    if stderr.strip():
        blocks.append(stderr.rstrip())
    if not blocks:
        return
    print("    worker diagnostics:", file=sys.stderr)
    for block in blocks:
        for line in block.splitlines():
            print(f"      {line}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "REGDOCS Stage 3 Azure: durable single-threaded analysis with no "
            "automatic resubmission retries"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--db", type=Path, default=stored_path(DATABASE_PATH))
    p.add_argument("--endpoint", default=os.environ.get("CONTENTUNDERSTANDING_ENDPOINT"))
    p.add_argument("--key", default=os.environ.get("CONTENTUNDERSTANDING_KEY"))
    p.add_argument(
        "--api-version",
        default=os.environ.get("CONTENTUNDERSTANDING_API_VERSION", DEFAULT_API_VERSION),
    )
    p.add_argument(
        "--polling-interval",
        type=float,
        default=float(os.environ.get("CONTENTUNDERSTANDING_POLLING_INTERVAL", DEFAULT_POLLING_INTERVAL)),
    )
    p.add_argument("--download-dir", type=Path, default=stored_path(DOWNLOAD_FILES_DIR))
    p.add_argument("--output-dir", type=Path, default=stored_path(CONTENT_UNDERSTANDING_DIR))
    p.add_argument("--lock-file", type=Path, default=stored_path(ANALYZE_LOCK_PATH))
    p.add_argument("--force-lock", action="store_true")
    p.add_argument(
        "--state-file",
        type=Path,
        default=stored_path(DEFAULT_STATE_FILE),
        help="Durable supervisor process-history state",
    )
    p.add_argument(
        "--worker-sleep-seconds",
        type=float,
        default=DEFAULT_WORKER_SLEEP_SECONDS,
        help="Pause between document worker processes",
    )
    p.add_argument(
        "--analyzer-id",
        default=os.environ.get("CONTENTUNDERSTANDING_ANALYZER_ID", DEFAULT_ANALYZER_ID),
    )
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--limit", type=int, help="Process at most N eligible documents")
    scope.add_argument("--document-id", help="Analyze one REGDOCS document ID")
    scope.add_argument("--all", dest="all_candidates", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-reconcile-artifacts", action="store_true")
    p.add_argument("--no-verify-hash", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    args.db = resolve_stored_path(args.db)
    args.download_dir = resolve_stored_path(args.download_dir)
    args.output_dir = resolve_stored_path(args.output_dir)
    args.lock_file = resolve_stored_path(args.lock_file)
    args.state_file = resolve_stored_path(args.state_file)

    if not args.db.is_file():
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 1:
        print("ERROR: --limit must be at least 1", file=sys.stderr)
        return 2
    if args.polling_interval <= 0:
        print("ERROR: --polling-interval must be greater than 0", file=sys.stderr)
        return 2
    if args.worker_sleep_seconds < 0:
        print("ERROR: --worker-sleep-seconds cannot be negative", file=sys.stderr)
        return 2

    worker = Path(__file__).with_name("regdocs_3_azure_worker.py")
    if not worker.is_file():
        print(f"ERROR: Azure worker not found: {worker}", file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(args.state_file)

    try:
        lock = StageLock(args.lock_file, force=args.force_lock)
        lock.__enter__()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    con = open_db(args.db)
    started = time.monotonic()
    run_id: Optional[int] = None
    total = launched = succeeded = handled_failed = crashed = skipped = pages_succeeded = 0

    try:
        selected = select_documents(
            con,
            state,
            args.output_dir,
            args.analyzer_id,
            args.api_version,
            args.document_id,
            args.limit,
            args.force,
        )
        total = len(selected)
        save_state(args.state_file, state)
        run_id = create_supervisor_run(con, args, total)

        print(f"Run {run_id}: Azure supervisor {SCRIPT_VERSION}; {total} document(s) selected")
        print("Concurrency:       1 child process")
        print("Azure retries:     disabled")
        print("Retry boundary:    next normal Stage 3 Azure rerun")
        print()

        if total == 0:
            update_supervisor_run(
                con, run_id, total, 0, 0, 0, 0, 0, 0,
                status="SUCCEEDED",
                message="Azure supervisor: no eligible documents",
            )
            return 0

        progress_width = len(str(total))

        for index, document_id in enumerate(selected, start=1):
            info = state["documents"].setdefault(document_id, {})
            if not isinstance(info, dict):
                info = {}
                state["documents"][document_id] = info

            info["worker_launches"] = int(info.get("worker_launches") or 0) + 1
            info["last_started_at"] = utcnow()
            info["last_exit_code"] = None
            info["last_signal"] = None
            info["last_run_status"] = "RUNNING"
            info["last_pipeline_run_id"] = run_id
            save_state(args.state_file, state)

            launched += 1
            print(
                f"[{index:0{progress_width}d}/{total}] {document_id} ... ",
                end="",
                flush=True,
            )

            try:
                result = subprocess.run(
                    child_command(args, document_id, run_id),
                    env=child_environment(args),
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                returncode = int(result.returncode)
                worker_stdout = result.stdout or ""
                worker_stderr = result.stderr or ""
            except KeyboardInterrupt:
                info["last_run_status"] = "INTERRUPTED"
                save_state(args.state_file, state)
                print("INTERRUPTED")
                update_supervisor_run(
                    con, run_id, total, launched - 1, succeeded, handled_failed,
                    crashed, skipped, pages_succeeded, status="INTERRUPTED",
                    message="Azure supervisor interrupted by user",
                )
                print(
                    "Azure supervisor interrupted; committed analyses remain attached "
                    f"to Run {run_id} and will be skipped on restart.",
                    file=sys.stderr,
                )
                return 130

            info["last_finished_at"] = utcnow()
            info["last_exit_code"] = returncode
            info["last_signal"] = returncode_signal(returncode)
            save_state(args.state_file, state)

            con.close()
            con = open_db(args.db)
            analysis = current_document_analysis(
                con, document_id, args.analyzer_id, args.api_version
            )
            status = str(analysis["status"]) if analysis is not None else None
            error_code = str(analysis["error_code"] or "") if analysis is not None else ""

            if returncode == 130:
                info["last_run_status"] = "INTERRUPTED"
                save_state(args.state_file, state)
                print("INTERRUPTED")
                print_worker_diagnostics(worker_stdout, worker_stderr)
                update_supervisor_run(
                    con, run_id, total, launched, succeeded, handled_failed,
                    crashed, skipped, pages_succeeded, status="INTERRUPTED",
                    message=f"Azure worker interrupted on {document_id}",
                )
                return 130

            if returncode == 2:
                info["last_run_status"] = "CONFIG_ERROR"
                save_state(args.state_file, state)
                print("CONFIG_ERROR")
                print_worker_diagnostics(worker_stdout, worker_stderr)
                update_supervisor_run(
                    con, run_id, total, launched, succeeded, handled_failed,
                    crashed, skipped, pages_succeeded, status="FAILED",
                    message=f"Azure worker configuration/input error on {document_id}",
                )
                return 2

            if returncode == 0:
                info["last_run_status"] = status or "NO_ANALYSIS_ROW"
                info["completed_at"] = utcnow()
                save_state(args.state_file, state)
                if status == "SUCCEEDED":
                    succeeded += 1
                    doc_pages = int(analysis["page_count"] or 0) if analysis is not None else 0
                    doc_tables = int(analysis["table_count"] or 0) if analysis is not None else 0
                    doc_sections = int(analysis["section_count"] or 0) if analysis is not None else 0
                    doc_elapsed = analysis["elapsed_seconds"] if analysis is not None else None
                    elapsed_text = (
                        f"{float(doc_elapsed):.1f}s" if doc_elapsed is not None else "n/a"
                    )
                    pages_succeeded += doc_pages
                    print(
                        f"SUCCEEDED pages={doc_pages} tables={doc_tables} "
                        f"sections={doc_sections} elapsed={elapsed_text}",
                        flush=True,
                    )
                else:
                    skipped += 1
                    print(f"{status or 'NO_ANALYSIS_ROW'}", flush=True)
                    print_worker_diagnostics(worker_stdout, worker_stderr)
            elif status == "FAILED" and error_code != "WORKER_CRASH":
                info["last_run_status"] = "FAILED"
                info["completed_at"] = utcnow()
                save_state(args.state_file, state)
                handled_failed += 1
                print(f"FAILED {error_code or 'ANALYZE_FAILED'}")
                print_worker_diagnostics(worker_stdout, worker_stderr)
            else:
                signal_name = info.get("last_signal")
                detail = f" signal={signal_name}" if signal_name else ""
                crash_message = (
                    f"Azure worker exited {returncode}{detail} before recording a normal "
                    "terminal result"
                )
                mark_worker_crash(
                    con,
                    document_id,
                    args.analyzer_id,
                    args.api_version,
                    crash_message,
                )
                info["last_run_status"] = "WORKER_CRASH"
                info["crash_count"] = int(info.get("crash_count") or 0) + 1
                info["last_crashed_at"] = utcnow()
                save_state(args.state_file, state)
                crashed += 1
                print(f"WORKER_CRASH exit={returncode}{detail}")
                print_worker_diagnostics(worker_stdout, worker_stderr)

            update_supervisor_run(
                con,
                run_id,
                total,
                launched,
                succeeded,
                handled_failed,
                crashed,
                skipped,
                pages_succeeded,
            )

            if args.worker_sleep_seconds:
                time.sleep(args.worker_sleep_seconds)

        elapsed = time.monotonic() - started
        final_status = "SUCCEEDED" if handled_failed == 0 and crashed == 0 else "COMPLETED_WITH_ERRORS"
        update_supervisor_run(
            con,
            run_id,
            total,
            launched,
            succeeded,
            handled_failed,
            crashed,
            skipped,
            pages_succeeded,
            status=final_status,
            message=(
                f"Azure {final_status}: {succeeded} succeeded, {handled_failed} failed, "
                f"{crashed} crashed, {skipped} skipped, {pages_succeeded} pages, "
                f"elapsed={elapsed:.1f}s"
            ),
        )
        print()
        print(
            f"Run {run_id} {final_status}: selected={total} succeeded={succeeded} "
            f"failed={handled_failed} crashed={crashed} skipped={skipped} "
            f"pages={pages_succeeded} elapsed={elapsed:.1f}s concurrency=1 retries=0"
        )
        return 0 if final_status == "SUCCEEDED" else 1

    except KeyboardInterrupt:
        if run_id is not None:
            with contextlib.suppress(Exception):
                update_supervisor_run(
                    con, run_id, total, launched, succeeded, handled_failed,
                    crashed, skipped, pages_succeeded, status="INTERRUPTED",
                    message="Azure supervisor interrupted by user",
                )
        print("\nAzure supervisor interrupted; committed analyses are preserved.", file=sys.stderr)
        return 130
    except Exception as exc:
        if run_id is not None:
            with contextlib.suppress(Exception):
                update_supervisor_run(
                    con, run_id, total, launched, succeeded, handled_failed,
                    crashed, skipped, pages_succeeded, status="FAILED",
                    message=f"Azure supervisor failed: {str(exc)[:800]}",
                )
        print(f"FATAL: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        con.close()
        lock.__exit__()


if __name__ == "__main__":
    raise SystemExit(main())
