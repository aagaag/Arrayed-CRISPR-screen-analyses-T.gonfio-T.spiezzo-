from __future__ import annotations

import json
import sqlite3
import re
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


PUBLIC_USER_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PUBLIC_USER_ID_MIN_LENGTH = 3
PUBLIC_USER_ID_MAX_LENGTH = 48


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat()


def _to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y"}


class MetadataStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        schema_statements = [
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS datasets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                archived_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS dataset_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dataset_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                role TEXT NOT NULL,
                filename TEXT NOT NULL,
                path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                checksum TEXT,
                uploaded_by TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                FOREIGN KEY (dataset_id) REFERENCES datasets(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                dataset_id INTEGER,
                dataset_version INTEGER,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                params_json TEXT NOT NULL,
                started_by TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                log_path TEXT,
                output_dir TEXT,
                error_text TEXT,
                FOREIGN KEY (dataset_id) REFERENCES datasets(id) ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS run_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                filename TEXT NOT NULL,
                path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS access_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                requested_by_username TEXT,
                requested_at TEXT NOT NULL,
                note TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                decided_at TEXT,
                decided_by TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_datasets_archived_at ON datasets(archived_at)",
            "CREATE INDEX IF NOT EXISTS idx_dataset_files_dataset_version ON dataset_files(dataset_id, version)",
            "CREATE INDEX IF NOT EXISTS idx_runs_status_started_at ON runs(status, started_at)",
            "CREATE INDEX IF NOT EXISTS idx_runs_started_by ON runs(started_by)",
            "CREATE INDEX IF NOT EXISTS idx_run_artifacts_run_id ON run_artifacts(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_access_requests_email ON access_requests(email)",
            "CREATE INDEX IF NOT EXISTS idx_access_requests_status ON access_requests(status)",
        ]
        with self._lock, self._connect() as conn:
            for statement in schema_statements:
                conn.execute(statement)
            self._ensure_column(conn, "users", "email", "TEXT")
            self._ensure_column(conn, "users", "password_hash", "TEXT")
            self._ensure_column(conn, "users", "status", "TEXT NOT NULL DEFAULT 'approved'")
            self._ensure_column(conn, "users", "approved_at", "TEXT")
            self._ensure_column(conn, "users", "approved_by", "TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users(email) WHERE email IS NOT NULL")
            conn.execute("UPDATE users SET status='approved' WHERE status IS NULL OR TRIM(status)=''")

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r["name"]) for r in rows}

    @classmethod
    def _ensure_column(cls, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        if column in cls._table_columns(conn, table):
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @staticmethod
    def _normalized_email(email: str) -> str:
        return (email or "").strip().lower()

    @staticmethod
    def _status_text(value: object) -> str:
        text = str(value or "").strip().lower()
        return text if text else "approved"

    @staticmethod
    def _base_username_from_email(email: str) -> str:
        local = email.split("@", 1)[0]
        out = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in local)
        out = out.strip("._-")
        return out[:48] if out else "user"

    def _next_available_username(self, conn: sqlite3.Connection, base: str) -> str:
        candidate = base
        idx = 2
        while True:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE lower(username)=lower(?) LIMIT 1",
                (candidate,),
            ).fetchone()
            if not exists:
                return candidate
            candidate = f"{base}_{idx}"
            idx += 1

    def upsert_user(self, username: str, *, is_admin: bool = False) -> None:
        clean = (username or "").strip()
        if not clean:
            clean = "unknown"
        now = _utc_now_iso()
        admin_flag = 1 if _to_bool(is_admin) else 0
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (username, is_admin, created_at, status)
                VALUES (?, ?, ?, 'approved')
                ON CONFLICT(username) DO UPDATE SET
                    is_admin = MAX(users.is_admin, excluded.is_admin)
                """,
                (clean, admin_flag, now),
            )

    def has_admin_login(self) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM users
                WHERE is_admin = 1
                  AND lower(COALESCE(status, 'approved')) = 'approved'
                  AND COALESCE(TRIM(password_hash), '') != ''
                """
            ).fetchone()
        return bool(row and int(row["n"]) > 0)

    def healthcheck(self) -> dict[str, Any]:
        """Probe sqlite availability and writeability without mutating persistent data."""
        with self._connect() as conn:
            conn.execute("SELECT 1").fetchone()
            writable = True
            error = ""
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError as exc:
                writable = False
                error = str(exc)
        return {
            "db_path": str(self.db_path),
            "writable": writable,
            "error": error,
        }

    def ensure_admin_account(self, email: str, password_hash: str, *, approved_by: str = "bootstrap") -> None:
        clean_email = self._normalized_email(email)
        if not clean_email:
            raise ValueError("Admin email must be provided.")
        if not password_hash:
            raise ValueError("Admin password hash must be provided.")
        now = _utc_now_iso()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id, username FROM users WHERE lower(email)=lower(?) LIMIT 1",
                (clean_email,),
            ).fetchone()
            if row is None:
                base = self._base_username_from_email(clean_email)
                username = self._next_available_username(conn, base)
                conn.execute(
                    """
                    INSERT INTO users (username, email, password_hash, status, is_admin, approved_at, approved_by, created_at)
                    VALUES (?, ?, ?, 'approved', 1, ?, ?, ?)
                    """,
                    (username, clean_email, password_hash, now, approved_by, now),
                )
                return
            conn.execute(
                """
                UPDATE users
                SET email = ?,
                    password_hash = ?,
                    status = 'approved',
                    is_admin = 1,
                    approved_at = COALESCE(approved_at, ?),
                    approved_by = COALESCE(approved_by, ?)
                WHERE id = ?
                """,
                (clean_email, password_hash, now, approved_by, int(row["id"])),
            )

    def create_access_request(
        self,
        *,
        email: str,
        requested_by_username: str = "",
        password_hash: str,
        note: str = "",
    ) -> dict[str, Any]:
        clean_email = self._normalized_email(email)
        requested_public_username = (requested_by_username or "").strip()
        if not clean_email:
            raise ValueError("Email is required.")
        if not requested_public_username:
            raise ValueError("User ID is required.")
        if "@" in requested_public_username:
            raise ValueError("User ID must not be an email address.")
        if (
            len(requested_public_username) < PUBLIC_USER_ID_MIN_LENGTH
            or len(requested_public_username) > PUBLIC_USER_ID_MAX_LENGTH
        ):
            raise ValueError(
                f"User ID must be between {PUBLIC_USER_ID_MIN_LENGTH} and {PUBLIC_USER_ID_MAX_LENGTH} characters."
            )
        if not PUBLIC_USER_ID_RE.fullmatch(requested_public_username):
            raise ValueError("User ID can use letters, digits, dot (.), underscore (_), and hyphen (-) only.")

        now = _utc_now_iso()
        clean_note = (note or "").strip()
        clean_requester = (requested_by_username or "").strip()

        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT id, username, status FROM users WHERE lower(email)=lower(?) LIMIT 1",
                (clean_email,),
            ).fetchone()
            username_holder = conn.execute(
                "SELECT id FROM users WHERE lower(username)=lower(?) LIMIT 1",
                (requested_public_username,),
            ).fetchone()
            if existing is None:
                if username_holder is not None:
                    raise ValueError("This user ID is already taken.")
                username = requested_public_username
                conn.execute(
                    """
                    INSERT INTO users (username, email, password_hash, status, is_admin, created_at)
                    VALUES (?, ?, ?, 'pending', 0, ?)
                    """,
                    (requested_public_username, clean_email, password_hash, now),
                )
            else:
                status = self._status_text(existing["status"])
                if status == "approved":
                    raise ValueError("This email is already approved.")
                current_user_id = int(existing["id"])
                if username_holder is not None and int(username_holder["id"]) != current_user_id:
                    raise ValueError("This user ID is already taken.")
                username = requested_public_username
                conn.execute(
                    """
                    UPDATE users
                    SET username = ?,
                        password_hash = ?,
                        status = 'pending',
                        approved_at = NULL,
                        approved_by = NULL
                    WHERE id = ?
                    """,
                    (requested_public_username, password_hash, current_user_id),
                )

            conn.execute(
                """
                INSERT INTO access_requests (email, requested_by_username, requested_at, note, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (clean_email, clean_requester or requested_public_username, now, clean_note),
            )
            req_id_row = conn.execute("SELECT last_insert_rowid() AS rid").fetchone()
            req_id = int(req_id_row["rid"]) if req_id_row else 0
        return {"request_id": req_id, "email": clean_email, "username": username}

    def list_access_requests(self, *, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        args: list[Any] = []
        where = ""
        if status:
            where = "WHERE lower(ar.status)=lower(?)"
            args.append(status)
        args.append(safe_limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT ar.id, ar.email, ar.requested_by_username, ar.requested_at, ar.note, ar.status, ar.decided_at, ar.decided_by,
                       u.username, u.is_admin, u.status AS user_status
                FROM access_requests ar
                LEFT JOIN users u ON lower(u.email)=lower(ar.email)
                {where}
                ORDER BY ar.requested_at DESC, ar.id DESC
                LIMIT ?
                """,
                tuple(args),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_access_request(self, request_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT ar.id, ar.email, ar.requested_by_username, ar.requested_at, ar.note, ar.status, ar.decided_at, ar.decided_by,
                       u.username, u.is_admin, u.status AS user_status
                FROM access_requests ar
                LEFT JOIN users u ON lower(u.email)=lower(ar.email)
                WHERE ar.id = ?
                LIMIT 1
                """,
                (int(request_id),),
            ).fetchone()
        return dict(row) if row else None

    def decide_access_request(self, request_id: int, *, approve: bool, decided_by: str) -> dict[str, Any]:
        now = _utc_now_iso()
        decision = "approved" if approve else "rejected"
        with self._lock, self._connect() as conn:
            req = conn.execute(
                "SELECT id, email, status FROM access_requests WHERE id=?",
                (int(request_id),),
            ).fetchone()
            if req is None:
                raise ValueError("Access request not found.")
            if self._status_text(req["status"]) != "pending":
                raise ValueError("Access request is already decided.")

            conn.execute(
                """
                UPDATE access_requests
                SET status = ?, decided_at = ?, decided_by = ?
                WHERE id = ?
                """,
                (decision, now, decided_by, int(request_id)),
            )

            if approve:
                conn.execute(
                    """
                    UPDATE users
                    SET status = 'approved',
                        approved_at = ?,
                        approved_by = ?
                    WHERE lower(email)=lower(?)
                    """,
                    (now, decided_by, str(req["email"])),
                )
            else:
                conn.execute(
                    """
                    UPDATE users
                    SET status = 'disabled'
                    WHERE lower(email)=lower(?)
                      AND lower(COALESCE(status, '')) = 'pending'
                    """,
                    (str(req["email"]),),
                )

            user = conn.execute(
                """
                SELECT id, username, email, is_admin, status, approved_at, approved_by, created_at
                FROM users
                WHERE lower(email)=lower(?)
                LIMIT 1
                """,
                (str(req["email"]),),
            ).fetchone()
        return dict(user) if user else {"email": str(req["email"])}

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        clean_email = self._normalized_email(email)
        if not clean_email:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, username, email, password_hash, is_admin, status, approved_at, approved_by, created_at
                FROM users
                WHERE lower(email)=lower(?)
                LIMIT 1
                """,
                (clean_email,),
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        clean = (username or "").strip()
        if not clean:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, username, email, password_hash, is_admin, status, approved_at, approved_by, created_at
                FROM users
                WHERE lower(username)=lower(?)
                LIMIT 1
                """,
                (clean,),
            ).fetchone()
        return dict(row) if row else None

    def set_user_admin_by_username(self, username: str, *, is_admin: bool = True, require_approved: bool = True) -> bool:
        clean = (username or "").strip()
        if not clean:
            return False
        flag = 1 if _to_bool(is_admin) else 0
        with self._lock, self._connect() as conn:
            if require_approved:
                row = conn.execute(
                    """
                    UPDATE users
                    SET is_admin = ?
                    WHERE lower(username)=lower(?)
                      AND lower(COALESCE(status, 'approved')) = 'approved'
                    """,
                    (flag, clean),
                )
            else:
                row = conn.execute(
                    "UPDATE users SET is_admin = ? WHERE lower(username)=lower(?)",
                    (flag, clean),
                )
        return bool(row and int(row.rowcount or 0) > 0)

    def set_user_admin_by_email(self, email: str, *, is_admin: bool = True, require_approved: bool = True) -> bool:
        clean_email = self._normalized_email(email)
        if not clean_email:
            return False
        flag = 1 if _to_bool(is_admin) else 0
        with self._lock, self._connect() as conn:
            if require_approved:
                row = conn.execute(
                    """
                    UPDATE users
                    SET is_admin = ?
                    WHERE lower(email)=lower(?)
                      AND lower(COALESCE(status, 'approved')) = 'approved'
                    """,
                    (flag, clean_email),
                )
            else:
                row = conn.execute(
                    "UPDATE users SET is_admin = ? WHERE lower(email)=lower(?)",
                    (flag, clean_email),
                )
        return bool(row and int(row.rowcount or 0) > 0)

    def set_user_status_by_username(
        self,
        username: str,
        *,
        status: str,
        clear_admin: bool = False,
    ) -> bool:
        clean = (username or "").strip()
        target_status = str(status or "").strip().lower()
        if not clean or not target_status:
            return False
        with self._lock, self._connect() as conn:
            if clear_admin:
                row = conn.execute(
                    """
                    UPDATE users
                    SET status = ?, is_admin = 0
                    WHERE lower(username)=lower(?)
                    """,
                    (target_status, clean),
                )
            else:
                row = conn.execute(
                    "UPDATE users SET status = ? WHERE lower(username)=lower(?)",
                    (target_status, clean),
                )
        return bool(row and int(row.rowcount or 0) > 0)

    def approve_user_by_username(self, username: str, *, approved_by: str) -> bool:
        clean = (username or "").strip()
        actor = (approved_by or "").strip() or "admin"
        if not clean:
            return False
        now = _utc_now_iso()
        with self._lock, self._connect() as conn:
            user = conn.execute(
                "SELECT email FROM users WHERE lower(username)=lower(?) LIMIT 1",
                (clean,),
            ).fetchone()
            if user is None:
                return False
            row = conn.execute(
                """
                UPDATE users
                SET status = 'approved',
                    approved_at = ?,
                    approved_by = ?
                WHERE lower(username)=lower(?)
                """,
                (now, actor, clean),
            )
            email = str(user["email"] or "").strip().lower()
            if email:
                conn.execute(
                    """
                    UPDATE access_requests
                    SET status = 'approved',
                        decided_at = ?,
                        decided_by = ?
                    WHERE lower(email)=lower(?)
                      AND lower(COALESCE(status, '')) = 'pending'
                    """,
                    (now, actor, email),
                )
        return bool(row and int(row.rowcount or 0) > 0)

    def decide_pending_access_requests_by_email(self, email: str, *, approve: bool, decided_by: str) -> int:
        clean_email = self._normalized_email(email)
        actor = (decided_by or "").strip() or "admin"
        if not clean_email:
            return 0
        decided_status = "approved" if approve else "rejected"
        now = _utc_now_iso()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                UPDATE access_requests
                SET status = ?,
                    decided_at = ?,
                    decided_by = ?
                WHERE lower(email)=lower(?)
                  AND lower(COALESCE(status, '')) = 'pending'
                """,
                (decided_status, now, actor, clean_email),
            )
        return int(row.rowcount or 0)

    def create_run(
        self,
        *,
        run_id: str,
        mode: str,
        status: str,
        params: dict[str, Any],
        started_by: str,
        dataset_id: int | None = None,
        dataset_version: int | None = None,
        output_dir: str | None = None,
        log_path: str | None = None,
    ) -> None:
        now = _utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    id,
                    dataset_id,
                    dataset_version,
                    mode,
                    status,
                    params_json,
                    started_by,
                    started_at,
                    output_dir,
                    log_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    dataset_id,
                    dataset_version,
                    mode,
                    status,
                    json.dumps(params, ensure_ascii=True),
                    started_by,
                    now,
                    output_dir or "",
                    log_path or "",
                ),
            )

    def set_run_status(
        self,
        run_id: str,
        *,
        status: str,
        error_text: str | None = None,
        finished: bool = False,
        output_dir: str | None = None,
    ) -> None:
        finished_at = _utc_now_iso() if finished else None
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?,
                    error_text = ?,
                    finished_at = COALESCE(?, finished_at),
                    output_dir = COALESCE(?, output_dir)
                WHERE id = ?
                """,
                (status, error_text, finished_at, output_dir, run_id),
            )

    def list_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, dataset_id, dataset_version, mode, status, started_by, started_at, finished_at, output_dir, error_text
                FROM runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, dataset_id, dataset_version, mode, status, params_json, started_by, started_at, finished_at, output_dir, error_text
                FROM runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        rec = dict(row)
        try:
            rec["params"] = json.loads(rec.pop("params_json") or "{}")
        except json.JSONDecodeError:
            rec["params"] = {}
            rec.pop("params_json", None)
        return rec

    def list_users(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, username, email, is_admin, status, approved_at, approved_by, created_at
                FROM users
                ORDER BY username COLLATE NOCASE ASC
                """
            ).fetchall()
        users: list[dict[str, Any]] = []
        for row in rows:
            rec = dict(row)
            rec["is_admin"] = bool(rec.get("is_admin"))
            rec["status"] = self._status_text(rec.get("status"))
            users.append(rec)
        return users

    def summary(self) -> dict[str, int]:
        with self._connect() as conn:
            users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            datasets = conn.execute("SELECT COUNT(*) FROM datasets WHERE archived_at IS NULL").fetchone()[0]
            runs_total = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            runs_active = conn.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued','running')").fetchone()[0]
        return {
            "users": int(users),
            "datasets": int(datasets),
            "runs_total": int(runs_total),
            "runs_active": int(runs_active),
        }
