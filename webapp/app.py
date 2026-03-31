from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import shutil
import smtplib
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

from webapp.metadata_store import MetadataStore


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = "results"
DEFAULT_MODE = "arrayed"
DEFAULT_SHEET = "skylineplot2"
DEFAULT_SKIP_FRET = 38
DEFAULT_SKIP_GLO = 9
DEFAULT_HEATMAP_PLATE = "all"
VALID_MODES = {"arrayed", "pooled"}
HEATMAP_SELECTOR_RE = re.compile(r"^\d+$|^\d+\s*-\s*\d+$|^\d+(?:\s*,\s*\d+)+$|^all$", re.IGNORECASE)
REQUIRED_SKYLINE_COLUMNS = ("gene", "log2fc", "chrom", "pos")
SKYLINE_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "gene": ("Gene_symbol", "Gene symbol", "GeneSymbol", "Gene", "Symbol", "gene"),
    "log2fc": ("Mean_log2FC", "Mean_log2", "mean_log2fc", "mean_log2", "log2fc"),
    "chrom": ("Chromosome", "chromosome", "Chrom", "Chr", "chrom"),
    "pos": ("Start_Position", "Start position", "StartPosition", "Start", "pos"),
}
RAW_FILE_EXTENSIONS = {".csv", ".tsv", ".txt"}
EXCEL_FILE_EXTENSIONS = {".xlsx", ".xls"}
SCAN_FILE_EXTENSIONS = RAW_FILE_EXTENSIONS | EXCEL_FILE_EXTENSIONS
LAYOUT_FILE_EXTENSIONS = {".csv", ".xlsx", ".xls", ".xlsm"}
GENOMICS_FILE_EXTENSIONS = {".xlsx", ".xls", ".xlsm"}
MAX_GENOMICS_WORKBOOK_PROBES = 6
AUTH_CONTACT_EMAIL = os.getenv("PRPCSCREEN_AUTH_CONTACT_EMAIL", "contact@isab.science").strip() or "contact@isab.science"
SESSION_SECRET = os.getenv("PRPCSCREEN_SESSION_SECRET", "").strip() or "change-me-prpcscreen-session-secret"
APPROVAL_TOKEN_SECRET = os.getenv("PRPCSCREEN_APPROVAL_TOKEN_SECRET", "").strip() or SESSION_SECRET
PUBLIC_BASE_URL = os.getenv("PRPCSCREEN_PUBLIC_BASE_URL", "").strip().rstrip("/")
SESSION_COOKIE_DOMAIN = os.getenv("PRPCSCREEN_SESSION_COOKIE_DOMAIN", "").strip() or None
BOOTSTRAP_ADMIN_EMAIL = os.getenv("PRPCSCREEN_ADMIN_EMAIL", "admin@isab.science").strip() or "admin@isab.science"
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("PRPCSCREEN_ADMIN_PASSWORD", "admin").strip() or "admin"
PASSWORD_MIN_LENGTH = 1
APPROVAL_TOKEN_TTL_SECONDS = int(os.getenv("PRPCSCREEN_APPROVAL_TOKEN_TTL_SECONDS", "604800").strip() or "604800")
PUBLIC_USER_ID_MIN_LENGTH = 3
PUBLIC_USER_ID_MAX_LENGTH = 48
PUBLIC_USER_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PRIMARY_ADMIN_USERNAME = os.getenv("PRPCSCREEN_PRIMARY_ADMIN_USER", "aag").strip() or "aag"
PRIMARY_ADMIN_EMAIL = os.getenv("PRPCSCREEN_PRIMARY_ADMIN_EMAIL", "adriano.aguzzi@isab.science").strip().lower()
SWISS_TZ = ZoneInfo("Europe/Zurich")
LOCAL_BYPASS_HOSTS = {
    host.strip().lower()
    for host in os.getenv(
        "PRPCSCREEN_LOCAL_BYPASS_HOSTS",
        "crispr-tools.lan,localhost,127.0.0.1,127.0.1.1,10.10.20.10,appenzell.internet-box.ch",
    ).split(",")
    if host.strip()
}


def _resolve_metadata_db_path() -> Path:
    configured = os.getenv("PRPCSCREEN_METADATA_DB_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else (REPO_ROOT / path)
    return REPO_ROOT / "webapp" / "state" / "metadata.sqlite3"


METADATA_DB_PATH = _resolve_metadata_db_path()
UPLOADS_ROOT = REPO_ROOT / "webapp" / "state" / "uploads"
CRISPR_SHARED_DATA_ROOT = Path(
    os.getenv("CRISPR_SHARED_DATA_ROOT", "/home/aag/crispr_data/shared").strip() or "/home/aag/crispr_data/shared"
).expanduser()
SHARED_GENOMICS_WORKBOOKS = (
    CRISPR_SHARED_DATA_ROOT / "annotations" / "PrP_genes_and_NT_ordered_AguzziLab.xlsx",
)
DEFAULT_SYSTEM_DATA_ROOT = Path(
    os.getenv("PRPCSCREEN_SYSTEM_DATA_ROOT", "/srv/crispr/ScreenResults").strip() or "/srv/crispr/ScreenResults"
).expanduser()


def _default_data_root() -> str:
    env_override = os.getenv("PRPCSCREEN_DATA_ROOT", "").strip()
    if env_override:
        override_path = Path(env_override).expanduser().resolve()
        return str(override_path) if override_path.exists() else env_override

    home = Path.home()
    suffix = Path("Neuropathology - Manuscripts") / "TrevisanWang2024" / "Data" / "ScreenResults"
    candidates: list[Path] = []
    if sys.platform.startswith("win"):
        for child in home.iterdir() if home.exists() else []:
            if child.is_dir() and "UZH" in child.name and "Universit" in child.name:
                candidates.append(child / suffix)
    else:
        candidates.append(DEFAULT_SYSTEM_DATA_ROOT)
        candidates.append(home / suffix)
        candidates.append(Path("/home/crispr_data/TrevisanWang2024/ScreenResults"))
    for c in candidates:
        if c.exists():
            return str(c.resolve())
    if env_override:
        return env_override
    return str((candidates[0] if candidates else (home / suffix)).resolve())


def _resolve_scan_root(root_text: str) -> Path:
    requested = Path(root_text).expanduser().resolve()
    if requested.exists():
        return requested

    preferred_root = DEFAULT_SYSTEM_DATA_ROOT.resolve()
    if str(requested).replace("\\", "/") == str(preferred_root).replace("\\", "/"):
        return preferred_root if preferred_root.exists() else requested

    # Backward compatibility for old default roots used before shared data mount.
    normalized = str(requested).replace("\\", "/")
    legacy_suffix = "/Neuropathology - Manuscripts/TrevisanWang2024/Data/ScreenResults"
    if normalized.endswith(legacy_suffix):
        if preferred_root.exists():
            return preferred_root
        fallback = Path("/home/crispr_data/TrevisanWang2024/ScreenResults").resolve()
        if fallback.exists():
            return fallback
    return requested


def _shared_genomics_candidates() -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()
    for path in SHARED_GENOMICS_WORKBOOKS:
        expanded = path.expanduser()
        if not expanded.exists():
            continue
        resolved = expanded.resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(resolved)
    return candidates


def _resolve_python() -> str:
    return sys.executable


def _uploads_root() -> Path:
    UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)
    return UPLOADS_ROOT


def _sanitize_upload_filename(filename: str) -> str:
    name = Path(filename or "").name.strip()
    if not name:
        return "upload.bin"
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return clean or "upload.bin"


def _safe_upload_relative_path(relative_path: str, fallback_name: str) -> Path:
    raw = (relative_path or "").replace("\\", "/").strip("/")
    if not raw:
        return Path(_sanitize_upload_filename(fallback_name))
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise HTTPException(status_code=400, detail=f"Unsafe upload path: {relative_path}")
    parts = [_sanitize_upload_filename(part) for part in candidate.parts if part not in {"", "."}]
    if not parts:
        parts = [_sanitize_upload_filename(fallback_name)]
    return Path(*parts)


def _save_upload_stream(upload: UploadFile, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    upload.file.seek(0)
    with destination.open("wb") as handle:
        shutil.copyfileobj(upload.file, handle)


def _find_workbook_skyline_sheets(path_text: str) -> list[str]:
    path = Path(path_text)
    if not path.exists() or path.suffix.lower() not in GENOMICS_FILE_EXTENSIONS:
        return []
    try:
        import pandas as pd
    except Exception:
        return []
    try:
        workbook = pd.ExcelFile(path)
    except Exception:
        return []
    matches: list[str] = []
    try:
        for sheet_name in workbook.sheet_names:
            try:
                header = pd.read_excel(workbook, sheet_name=sheet_name, nrows=0)
            except Exception:
                continue
            if _has_required_skyline_columns(list(header.columns)):
                matches.append(str(sheet_name))
    finally:
        try:
            workbook.close()
        except Exception:
            pass
    return matches


def _is_likely_arrayed_measurement_file(path: Path) -> bool:
    if path.suffix.lower() not in RAW_FILE_EXTENSIONS:
        return False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = [handle.readline().strip() for _ in range(80)]
    except Exception:
        return False
    header_seen = any(
        re.match(r"^,0?1,0?2,0?3,0?4,0?5,0?6,0?7,0?8,0?9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,?$", line)
        for line in lines
    )
    if header_seen:
        return True
    plate_row_count = sum(1 for line in lines if re.match(r"^[A-P],", line))
    return plate_row_count >= 8


def _validate_arrayed_raw_input(raw_path: Path) -> dict[str, Any]:
    if not raw_path.exists():
        raise HTTPException(status_code=400, detail=f"Raw dir/file not found: {raw_path}")
    raw_root = raw_path if raw_path.is_dir() else raw_path.parent
    if raw_path.is_file() and raw_path.suffix.lower() not in RAW_FILE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Arrayed raw input must be a CSV/TSV/TXT plate export or a directory containing such files.",
        )
    candidates = [
        p
        for ext in ("*.csv", "*.tsv", "*.txt")
        for p in raw_root.rglob(ext)
    ]
    candidates = sorted(set(candidates))
    plausible = [p for p in candidates if _is_likely_arrayed_measurement_file(p)]
    if not plausible:
        raise HTTPException(
            status_code=400,
            detail=(
                "Arrayed raw input does not contain recognizable plate export files. "
                "Upload a folder with CSV/TSV/TXT instrument exports."
            ),
        )
    return {
        "path": str(raw_path),
        "mode": "arrayed",
        "kind": "directory" if raw_path.is_dir() else "file",
        "measurement_files": len(plausible),
        "sample_files": [p.name for p in plausible[:5]],
        "message": f"Validated {len(plausible)} arrayed measurement file(s).",
    }


def _validate_pooled_raw_input(raw_path: Path) -> dict[str, Any]:
    if not raw_path.exists():
        raise HTTPException(status_code=400, detail=f"Pooled input file not found: {raw_path}")
    if not raw_path.is_file():
        raise HTTPException(status_code=400, detail="Pooled mode requires one uploaded table file, not a directory.")
    if raw_path.suffix.lower() not in SCAN_FILE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Pooled input must be CSV, TSV, TXT, XLSX, or XLS.")
    try:
        from prpcscreen.analysis.pooled_processing import load_pooled_table, resolve_replicate_columns
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Unable to load pooled validation helpers: {exc}") from exc
    try:
        pooled_df, used_sheet = load_pooled_table(raw_path)
        reference_cols, treatment_cols = resolve_replicate_columns(pooled_df)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail=(
                "Pooled table format is not compatible with the pipeline: "
                f"{exc}. Expected replicate columns like Negative_R1.. and Positive_R1.."
            ),
        ) from exc
    return {
        "path": str(raw_path),
        "mode": "pooled",
        "kind": "file",
        "rows": int(len(pooled_df)),
        "columns": int(len(pooled_df.columns)),
        "sheet": used_sheet,
        "reference_columns": reference_cols,
        "treatment_columns": treatment_cols,
        "message": (
            f"Validated pooled table with {len(reference_cols)} reference and "
            f"{len(treatment_cols)} treatment replicate column(s)."
        ),
    }


def _validate_layout_input(layout_path: Path) -> dict[str, Any]:
    if not layout_path.exists():
        raise HTTPException(status_code=400, detail=f"Layout CSV not found: {layout_path}")
    if layout_path.suffix.lower() not in LAYOUT_FILE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Layout input must be CSV or Excel (.xlsx/.xls/.xlsm).")
    if not _path_has_layout_columns(str(layout_path)):
        raise HTTPException(
            status_code=400,
            detail=(
                "Layout file does not look like the flattened annotation table required for arrayed runs. "
                "It must include Plate_number_384, Well_number_384, Is_NT_ctrl, and Is_pos_ctrl."
            ),
        )
    return {
        "path": str(layout_path),
        "columns_required": list(REQUIRED_LAYOUT_COLUMNS),
        "message": "Validated layout annotation columns.",
    }


def _validate_genomics_input(genomics_path: Path, sheet: str) -> dict[str, Any]:
    if not genomics_path.exists():
        raise HTTPException(status_code=400, detail=f"Genomics Excel not found: {genomics_path}")
    if genomics_path.suffix.lower() not in GENOMICS_FILE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Genomics input must be an Excel workbook (.xlsx/.xls/.xlsm).")
    if _looks_like_layout_workbook(str(genomics_path)):
        raise HTTPException(
            status_code=400,
            detail=(
                "Genomics Excel appears to be a layout workbook. "
                "Select a workbook with skyline columns like Gene_symbol, Mean_log2FC, Chromosome, and Start_Position."
            ),
        )
    matches = _find_workbook_skyline_sheets(str(genomics_path))
    if not matches:
        raise HTTPException(
            status_code=400,
            detail=(
                "Genomics workbook is not skyline-compatible. "
                "No worksheet with Gene_symbol, Mean_log2FC, Chromosome, and Start_Position was found."
            ),
        )
    validate_cmd = [
        _resolve_python(),
        "prpcscreen/scripts/plot_genomic_signal_skyline.py",
        str(genomics_path),
        "--sheet",
        (sheet or DEFAULT_SHEET).strip() or DEFAULT_SHEET,
        "--validate-only",
    ]
    preflight = subprocess.run(
        validate_cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if preflight.returncode != 0:
        detail_lines = [line.strip() for line in (preflight.stdout + "\n" + preflight.stderr).splitlines() if line.strip()]
        detail = " | ".join(detail_lines[:4]) if detail_lines else "Unknown skyline validation error."
        raise HTTPException(status_code=400, detail=f"Genomics workbook failed skyline validation: {detail}")
    return {
        "path": str(genomics_path),
        "skyline_sheets": matches,
        "validated_sheet": (sheet or DEFAULT_SHEET).strip() or DEFAULT_SHEET,
        "message": "Validated genomics workbook for skyline plotting.",
    }


def _validate_mode_inputs(
    *,
    mode: str,
    raw_dir: str,
    layout_csv: str = "",
    genomics_excel: str = "",
    heatmap_plate: str = DEFAULT_HEATMAP_PLATE,
    sheet: str = DEFAULT_SHEET,
) -> dict[str, Any]:
    normalized_mode = _normalize_mode(mode)
    if not normalized_mode:
        raise HTTPException(status_code=400, detail=f"Invalid mode '{mode}'. Use one of: {', '.join(sorted(VALID_MODES))}.")
    if not str(raw_dir or "").strip():
        raise HTTPException(status_code=400, detail="Missing required field: raw_dir")

    raw_path = Path(str(raw_dir).strip()).expanduser()
    checks: dict[str, Any] = {
        "mode": normalized_mode,
        "raw_dir": _validate_arrayed_raw_input(raw_path) if normalized_mode == "arrayed" else _validate_pooled_raw_input(raw_path),
    }

    genomics_text = str(genomics_excel or "").strip()
    if normalized_mode == "arrayed":
        if not str(layout_csv or "").strip():
            raise HTTPException(status_code=400, detail="Missing required field: layout_csv")
        if not genomics_text:
            raise HTTPException(status_code=400, detail="Missing required field: genomics_excel")
        if not _is_valid_heatmap_selector(heatmap_plate):
            raise HTTPException(
                status_code=400,
                detail="Invalid heatmap_plate selector. Use one number (1), a range (1-4), a series (1,2,6), or 'all'.",
            )
        checks["layout_csv"] = _validate_layout_input(Path(str(layout_csv).strip()).expanduser())
        checks["genomics_excel"] = _validate_genomics_input(Path(genomics_text).expanduser(), sheet=sheet)
    elif genomics_text:
        checks["genomics_excel"] = _validate_genomics_input(Path(genomics_text).expanduser(), sheet=sheet)
    return checks


def _is_valid_heatmap_selector(value: str) -> bool:
    return bool(HEATMAP_SELECTOR_RE.fullmatch(value.strip()))


def _normalize_mode(value: str | None) -> str:
    text = (value or "").strip().lower()
    return text if text in VALID_MODES else ""


def _safe_path(path_text: str) -> Path:
    p = Path(path_text)
    p = p if p.is_absolute() else (REPO_ROOT / p)
    r = p.resolve()
    repo_resolved = REPO_ROOT.resolve()
    try:
        r.relative_to(repo_resolved)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Path outside repository is not allowed: {path_text}") from exc
    return r


def _looks_like_layout_workbook(path_text: str) -> bool:
    p = Path(path_text)
    name = p.name.lower()
    norm = str(p).lower().replace("\\", "/")
    if re.fullmatch(r"layout(?:\.(xlsx|xls))?", name):
        return True
    return "/layout/" in norm


REQUIRED_LAYOUT_COLUMNS = {"plate_number_384", "well_number_384", "is_nt_ctrl", "is_pos_ctrl"}


def _has_required_layout_columns(columns: list[object]) -> bool:
    lookup = {str(c).strip().lower() for c in columns if str(c).strip()}
    return REQUIRED_LAYOUT_COLUMNS.issubset(lookup)


def _has_required_skyline_columns(columns: list[object]) -> bool:
    lookup = {str(c).strip().lower() for c in columns if str(c).strip()}
    for canonical in REQUIRED_SKYLINE_COLUMNS:
        aliases = SKYLINE_COLUMN_ALIASES[canonical]
        if not any(alias.strip().lower() in lookup for alias in aliases):
            return False
    return True


@lru_cache(maxsize=512)
def _workbook_has_skyline_columns(path_text: str) -> bool:
    path = Path(path_text)
    if not path.exists() or path.suffix.lower() not in {".xlsx", ".xls"}:
        return False
    try:
        import pandas as pd
    except Exception:
        return False
    try:
        workbook = pd.ExcelFile(path)
    except Exception:
        return False
    try:
        for sheet_name in workbook.sheet_names:
            try:
                # Reuse the already-open workbook object. Re-opening from path for each
                # sheet is very expensive on large .xlsx files.
                header = pd.read_excel(workbook, sheet_name=sheet_name, nrows=0)
            except Exception:
                continue
            if _has_required_skyline_columns(list(header.columns)):
                return True
    finally:
        try:
            workbook.close()
        except Exception:
            pass
    return False


@lru_cache(maxsize=512)
def _path_has_layout_columns(path_text: str) -> bool:
    path = Path(path_text)
    if not path.exists():
        return False
    try:
        import pandas as pd
    except Exception:
        return False
    suffix = path.suffix.lower()
    try:
        if suffix in {".xlsx", ".xls"}:
            workbook = pd.ExcelFile(path)
            try:
                for sheet_name in workbook.sheet_names:
                    try:
                        header = pd.read_excel(workbook, sheet_name=sheet_name, nrows=0)
                    except Exception:
                        continue
                    if _has_required_layout_columns(list(header.columns)):
                        return True
            finally:
                try:
                    workbook.close()
                except Exception:
                    pass
            return False
        if suffix in {".csv", ".tsv", ".txt"}:
            sep = "\t" if suffix == ".tsv" else None
            header = pd.read_csv(path, nrows=0, sep=sep, engine="python")
            return _has_required_layout_columns(list(header.columns))
    except Exception:
        return False
    return False


class RunRequest(BaseModel):
    mode: str = DEFAULT_MODE
    raw_dir: str
    layout_csv: str = ""
    genomics_excel: str = ""
    output_dir: str = DEFAULT_OUTPUT_DIR
    sheet: str = DEFAULT_SHEET
    skip_fret: int = DEFAULT_SKIP_FRET
    skip_glo: int = DEFAULT_SKIP_GLO
    heatmap_plate: str = DEFAULT_HEATMAP_PLATE
    debug: bool = False
    control_overrides: dict[str, str] | None = None
    step_keys: list[str] | None = None


class ScanRequest(BaseModel):
    root: str


class InputValidationRequest(BaseModel):
    mode: str = DEFAULT_MODE
    raw_dir: str
    layout_csv: str = ""
    genomics_excel: str = ""
    heatmap_plate: str = DEFAULT_HEATMAP_PLATE
    sheet: str = DEFAULT_SHEET


class SignupRequest(BaseModel):
    email: str
    user_id: str
    password: str
    note: str = ""


class LoginRequest(BaseModel):
    user_id: str
    password: str


@dataclass
class RunState:
    id: str
    status: str = "queued"
    logs: list[str] = field(default_factory=list)
    error: str | None = None
    outputs: dict[str, str] = field(default_factory=dict)

    def add(self, line: str) -> None:
        self.logs.append(line.rstrip("\n"))


RUNS: dict[str, RunState] = {}
RUN_LOCK = threading.Lock()
metadata_store = MetadataStore(METADATA_DB_PATH)

app = FastAPI(title="PrPC Screen Web Runner")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=bool(SESSION_COOKIE_DOMAIN),
    domain=SESSION_COOKIE_DOMAIN,
)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.mount("/static", StaticFiles(directory=str(REPO_ROOT / "webapp" / "static")), name="static")
templates = Jinja2Templates(directory=str(REPO_ROOT / "webapp" / "templates"))


def _safe_metadata_call(label: str, callback: Any, *args: Any, **kwargs: Any) -> None:
    try:
        callback(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        print(f"[metadata] {label} failed: {exc}", file=sys.stderr)


def _run_request_payload(req: RunRequest) -> dict[str, Any]:
    if hasattr(req, "model_dump"):
        payload = req.model_dump()
    else:
        payload = req.dict()  # type: ignore[attr-defined]
    return dict(payload)


def _password_hash(password: str) -> str:
    pwd = (password or "").encode("utf-8")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pwd, salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode("ascii") + "$" + base64.b64encode(digest).decode("ascii")


def _password_verify(password: str, stored: str) -> bool:
    if not stored or "$" not in stored:
        return False
    try:
        algo, salt_b64, digest_b64 = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    try:
        salt = base64.b64decode(salt_b64.encode("ascii"), validate=True)
        digest = base64.b64decode(digest_b64.encode("ascii"), validate=True)
    except Exception:
        return False
    candidate = hashlib.scrypt((password or "").encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=len(digest))
    return hmac.compare_digest(candidate, digest)


def _normalize_public_user_id(value: str) -> str:
    clean = (value or "").strip()
    if not clean:
        raise ValueError("User ID is required.")
    if "@" in clean:
        raise ValueError("User ID must not be an email address.")
    if len(clean) < PUBLIC_USER_ID_MIN_LENGTH or len(clean) > PUBLIC_USER_ID_MAX_LENGTH:
        raise ValueError(
            f"User ID must be between {PUBLIC_USER_ID_MIN_LENGTH} and {PUBLIC_USER_ID_MAX_LENGTH} characters."
        )
    if not PUBLIC_USER_ID_RE.fullmatch(clean):
        raise ValueError(
            "User ID can use letters, digits, dot (.), underscore (_), and hyphen (-) only."
        )
    return clean


def _session_user(request: Request) -> dict[str, Any] | None:
    session = _session_user_from_session(request)
    if session is not None:
        return session
    return _edge_authenticated_user(request) or _local_bypass_user(request)


def _session_user_from_session(request: Request | None) -> dict[str, Any] | None:
    if request is None:
        return None
    session = request.session.get("user")
    if not isinstance(session, dict):
        return None
    username = str(session.get("username", "")).strip()
    if not username:
        return None
    user = metadata_store.get_user_by_username(username)
    if not user:
        return None
    user["is_admin"] = bool(user.get("is_admin"))
    return user


def _request_host(request: Request | None) -> str:
    if request is None:
        return ""
    forwarded = request.headers.get("x-forwarded-host", "").strip()
    host = forwarded or request.headers.get("host", "").strip()
    return host.split(":", 1)[0].strip().lower()


def _is_private_ip_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def _is_local_bypass_request(request: Request | None) -> bool:
    host = _request_host(request)
    if not host:
        return False
    if host in LOCAL_BYPASS_HOSTS or host.endswith(".lan"):
        return True
    return _is_private_ip_host(host)


def _local_bypass_user(request: Request | None) -> dict[str, Any] | None:
    if not _is_local_bypass_request(request):
        return None
    return {
        "username": PRIMARY_ADMIN_USERNAME,
        "email": PRIMARY_ADMIN_EMAIL,
        "is_admin": True,
        "status": "approved",
    }


def _edge_authenticated_user(request: Request | None) -> dict[str, Any] | None:
    if request is None:
        return None
    raw = request.headers.get("x-auth-user", "").strip()
    if not raw:
        return None

    # Prefer a real approved app account when the shared-auth identity matches one.
    user = metadata_store.get_user_by_username(raw)
    if not user and "@" in raw:
        user = metadata_store.get_user_by_email(raw)
    if user:
        user["is_admin"] = bool(user.get("is_admin"))
        return user

    username = raw
    email = ""
    if "@" in raw:
        email = raw.lower()
        username = raw.split("@", 1)[0]
    return {
        "username": username[:255] or "shared-auth",
        "email": email,
        "is_admin": False,
        "status": "approved",
    }


def _require_user(request: Request) -> dict[str, Any]:
    user = _session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    status = str(user.get("status") or "approved").strip().lower()
    if status != "approved":
        raise HTTPException(status_code=403, detail=f"Account status is '{status}'.")
    return user


def _require_admin(request: Request) -> dict[str, Any]:
    user = _require_user(request)
    if not bool(user.get("is_admin")):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


def _is_primary_admin_user(user: dict[str, Any]) -> bool:
    username = str(user.get("username") or "").strip().lower()
    email = str(user.get("email") or "").strip().lower()
    return username == PRIMARY_ADMIN_USERNAME.lower() or (PRIMARY_ADMIN_EMAIL and email == PRIMARY_ADMIN_EMAIL)


def _require_primary_admin(request: Request) -> dict[str, Any]:
    user = _require_admin(request)
    if not _is_primary_admin_user(user):
        raise HTTPException(status_code=403, detail="Access restricted to the primary admin account.")
    return user


def _maybe_promote_primary_admin(user: dict[str, Any] | None) -> None:
    if not isinstance(user, dict):
        return
    username = str(user.get("username") or "").strip()
    email = str(user.get("email") or "").strip().lower()
    should_promote = False
    if username and username.lower() == PRIMARY_ADMIN_USERNAME.lower():
        should_promote = True
        _safe_metadata_call(
            "promote_primary_admin_username",
            metadata_store.set_user_admin_by_username,
            username,
            is_admin=True,
            require_approved=True,
        )
    if PRIMARY_ADMIN_EMAIL and email == PRIMARY_ADMIN_EMAIL:
        should_promote = True
        _safe_metadata_call(
            "promote_primary_admin_email",
            metadata_store.set_user_admin_by_email,
            email,
            is_admin=True,
            require_approved=True,
        )
    if should_promote:
        user["is_admin"] = True


def _request_username(request: Request | None) -> str:
    if request is None:
        return "local"
    user = _session_user(request)
    if user:
        return str(user.get("username") or "local")
    for header in ("x-auth-user", "x-forwarded-user", "x-auth-request-user", "x-remote-user"):
        value = request.headers.get(header, "").strip()
        if value:
            return value[:255]
    return "local"


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _public_base_url(request: Request | None = None) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL.rstrip("/")
    if request is not None:
        return str(request.base_url).rstrip("/")
    return "http://127.0.0.1"


def _approval_token(request_id: int, email: str, expires_ts: int) -> str:
    payload = f"{int(request_id)}|{email.strip().lower()}|{int(expires_ts)}"
    return hmac.new(APPROVAL_TOKEN_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _approval_link(request_id: int, email: str, request: Request | None = None) -> str:
    exp = int((_utc_now() + timedelta(seconds=max(300, APPROVAL_TOKEN_TTL_SECONDS))).timestamp())
    sig = _approval_token(request_id, email, exp)
    return f"{_public_base_url(request)}/auth/approve-access?rid={int(request_id)}&email={quote(email)}&exp={exp}&sig={sig}"


def _send_email(subject: str, body: str, to_addrs: list[str], html_body: str | None = None) -> bool:
    host = os.getenv("SMTP_HOST", "").strip()
    try:
        port = int(os.getenv("SMTP_PORT", "587").strip() or "587")
    except ValueError:
        print("[auth-email] invalid SMTP_PORT; using default 587", file=sys.stderr)
        port = 587
    username = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASS", "").strip()
    sender = os.getenv("SMTP_FROM", "noreply@isab.science").strip() or "noreply@isab.science"
    if not host:
        sendmail_bin = "/usr/sbin/sendmail"
        if os.path.exists(sendmail_bin):
            try:
                msg = EmailMessage()
                msg["Subject"] = subject
                msg["From"] = sender
                msg["To"] = ", ".join(to_addrs)
                msg.set_content(body)
                if html_body:
                    msg.add_alternative(html_body, subtype="html")
                proc = subprocess.run(
                    [sendmail_bin, "-t", "-i"],
                    input=msg.as_string(),
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if proc.returncode == 0:
                    print(
                        f"[auth-email] sendmail ok: subject={subject!r} to={','.join(to_addrs)}",
                        file=sys.stderr,
                    )
                    return True
                print(f"[auth-email] sendmail failed rc={proc.returncode}: {proc.stderr.strip()}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"[auth-email] sendmail exception: {exc}", file=sys.stderr)
        print("[auth-email] SMTP_HOST is not configured; email not sent.", file=sys.stderr)
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = ", ".join(to_addrs)
        msg.set_content(body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            if os.getenv("SMTP_STARTTLS", "1").strip().lower() in {"1", "true", "yes", "on"}:
                smtp.starttls()
                smtp.ehlo()
            if username:
                smtp.login(username, password)
            smtp.send_message(msg)
        print(f"[auth-email] smtp ok: subject={subject!r} to={','.join(to_addrs)}", file=sys.stderr)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[auth-email] send failed: {exc}", file=sys.stderr)
        return False


def _email_recipients(*emails: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in emails:
        clean = (raw or "").strip().lower()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _to_swiss_display(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    candidate = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(SWISS_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _apply_swiss_time_fields(records: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in records:
        row = dict(rec)
        for field in fields:
            if field in row:
                row[field] = _to_swiss_display(row.get(field))
        out.append(row)
    return out


def _admin_notification_recipients() -> list[str]:
    return _email_recipients(AUTH_CONTACT_EMAIL, PRIMARY_ADMIN_EMAIL, BOOTSTRAP_ADMIN_EMAIL)


@app.on_event("startup")
def startup_init_metadata() -> None:
    _safe_metadata_call("init_schema", metadata_store.init_schema)
    if BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD and not metadata_store.has_admin_login():
        _safe_metadata_call(
            "ensure_bootstrap_admin",
            metadata_store.ensure_admin_account,
            BOOTSTRAP_ADMIN_EMAIL,
            _password_hash(BOOTSTRAP_ADMIN_PASSWORD),
            approved_by="bootstrap",
        )
    if PRIMARY_ADMIN_USERNAME:
        _safe_metadata_call(
            "promote_primary_admin_at_startup_by_username",
            metadata_store.set_user_admin_by_username,
            PRIMARY_ADMIN_USERNAME,
            is_admin=True,
            require_approved=True,
        )
    if PRIMARY_ADMIN_EMAIL:
        _safe_metadata_call(
            "promote_primary_admin_at_startup_by_email",
            metadata_store.set_user_admin_by_email,
            PRIMARY_ADMIN_EMAIL,
            is_admin=True,
            require_approved=True,
        )


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    try:
        details = metadata_store.healthcheck()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"metadata healthcheck failed: {exc}") from exc
    if not details.get("writable"):
        reason = str(details.get("error") or "metadata DB not writable")
        raise HTTPException(status_code=503, detail=reason)
    return {"ok": True, "metadata": details}


def _scan_root(root_text: str) -> dict[str, Any]:
    root = _resolve_scan_root(root_text)
    if not root.exists():
        raise HTTPException(status_code=400, detail=f"Data root not found: {root}")
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"Data root is not a directory: {root}")

    # Scan root recursively. For parent, scan direct files only to catch common
    # one-level-above placement (e.g. Data/GeneticLocation.xlsx) without traversing
    # every sibling subtree.
    scan_roots: list[Path] = [root]
    parent = root.parent
    include_parent_direct = parent.exists() and parent != root
    if include_parent_direct:
        scan_roots.append(parent)

    files_by_key: dict[str, Path] = {}
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() not in SCAN_FILE_EXTENSIONS:
                continue
            key = os.path.normcase(str(path))
            if key not in files_by_key:
                files_by_key[key] = path
    if include_parent_direct:
        try:
            for path in parent.iterdir():
                if path.is_file():
                    if path.suffix.lower() not in SCAN_FILE_EXTENSIONS:
                        continue
                    key = os.path.normcase(str(path))
                    if key not in files_by_key:
                        files_by_key[key] = path
                    continue
                if not path.is_dir() or path == root:
                    continue
                try:
                    for child in path.iterdir():
                        if not child.is_file() or child.suffix.lower() not in SCAN_FILE_EXTENSIONS:
                            continue
                        key = os.path.normcase(str(child))
                        if key not in files_by_key:
                            files_by_key[key] = child
                except OSError:
                    continue
        except OSError:
            pass

    files = list(files_by_key.values())

    root_dirs: list[Path] = []
    parent_dirs: list[Path] = []
    try:
        root_dirs = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        root_dirs = []
    if include_parent_direct:
        try:
            parent_dirs = [p for p in parent.iterdir() if p.is_dir() and p != root]
        except OSError:
            parent_dirs = []

    raw_candidates = [str(root)]
    raw_candidates.extend(str(p) for p in sorted(root_dirs, key=lambda x: x.as_posix().lower()))
    raw_candidates.extend(str(p) for p in sorted(parent_dirs, key=lambda x: x.as_posix().lower()))
    raw_candidates.extend(
        str(p)
        for p in sorted(files, key=lambda x: x.as_posix().lower())
        if p.suffix.lower() in RAW_FILE_EXTENSIONS
    )
    excel_files = [p for p in files if p.suffix.lower() in EXCEL_FILE_EXTENSIONS]
    known_excel_paths = {str(p.resolve()) for p in excel_files if p.exists()}
    for shared_path in _shared_genomics_candidates():
        if str(shared_path) not in known_excel_paths:
            excel_files.append(shared_path)
            known_excel_paths.add(str(shared_path))
    layout_files = [p for p in files if p.suffix.lower() == ".csv"]
    layout_files.extend(p for p in excel_files if _looks_like_layout_workbook(str(p)))

    def rank_layout_base(path: Path) -> int:
        score = 0
        name = path.name.lower()
        full = str(path).lower()
        if _looks_like_layout_workbook(str(path)):
            score += 2
        if re.search(r"layout|annotation|annot|plate|map", name):
            score += 3
        if "/layout/" in full.replace("\\", "/"):
            score += 2
        if re.search(r"integrated|analyzed|hits|complete|genes", name):
            score -= 6
        if re.search(r"fret|tr-fret|glo|raw|edge", name):
            score -= 3
        return score

    def layout_header_bonus(path: Path) -> int:
        bonus = 0
        if _path_has_layout_columns(str(path)):
            bonus += 20
        return bonus

    layout_scored = [{"path": p, "score": rank_layout_base(p)} for p in layout_files]
    layout_scored.sort(key=lambda rec: (-rec["score"], str(rec["path"]).lower()))
    for rec in layout_scored:
        rec["score"] += layout_header_bonus(rec["path"])
    layout_scored.sort(key=lambda rec: (-rec["score"], str(rec["path"]).lower()))
    layout_candidates = [str(rec["path"]) for rec in layout_scored]

    def rank_genomics_base(path: Path) -> int:
        score = 0
        name = path.name.lower()
        full = str(path).lower().replace("\\", "/")
        if re.search(r"genetic|genomic|chrom|location|skyline", name):
            score += 6
        if re.search(r"layout|plate|map", name):
            score -= 4
        if "/layout/" in full:
            score -= 4
        if _looks_like_layout_workbook(str(path)):
            score -= 6
        return score

    genomics_scored = [{"path": p, "score": rank_genomics_base(p)} for p in excel_files]
    genomics_scored.sort(key=lambda rec: (-rec["score"], str(rec["path"]).lower()))

    probe_pool = [
        rec
        for rec in genomics_scored
        if re.search(r"genetic|genomic|chrom|location|skyline|gene", rec["path"].name.lower())
    ]
    if not probe_pool:
        probe_pool = genomics_scored
    for rec in probe_pool[:MAX_GENOMICS_WORKBOOK_PROBES]:
        if _workbook_has_skyline_columns(str(rec["path"])):
            rec["score"] += 20

    genomics_scored.sort(key=lambda rec: (-rec["score"], str(rec["path"]).lower()))
    genomics_candidates = [str(rec["path"]) for rec in genomics_scored]

    return {
        "root": str(root),
        "scan_roots": [str(p) for p in scan_roots],
        "raw_candidates": raw_candidates[:400],
        "layout_candidates": layout_candidates[:400],
        "genomics_candidates": genomics_candidates[:200],
        "raw_selected": raw_candidates[0] if raw_candidates else "",
        "layout_selected": layout_candidates[0] if layout_candidates else "",
        "genomics_selected": genomics_candidates[0] if genomics_candidates else "",
        "counts": {
            "raw": len(raw_candidates),
            "layout": len(layout_candidates),
            "genomics": len(genomics_candidates),
        },
    }


def _build_steps(req: RunRequest) -> tuple[list[dict[str, Any]], dict[str, str]]:
    output_dir = Path(req.output_dir)
    output_abs = output_dir if output_dir.is_absolute() else (REPO_ROOT / output_dir)
    fig_dir = output_abs / "figures"
    output_abs.mkdir(parents=True, exist_ok=True)
    fig_dir.mkdir(parents=True, exist_ok=True)

    integrated = output_abs / "01_integrated.csv"
    analyzed = output_abs / "02_analyzed.csv"
    hits = output_abs / "03_hits.csv"

    mode = _normalize_mode(req.mode) or DEFAULT_MODE

    if mode == "pooled":
        pooled_cmd = [
            _resolve_python(),
            "prpcscreen/scripts/run_pooled_pipeline.py",
            str(req.raw_dir),
            "--output-dir",
            str(output_abs),
        ]
        if str(req.genomics_excel).strip():
            pooled_cmd.extend(["--genomics-excel", str(req.genomics_excel)])
            if str(req.sheet).strip():
                pooled_cmd.extend(["--skyline-sheet", str(req.sheet).strip()])
        if req.debug:
            pooled_cmd.append("--debug")
        steps = [
            {
                "key": "run_pooled_pipeline",
                "label": "Run pooled pipeline",
                "cmd": pooled_cmd,
                "kind": "pipeline",
            }
        ]
    else:
        raw_dir = Path(req.raw_dir)
        raw_for_integration = raw_dir if raw_dir.is_dir() else raw_dir.parent

        steps = [
            {
                "key": "integrate_raw_data",
                "label": "Integrate raw data",
                "cmd": [
                    _resolve_python(),
                    "prpcscreen/scripts/merge_assay_exports.py",
                    str(raw_for_integration),
                    str(req.layout_csv),
                    str(integrated),
                    "--skip-fret",
                    str(req.skip_fret),
                    "--skip-glo",
                    str(req.skip_glo),
                ],
                "kind": "preprocess",
            },
            {
                "key": "analyze_integrated_data",
                "label": "Analyze integrated data",
                "cmd": [
                    _resolve_python(),
                    "prpcscreen/scripts/compute_screen_metrics.py",
                    str(integrated),
                    str(analyzed),
                    "--hits_csv",
                    str(hits),
                ],
                "kind": "preprocess",
            },
            {
                "key": "plate_quality_controls",
                "label": "Plate quality controls",
                "cmd": [_resolve_python(), "prpcscreen/scripts/plot_plate_health.py", str(analyzed), str(fig_dir / "plate_qc_ssmd_controls.png"), "--interactive-only"],
                "kind": "figure",
            },
            {
                "key": "plate_well_trajectory_plot",
                "label": "Plate well trajectory plot",
                "cmd": [
                    _resolve_python(),
                    "prpcscreen/scripts/plot_well_trajectories.py",
                    str(analyzed),
                    str(fig_dir / "plate_well_series_raw_rep1.png"),
                    "--column",
                    "Raw_rep1",
                    "--interactive-only",
                ],
                "kind": "figure",
            },
            {
                "key": "replicate_agreement_diagnostics",
                "label": "Replicate agreement diagnostics",
                "cmd": [
                    _resolve_python(),
                    "prpcscreen/scripts/plot_replicate_agreement.py",
                    str(analyzed),
                    str(fig_dir / "replicate_agreement_log2fc.png"),
                    "--stem",
                    "Log2FC",
                ],
                "kind": "figure",
            },
            {
                "key": "signal_distribution_histogram",
                "label": "Signal distribution histogram",
                "cmd": [
                    _resolve_python(),
                    "prpcscreen/scripts/plot_signal_distributions.py",
                    str(analyzed),
                    "--output_html",
                    str(fig_dir / "distribution_log2fc_rep1_interactive.html"),
                    "--column",
                    "Log2FC_rep1",
                    "--genomics_excel",
                    str(req.genomics_excel),
                ],
                "kind": "figure",
            },
            {
                "key": "candidate_landscape_plots",
                "label": "Candidate landscape plots",
                "cmd": [
                    _resolve_python(),
                    "prpcscreen/scripts/plot_candidate_landscape.py",
                    str(analyzed),
                    str(fig_dir / "candidate_flashlight_ranked_meanlog2.png"),
                    "--volcano_html",
                    str(fig_dir / "candidate_volcano_interactive.html"),
                    "--genomics_excel",
                    str(req.genomics_excel),
                ],
                "kind": "figure",
            },
            {
                "key": "heatmap_violin_box_plot",
                "label": "Heatmap + violin/box plot",
                "cmd": [
                    _resolve_python(),
                    "prpcscreen/scripts/plot_spatial_and_group_views.py",
                    str(analyzed),
                    str(fig_dir / "plate_heatmap_replicates.png"),
                    str(fig_dir / "grouped_boxplot_raw_rep1.png"),
                    "--plate",
                    str(req.heatmap_plate),
                ],
                "kind": "figure",
            },
            {
                "key": "genomic_signal_skyline_plot",
                "label": "Genomic signal skyline plot",
                "cmd": [
                    _resolve_python(),
                    "prpcscreen/scripts/plot_genomic_signal_skyline.py",
                    str(req.genomics_excel),
                    str(fig_dir / "genomic_skyline_meanlog2fc.png"),
                    "--sheet",
                    req.sheet,
                    "--interactive-only",
                ],
                "kind": "figure",
            },
        ]
        if req.debug:
            for step in steps:
                step["cmd"] = list(step["cmd"]) + ["--debug"]

    outputs = {
        "integrated": str(integrated),
        "analyzed": str(analyzed),
        "hits": str(hits),
        "figures": str(fig_dir),
        "output_dir": str(output_abs),
    }
    return steps, outputs


def _available_steps(mode: str) -> list[dict[str, str]]:
    normalized_mode = _normalize_mode(mode) or DEFAULT_MODE
    sample = RunRequest(
        mode=normalized_mode,
        raw_dir="placeholder",
        layout_csv="placeholder.csv",
        genomics_excel="placeholder.xlsx",
        output_dir=DEFAULT_OUTPUT_DIR,
        sheet=DEFAULT_SHEET,
        skip_fret=DEFAULT_SKIP_FRET,
        skip_glo=DEFAULT_SKIP_GLO,
        heatmap_plate=DEFAULT_HEATMAP_PLATE,
        debug=False,
    )
    steps, _ = _build_steps(sample)
    return [
        {
            "key": str(step["key"]),
            "label": str(step["label"]),
            "kind": str(step.get("kind") or ""),
        }
        for step in steps
    ]


def _apply_control_overrides(layout_csv: str, overrides: dict[str, str], output_dir: Path) -> str:
    """Patch a layout CSV with user-defined control well assignments.

    Returns the path to the patched layout file (written into *output_dir*).
    If the overrides dict is empty the original path is returned unchanged.
    """
    if not overrides:
        return layout_csv

    import pandas as pd  # noqa: F811 – deferred to avoid top-level cost

    path = Path(layout_csv)
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)

    # Build lookup: well_number (int where possible) → role
    well_col = None
    for col in df.columns:
        if str(col).strip().lower() == "well_number_384":
            well_col = col
            break
    if well_col is None:
        return layout_csv  # cannot map wells – fall through to original

    # Convert well IDs (e.g. "A01") to 1-based well numbers for a 384-well plate.
    # A01 → 1, A02 → 2, …, A24 → 24, B01 → 25, etc.
    nt_wells: set[int] = set()
    pos_wells: set[int] = set()
    for well_id, role in overrides.items():
        m = re.fullmatch(r"([A-Pa-p])(\d{1,2})", str(well_id).strip())
        if not m:
            continue
        row = ord(m.group(1).upper()) - 65  # 0-based
        col = int(m.group(2)) - 1           # 0-based
        well_num = row * 24 + col + 1       # 1-based sequential
        role_lower = str(role).strip().lower()
        if role_lower in {"nt", "non-targeting"}:
            nt_wells.add(well_num)
        elif role_lower in {"pos_ctrl", "positive", "pos"}:
            pos_wells.add(well_num)

    if not nt_wells and not pos_wells:
        return layout_csv

    well_nums = pd.to_numeric(df[well_col], errors="coerce")

    # Reset existing control flags
    for col_name in df.columns:
        if str(col_name).strip().lower() == "is_nt_ctrl":
            df[col_name] = False
        elif str(col_name).strip().lower() == "is_pos_ctrl":
            df[col_name] = False

    # Apply new assignments
    for col_name in df.columns:
        if str(col_name).strip().lower() == "is_nt_ctrl":
            df.loc[well_nums.isin(nt_wells), col_name] = True
        elif str(col_name).strip().lower() == "is_pos_ctrl":
            df.loc[well_nums.isin(pos_wells), col_name] = True

    patched_path = output_dir / "layout_patched.csv"
    df.to_csv(patched_path, index=False)
    return str(patched_path)


def _run_pipeline(run_id: str, req: RunRequest) -> None:
    state = RUNS[run_id]
    try:
        state.status = "running"
        _safe_metadata_call("run_status_running", metadata_store.set_run_status, run_id, status="running")
        mode = _normalize_mode(req.mode) or DEFAULT_MODE
        state.add("Pipeline started.")
        if mode == "pooled":
            state.add(
                "Inputs: "
                f"mode={mode} | pooled_table={req.raw_dir} | genomics_excel={req.genomics_excel or '(none)'} | "
                f"sheet={req.sheet} | output_dir={req.output_dir} | debug={req.debug}"
            )
        else:
            state.add(
                "Inputs: "
                f"mode={mode} | raw_dir={req.raw_dir} | layout_csv={req.layout_csv} | genomics_excel={req.genomics_excel} | "
                f"sheet={req.sheet} | output_dir={req.output_dir} | "
                f"heatmap_plate={req.heatmap_plate} | debug={req.debug}"
            )
        # Apply GUI-defined control well overrides to the layout CSV before building pipeline steps.
        if mode == "arrayed" and req.control_overrides:
            output_dir = Path(req.output_dir)
            output_abs = output_dir if output_dir.is_absolute() else (REPO_ROOT / output_dir)
            output_abs.mkdir(parents=True, exist_ok=True)
            original_layout = req.layout_csv
            req.layout_csv = _apply_control_overrides(req.layout_csv, req.control_overrides, output_abs)
            if req.layout_csv != original_layout:
                n_overrides = len(req.control_overrides)
                state.add(f"Applied {n_overrides} control well override(s) from well-selector → {req.layout_csv}")
        steps, outputs = _build_steps(req)
        selected_keys = [str(key).strip() for key in (req.step_keys or []) if str(key).strip()]
        if selected_keys:
            key_set = set(selected_keys)
            steps = [step for step in steps if str(step["key"]) in key_set]
            if not steps:
                raise RuntimeError("No matching pipeline steps were selected.")
            state.add("Selected execution: " + ", ".join(str(step["label"]) for step in steps))
        else:
            state.add("Selected execution: full pipeline")

        total = len(steps)
        for idx, step in enumerate(steps, start=1):
            name = str(step["label"])
            cmd = list(step["cmd"])
            state.add(f"Progress [{idx}/{total}] Starting: {name}")
            state.add("  " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
            proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                state.add("    " + line.rstrip("\n"))
            rc = proc.wait()
            if rc != 0:
                raise RuntimeError(f"Step failed: {name} (exit code {rc})")
            state.add(f"Progress [{idx}/{total}] Done: {name}")
        state.outputs = outputs
        state.status = "completed"
        _safe_metadata_call(
            "run_status_completed",
            metadata_store.set_run_status,
            run_id,
            status="completed",
            finished=True,
            output_dir=outputs.get("output_dir", ""),
        )
        state.add("Pipeline completed.")
    except Exception as exc:  # noqa: BLE001
        state.status = "failed"
        state.error = str(exc)
        _safe_metadata_call(
            "run_status_failed",
            metadata_store.set_run_status,
            run_id,
            status="failed",
            finished=True,
            error_text=str(exc),
        )
        state.add(f"ERROR: {exc}")


@app.get("/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    user = _session_user(request)
    if not user:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "username": user.get("username"),
        "email": user.get("email"),
        "is_admin": bool(user.get("is_admin")),
        "status": user.get("status"),
    }


@app.get("/auth/login", response_class=HTMLResponse)
def auth_login_page(request: Request, force: int = Query(default=0)) -> HTMLResponse:
    if _session_user_from_session(request) and not force:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": "", "notice": ""})


@app.post("/auth/login")
async def auth_login(
    request: Request,
    user_id: str = Form(default=""),
    password: str = Form(default=""),
) -> HTMLResponse:
    clean_user_id = user_id.strip()
    user = metadata_store.get_user_by_username(clean_user_id)
    if not user or not _password_verify(password, str(user.get("password_hash") or "")):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid user ID or password.", "notice": ""},
            status_code=400,
        )
    status = str(user.get("status") or "approved").strip().lower()
    if status != "approved":
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "", "notice": f"Account status is '{status}'. Access is not enabled yet."},
            status_code=403,
        )
    request.session["user"] = {
        "username": str(user.get("username") or ""),
        "email": str(user.get("email") or ""),
        "is_admin": bool(user.get("is_admin")),
    }
    return RedirectResponse(url="/", status_code=303)


@app.get("/auth/signup", response_class=HTMLResponse)
def auth_signup_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="signup.html", context={"error": "", "notice": ""})


@app.post("/auth/signup")
async def auth_signup(
    request: Request,
    email: str = Form(default=""),
    password: str = Form(default=""),
    user_id: str = Form(default=""),
    note: str = Form(default=""),
) -> HTMLResponse:
    clean_email = email.strip().lower()
    try:
        clean_user_id = _normalize_public_user_id(user_id)
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="signup.html",
            context={"error": str(exc), "notice": ""},
            status_code=400,
        )
    if not clean_email or "@" not in clean_email:
        return templates.TemplateResponse(
            request=request,
            name="signup.html",
            context={"error": "Please provide a valid email address.", "notice": ""},
            status_code=400,
        )
    if len(password or "") < PASSWORD_MIN_LENGTH:
        return templates.TemplateResponse(
            request=request,
            name="signup.html",
            context={"error": f"Password must be at least {PASSWORD_MIN_LENGTH} characters.", "notice": ""},
            status_code=400,
        )
    try:
        created = metadata_store.create_access_request(
            email=clean_email,
            password_hash=_password_hash(password),
            note=note,
            requested_by_username=clean_user_id,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="signup.html",
            context={"error": str(exc), "notice": ""},
            status_code=400,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[auth-signup] create_access_request failed: {exc}", file=sys.stderr)
        return templates.TemplateResponse(
            request=request,
            name="signup.html",
            context={
                "error": "Unable to submit access request right now. Please try again in a moment or contact support.",
                "notice": "",
            },
            status_code=503,
        )

    subject = f"PrPCScreen access request: {clean_email}"
    approval_url = _approval_link(int(created.get("request_id") or 0), clean_email, request)
    body = (
        f"A new access request was submitted.\n\n"
        f"Email: {clean_email}\n"
        f"User ID: {clean_user_id}\n"
        f"Request id: {created.get('request_id')}\n"
        f"Note: {(note or '').strip() or '(none)'}\n\n"
        f"Approve now: {approval_url}\n"
        f"Admin page: {_public_base_url(request)}/admin/dashboard\n"
    )
    html_body = (
        "<html><body style=\"font-family:Segoe UI,Tahoma,sans-serif;color:#0f172a;\">"
        "<h2 style=\"margin-bottom:8px;\">A new access request was submitted</h2>"
        f"<p><b>Email:</b> {clean_email}<br>"
        f"<b>User ID:</b> {clean_user_id}<br>"
        f"<b>Request id:</b> {created.get('request_id')}<br>"
        f"<b>Note:</b> {(note or '').strip() or '(none)'}</p>"
        f"<p><a href=\"{approval_url}\" "
        "style=\"display:inline-block;background:#166534;color:#ffffff;text-decoration:none;padding:10px 14px;border-radius:8px;font-weight:700;\">Approve Access</a></p>"
        f"<p style=\"font-size:12px;color:#475569;\">If the button does not work, open this URL:<br>{approval_url}</p>"
        f"<p style=\"font-size:12px;\"><a href=\"{_public_base_url(request)}/admin/dashboard\">Open admin dashboard</a></p>"
        "</body></html>"
    )
    _send_email(subject, body, _admin_notification_recipients(), html_body=html_body)
    return templates.TemplateResponse(
        request=request,
        name="signup.html",
        context={
            "error": "",
            "notice": "Access request submitted. After approval you can sign in at /auth/login.",
        },
    )


@app.get("/auth/approve-access", response_class=HTMLResponse)
def auth_approve_access(
    request: Request,
    rid: int,
    email: str,
    exp: int,
    sig: str,
) -> HTMLResponse:
    now_ts = int(_utc_now().timestamp())
    if exp < now_ts:
        return HTMLResponse("<h2>Approval link expired.</h2>", status_code=400)
    expected = _approval_token(rid, email, exp)
    if not hmac.compare_digest(sig, expected):
        return HTMLResponse("<h2>Invalid approval signature.</h2>", status_code=400)
    try:
        approved_user = metadata_store.decide_access_request(rid, approve=True, decided_by="email-link")
        _maybe_promote_primary_admin(approved_user)
    except ValueError as exc:
        req = metadata_store.get_access_request(rid)
        msg = str(exc)
        if req and str(req.get("status") or "").strip().lower() == "approved":
            approved_email = str(req.get("email") or email).strip()
            return HTMLResponse(
                "<h2>Access already approved.</h2>"
                f"<p>{approved_email} is already approved and can sign in.</p>"
                f"<p><a href=\"{_public_base_url(request)}/auth/login\">Go to sign in</a></p>",
                status_code=200,
            )
        return HTMLResponse(
            "<h2>Approval failed</h2>"
            f"<p>{msg}</p>"
            f"<p><a href=\"{_public_base_url(request)}/auth/login\">Go to sign in</a></p>",
            status_code=400,
        )
    approved_email = str(approved_user.get("email") or "").strip()
    recipients = _email_recipients(approved_email, *_admin_notification_recipients())
    if recipients:
        _send_email(
            "PrPCScreen access approved",
            "Your account has been approved. You can now log in at /auth/login.",
            recipients,
        )
    return HTMLResponse(
        "<h2>Access approved.</h2>"
        f"<p>{approved_email or email} can now sign in.</p>"
        f"<p><a href=\"{_public_base_url(request)}/auth/login\">Go to sign in</a></p>"
    )


@app.post("/auth/logout")
def auth_logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/auth/login", status_code=303)


@app.get("/admin/access-requests", response_class=HTMLResponse)
def admin_access_requests(request: Request) -> HTMLResponse:
    user = _session_user(request)
    if not user or str(user.get("status") or "").strip().lower() != "approved":
        return RedirectResponse(url="/auth/login", status_code=303)
    _require_primary_admin(request)
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/access-requests/{request_id}/approve")
def admin_approve_access_request(request_id: int, request: Request) -> RedirectResponse:
    user = _require_primary_admin(request)
    try:
        approved_user = metadata_store.decide_access_request(request_id, approve=True, decided_by=str(user.get("username") or "admin"))
        _maybe_promote_primary_admin(approved_user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    approved_email = str(approved_user.get("email") or "").strip()
    recipients = _email_recipients(approved_email, *_admin_notification_recipients())
    if recipients:
        _send_email(
            "PrPCScreen access approved",
            "Your account has been approved. You can now log in at /auth/login.",
            recipients,
        )
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/access-requests/{request_id}/reject")
def admin_reject_access_request(request_id: int, request: Request) -> RedirectResponse:
    user = _require_primary_admin(request)
    req = metadata_store.get_access_request(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Access request not found.")
    req_user = str(req.get("username") or req.get("requested_by_username") or "").strip().lower()
    req_email = str(req.get("email") or "").strip().lower()
    if req_user == PRIMARY_ADMIN_USERNAME.lower() or (PRIMARY_ADMIN_EMAIL and req_email == PRIMARY_ADMIN_EMAIL):
        raise HTTPException(status_code=403, detail="Primary admin account cannot be rejected.")
    try:
        rejected_user = metadata_store.decide_access_request(request_id, approve=False, decided_by=str(user.get("username") or "admin"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rejected_email = str(rejected_user.get("email") or "").strip()
    recipients = _email_recipients(rejected_email, *_admin_notification_recipients())
    if recipients:
        _send_email(
            "PrPCScreen access request update",
            "Your access request was not approved.",
            recipients,
        )
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/users/block")
def admin_block_user(request: Request, username: str = Form(default="")) -> RedirectResponse:
    actor = _require_primary_admin(request)
    target = username.strip()
    if not target:
        raise HTTPException(status_code=400, detail="Missing target user.")
    if target.lower() == str(actor.get("username") or "").strip().lower():
        raise HTTPException(status_code=400, detail="You cannot block your own account.")
    if target.lower() == PRIMARY_ADMIN_USERNAME.lower():
        raise HTTPException(status_code=403, detail="Primary admin account cannot be blocked.")
    updated = metadata_store.set_user_status_by_username(target, status="rejected", clear_admin=True)
    if not updated:
        raise HTTPException(status_code=404, detail="Target user not found.")
    blocked_user = metadata_store.get_user_by_username(target) or {}
    blocked_email = str(blocked_user.get("email") or "").strip()
    if blocked_email:
        metadata_store.decide_pending_access_requests_by_email(
            blocked_email,
            approve=False,
            decided_by=str(actor.get("username") or "admin"),
        )
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/users/approve")
def admin_approve_user(request: Request, username: str = Form(default="")) -> RedirectResponse:
    actor = _require_primary_admin(request)
    target = username.strip()
    if not target:
        raise HTTPException(status_code=400, detail="Missing target user.")
    updated = metadata_store.approve_user_by_username(target, approved_by=str(actor.get("username") or "admin"))
    if not updated:
        raise HTTPException(status_code=404, detail="Target user not found.")
    approved_user = metadata_store.get_user_by_username(target) or {}
    approved_email = str(approved_user.get("email") or "").strip()
    recipients = _email_recipients(approved_email, *_admin_notification_recipients())
    if recipients:
        _send_email(
            "PrPCScreen access approved",
            "Your account has been approved. You can now log in at /auth/login.",
            recipients,
        )
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/users/restore")
def admin_restore_user(request: Request, username: str = Form(default="")) -> RedirectResponse:
    actor = _require_primary_admin(request)
    target = username.strip()
    if not target:
        raise HTTPException(status_code=400, detail="Missing target user.")
    updated = metadata_store.approve_user_by_username(target, approved_by=str(actor.get("username") or "admin"))
    if not updated:
        raise HTTPException(status_code=404, detail="Target user not found.")
    restored_user = metadata_store.get_user_by_username(target) or {}
    restored_email = str(restored_user.get("email") or "").strip()
    recipients = _email_recipients(restored_email, *_admin_notification_recipients())
    if recipients:
        _send_email(
            "PrPCScreen access restored",
            "Your account access has been restored. You can now log in at /auth/login.",
            recipients,
        )
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/users/make-admin")
def admin_make_user_admin(request: Request, username: str = Form(default="")) -> RedirectResponse:
    _require_primary_admin(request)
    target = username.strip()
    if not target:
        raise HTTPException(status_code=400, detail="Missing target user.")
    updated = metadata_store.set_user_admin_by_username(target, is_admin=True, require_approved=True)
    if not updated:
        raise HTTPException(status_code=400, detail="Target user must exist and be approved before admin elevation.")
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(request: Request) -> HTMLResponse:
    user = _session_user(request)
    if not user or str(user.get("status") or "").strip().lower() != "approved":
        return RedirectResponse(url="/auth/login", status_code=303)
    user = _require_primary_admin(request)

    users = metadata_store.list_users()
    active_users = [u for u in users if str(u.get("status") or "").strip().lower() == "approved"]
    pending_users = [u for u in users if str(u.get("status") or "").strip().lower() == "pending"]
    denied_users = [u for u in users if str(u.get("status") or "").strip().lower() in {"disabled", "rejected"}]

    requests = metadata_store.list_access_requests(limit=5000)
    req_counts = {"pending": 0, "approved": 0, "rejected": 0, "other": 0}
    for req in requests:
        st = str(req.get("status") or "").strip().lower()
        if st in req_counts:
            req_counts[st] += 1
        else:
            req_counts["other"] += 1

    runs = metadata_store.list_runs(limit=500)
    run_counts: dict[str, int] = {}
    for rec in runs:
        st = str(rec.get("status") or "").strip().lower() or "unknown"
        run_counts[st] = run_counts.get(st, 0) + 1

    summary = metadata_store.summary()
    analytics = {
        "total_users": int(summary.get("users", 0)),
        "active_users": len(active_users),
        "pending_users": len(pending_users),
        "denied_users": len(denied_users),
        "datasets": int(summary.get("datasets", 0)),
        "runs_total": int(summary.get("runs_total", 0)),
        "runs_active": int(summary.get("runs_active", 0)),
        "access_requests_total": len(requests),
        "access_requests_pending": req_counts["pending"],
        "access_requests_approved": req_counts["approved"],
        "access_requests_rejected": req_counts["rejected"],
        "tracked_run_statuses": run_counts,
    }

    active_users_view = _apply_swiss_time_fields(active_users, ("approved_at", "created_at"))
    pending_users_view = _apply_swiss_time_fields(pending_users, ("created_at",))
    denied_users_view = _apply_swiss_time_fields(denied_users, ("created_at",))
    access_requests_view = _apply_swiss_time_fields(requests, ("requested_at", "decided_at"))

    return templates.TemplateResponse(
        request=request,
        name="admin_dashboard.html",
        context={
            "user": user,
            "active_users": active_users_view,
            "pending_users": pending_users_view,
            "denied_users": denied_users_view,
            "all_access_requests": access_requests_view,
            "analytics": analytics,
        },
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    user = _session_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)
    if str(user.get("status") or "approved").strip().lower() != "approved":
        return RedirectResponse(url="/auth/login", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "user": user,
            "has_local_session": _session_user_from_session(request) is not None,
            "defaults": {
                "data_root": _default_data_root(),
                "mode": DEFAULT_MODE,
                "output_dir": DEFAULT_OUTPUT_DIR,
                "sheet": DEFAULT_SHEET,
                "skip_fret": DEFAULT_SKIP_FRET,
                "skip_glo": DEFAULT_SKIP_GLO,
                "heatmap_plate": DEFAULT_HEATMAP_PLATE,
            }
        },
    )


class LayoutControlsRequest(BaseModel):
    path: str


@app.post("/api/layout-controls")
def api_layout_controls(body: LayoutControlsRequest, request: Request) -> dict[str, Any]:
    """Read Is_NT_ctrl / Is_pos_ctrl from a layout file and return well-ID assignments."""
    _require_user(request)
    import pandas as pd

    p = Path(body.path)
    if not p.exists():
        raise HTTPException(status_code=400, detail=f"File not found: {body.path}")
    suffix = p.suffix.lower()
    try:
        if suffix in {".xlsx", ".xls", ".xlsm"}:
            df = pd.read_excel(p)
        else:
            df = pd.read_csv(p)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot read file: {exc}")

    cols_lower = {str(c).strip().lower(): c for c in df.columns}
    well_col = cols_lower.get("well_number_384")
    nt_col = cols_lower.get("is_nt_ctrl")
    pos_col = cols_lower.get("is_pos_ctrl")
    if well_col is None:
        raise HTTPException(status_code=400, detail="Layout file missing Well_number_384 column.")

    well_nums = pd.to_numeric(df[well_col], errors="coerce")
    assignments: dict[str, str] = {}

    def _well_num_to_id(n: int) -> str:
        n0 = int(n) - 1
        row = n0 // 24
        col = n0 % 24
        return chr(65 + row) + str(col + 1).zfill(2)

    if nt_col is not None:
        nt_mask = df[nt_col].astype(str).str.strip().str.lower().isin({"true", "1", "1.0"})
        for wn in well_nums[nt_mask].dropna().unique():
            assignments[_well_num_to_id(wn)] = "NT"

    if pos_col is not None:
        pos_mask = df[pos_col].astype(str).str.strip().str.lower().isin({"true", "1", "1.0"})
        for wn in well_nums[pos_mask].dropna().unique():
            assignments[_well_num_to_id(wn)] = "pos_ctrl"

    return {"assignments": assignments}


@app.post("/api/upload-input")
async def api_upload_input(
    request: Request,
    target: str = Form(...),
    mode: str = Form(DEFAULT_MODE),
    sheet: str = Form(DEFAULT_SHEET),
    files: list[UploadFile] = File(...),
    relative_paths: list[str] = Form(default=[]),
) -> dict[str, Any]:
    user = _require_user(request)
    target_name = str(target or "").strip().lower()
    normalized_mode = _normalize_mode(mode)
    if not normalized_mode:
        raise HTTPException(status_code=400, detail=f"Invalid mode '{mode}'.")
    if target_name not in {"raw_dir", "layout_csv", "genomics_excel"}:
        raise HTTPException(status_code=400, detail=f"Unsupported upload target: {target}")
    if not files:
        raise HTTPException(status_code=400, detail="No files were uploaded.")

    actor = str(user.get("username") or "user").strip() or "user"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    upload_base = _uploads_root() / f"{stamp}_{actor}_{uuid.uuid4().hex[:8]}"

    if target_name == "raw_dir" and normalized_mode == "arrayed":
        saved_root = upload_base / "raw_dir"
        rels = relative_paths if len(relative_paths) == len(files) else []
        for idx, upload in enumerate(files):
            rel = rels[idx] if idx < len(rels) else upload.filename
            destination = saved_root / _safe_upload_relative_path(rel, upload.filename or f"file_{idx + 1}")
            _save_upload_stream(upload, destination)
        validation = _validate_arrayed_raw_input(saved_root)
        return {
            "ok": True,
            "target": target_name,
            "path": str(saved_root),
            "saved_files": int(len(files)),
            "validation": validation,
        }

    if len(files) != 1:
        raise HTTPException(status_code=400, detail="This field accepts exactly one file.")

    upload = files[0]
    field_dir = upload_base / target_name
    destination = field_dir / _sanitize_upload_filename(upload.filename or "upload.bin")
    _save_upload_stream(upload, destination)

    if target_name == "raw_dir":
        validation = (
            _validate_arrayed_raw_input(destination)
            if normalized_mode == "arrayed"
            else _validate_pooled_raw_input(destination)
        )
    elif target_name == "layout_csv":
        validation = _validate_layout_input(destination)
    else:
        validation = _validate_genomics_input(destination, sheet=sheet)

    return {
        "ok": True,
        "target": target_name,
        "path": str(destination),
        "saved_files": 1,
        "validation": validation,
    }


@app.post("/api/validate-inputs")
def api_validate_inputs(body: InputValidationRequest, request: Request) -> dict[str, Any]:
    _require_user(request)
    checks = _validate_mode_inputs(
        mode=body.mode,
        raw_dir=body.raw_dir,
        layout_csv=body.layout_csv,
        genomics_excel=body.genomics_excel,
        heatmap_plate=body.heatmap_plate,
        sheet=body.sheet,
    )
    return {"ok": True, "checks": checks}


@app.post("/api/scan")
def api_scan(body: ScanRequest, request: Request) -> dict[str, Any]:
    _require_user(request)
    return _scan_root(body.root)


@app.get("/api/steps")
def api_steps(request: Request, mode: str = Query(default=DEFAULT_MODE)) -> dict[str, Any]:
    _require_user(request)
    normalized_mode = _normalize_mode(mode) or DEFAULT_MODE
    return {"mode": normalized_mode, "steps": _available_steps(normalized_mode)}


@app.post("/api/run")
def api_run(body: RunRequest, request: Request) -> dict[str, str]:
    actor_user = _require_user(request)
    checks = _validate_mode_inputs(
        mode=body.mode,
        raw_dir=body.raw_dir,
        layout_csv=body.layout_csv,
        genomics_excel=body.genomics_excel,
        heatmap_plate=body.heatmap_plate,
        sheet=body.sheet,
    )
    mode = str(checks.get("mode") or DEFAULT_MODE)
    body.mode = mode
    raw_path = Path(body.raw_dir)
    if not body.output_dir:
        raise HTTPException(status_code=400, detail="Missing required field: output_dir")

    genomics_path = Path(body.genomics_excel) if str(body.genomics_excel).strip() else None

    run_id = uuid.uuid4().hex[:12]
    state = RunState(id=run_id)
    actor = str(actor_user.get("username") or _request_username(request))
    _safe_metadata_call("upsert_user_run_actor", metadata_store.upsert_user, actor)
    _safe_metadata_call(
        "create_run",
        metadata_store.create_run,
        run_id=run_id,
        mode=mode,
        status="queued",
        params=_run_request_payload(body),
        started_by=actor,
        output_dir=body.output_dir,
    )
    with RUN_LOCK:
        RUNS[run_id] = state
    thread = threading.Thread(target=_run_pipeline, args=(run_id, body), daemon=True)
    thread.start()
    return {"run_id": run_id}


def _api_status_core(run_id: str, from_index: int = 0) -> dict[str, Any]:
    # Auth required even though results are public inside authenticated area.
    # This keeps endpoints inaccessible without session login.
    # request is omitted from signature intentionally to preserve API shape.
    #
    # NOTE: FastAPI route wrappers below call this function directly with request guard.
    state = RUNS.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")
    logs = state.logs[from_index:]
    return {
        "run_id": state.id,
        "status": state.status,
        "error": state.error,
        "logs": logs,
        "next_index": from_index + len(logs),
        "outputs": state.outputs,
    }


@app.get("/api/status/{run_id}")
def api_status_guarded(run_id: str, request: Request, from_index: int = Query(default=0, ge=0)) -> dict[str, Any]:
    _require_user(request)
    return _api_status_core(run_id, from_index=from_index)


@app.get("/api/meta/summary")
def api_meta_summary(request: Request) -> dict[str, int]:
    _require_admin(request)
    return metadata_store.summary()


@app.get("/api/meta/users")
def api_meta_users(request: Request) -> dict[str, Any]:
    _require_admin(request)
    return {"users": metadata_store.list_users()}


@app.get("/api/meta/runs")
def api_meta_runs(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    _require_admin(request)
    return {"runs": metadata_store.list_runs(limit=limit)}


@app.get("/api/meta/runs/{run_id}")
def api_meta_run(run_id: str, request: Request) -> dict[str, Any]:
    _require_admin(request)
    run = metadata_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return run


@app.get("/api/figures")
def api_figures(request: Request, output_dir: str = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    _require_user(request)
    out = _safe_path(output_dir)
    fig_dir = out / "figures"
    if not fig_dir.exists():
        return {"figures": []}

    def _figure_sort_key(path: Path) -> tuple[int, str]:
        name = path.name.lower()
        if name == "plate_heatmap_replicates_collection.svg":
            return (2, name)
        is_interactive = path.suffix.lower() in {".html", ".htm"} and "interactive" in name
        return (0 if is_interactive else 1, name)

    hidden_legacy_names = {
        "candidate_flashlight_ranked_meanlog2.png",
        "genomic_skyline_meanlog2fc.png",
        "plate_heatmap_raw_rep1.png",
        "plate_heatmap_raw_rep1_interactive.html",
        "plate_heatmap_replicates_collection_low.svg",
        "plate_heatmap_replicates_collection_medium.svg",
        "plate_heatmap_replicates_collection_high.svg",
        "plate_qc_ssmd_controls.png",
    }
    items = sorted(
        [
            p
            for p in fig_dir.iterdir()
            if p.is_file()
            and p.suffix.lower() in {".png", ".svg", ".html", ".htm"}
            and p.name.lower() not in hidden_legacy_names
        ],
        key=_figure_sort_key,
    )
    return {
        "figures": [
            {
                "name": p.name,
                "path": str(p),
                "url": f"/api/file?path={quote(str(p))}",
                "kind": "html" if p.suffix.lower() in {".html", ".htm"} else "image",
            }
            for p in items
        ]
    }


@app.get("/api/file")
def api_file(path: str, request: Request) -> FileResponse:
    _require_user(request)
    p = _safe_path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    return FileResponse(str(p))


@app.post("/api/auth/signup")
def api_auth_signup(body: SignupRequest) -> dict[str, Any]:
    try:
        clean_user_id = _normalize_public_user_id(body.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if len(body.password or "") < PASSWORD_MIN_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    try:
        created = metadata_store.create_access_request(
            email=body.email,
            password_hash=_password_hash(body.password),
            note=body.note,
            requested_by_username=clean_user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        print(f"[api-auth-signup] create_access_request failed: {exc}", file=sys.stderr)
        raise HTTPException(status_code=503, detail="Unable to submit access request right now.") from exc
    created_email = str(created.get("email") or "").strip().lower()
    approval_url = _approval_link(int(created.get("request_id") or 0), created_email, None)
    text_body = (
        f"Request id: {created.get('request_id')}\n"
        f"Email: {created_email}\n"
        f"User ID: {clean_user_id}\n"
        f"Note: {(body.note or '').strip() or '(none)'}\n\n"
        f"Approve now: {approval_url}\n"
        f"Admin page: {_public_base_url(None)}/admin/dashboard"
    )
    html_body = (
        "<html><body style=\"font-family:Segoe UI,Tahoma,sans-serif;color:#0f172a;\">"
        "<h2 style=\"margin-bottom:8px;\">A new access request was submitted</h2>"
        f"<p><b>Email:</b> {created_email}<br>"
        f"<b>User ID:</b> {clean_user_id}<br>"
        f"<b>Request id:</b> {created.get('request_id')}<br>"
        f"<b>Note:</b> {(body.note or '').strip() or '(none)'}</p>"
        f"<p><a href=\"{approval_url}\" "
        "style=\"display:inline-block;background:#166534;color:#ffffff;text-decoration:none;padding:10px 14px;border-radius:8px;font-weight:700;\">Approve Access</a></p>"
        f"<p style=\"font-size:12px;color:#475569;\">If the button does not work, open this URL:<br>{approval_url}</p>"
        f"<p style=\"font-size:12px;\"><a href=\"{_public_base_url(None)}/admin/dashboard\">Open admin dashboard</a></p>"
        "</body></html>"
    )
    _send_email(f"PrPCScreen access request: {created_email}", text_body, _admin_notification_recipients(), html_body=html_body)
    return {"ok": True, "request_id": created.get("request_id")}


@app.post("/api/auth/login")
def api_auth_login(body: LoginRequest, request: Request) -> dict[str, Any]:
    user = metadata_store.get_user_by_username(body.user_id)
    if not user or not _password_verify(body.password, str(user.get("password_hash") or "")):
        raise HTTPException(status_code=401, detail="Invalid user ID or password.")
    status = str(user.get("status") or "approved").strip().lower()
    if status != "approved":
        raise HTTPException(status_code=403, detail=f"Account status is '{status}'.")
    request.session["user"] = {
        "username": str(user.get("username") or ""),
        "email": str(user.get("email") or ""),
        "is_admin": bool(user.get("is_admin")),
    }
    return {"ok": True, "username": str(user.get("username") or ""), "is_admin": bool(user.get("is_admin"))}


@app.post("/api/auth/logout")
def api_auth_logout(request: Request) -> dict[str, Any]:
    request.session.clear()
    return {"ok": True}
