from __future__ import annotations

import argparse
import os
import sys
import re
from pathlib import Path

import numpy as np
import pandas as pd

DEBUG_ENV_DEFAULT = os.environ.get("PRPCSCREEN_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}


def debug_log(message: str, enabled: bool) -> None:
    """Emit script-level debug lines with a stable prefix."""
    if enabled:
        print(f"[02_merge_assay_exports] {message}", file=sys.stderr)


def derive_plate_labels(paths: list[Path]) -> list[str]:
    # Infer plate IDs from known acquisition filename patterns.
    out = []
    for p in paths:
        name = p.name
        if "5000_" in name and "_PRP_" in name:
            out.append(name.split("5000_", 1)[1].split("_PRP_", 1)[0])
        else:
            out.append(p.stem)
    return out


def normalize_plate_label(value: object) -> str:
    return str(value).strip().upper()


def infer_plate_label(path: Path, known_labels: set[str] | None = None) -> str | None:
    """
    Infer a plate identifier (for example "LXXIV") from common export names.
    """
    stem_upper = path.stem.strip().upper()

    # Common naming pattern: Plate_LXXIV_A.csv
    m = re.search(r"PLATE[_\-\s]*([A-Z0-9]+)(?:[_\-\s]*[AB])?$", stem_upper)
    if m:
        token = normalize_plate_label(m.group(1))
        if (known_labels is None) or (token in known_labels):
            return token

    # Legacy pattern: ...5000_<plate>_PRP_...
    if "5000_" in stem_upper and "_PRP_" in stem_upper:
        token = normalize_plate_label(stem_upper.split("5000_", 1)[1].split("_PRP_", 1)[0])
        if (known_labels is None) or (token in known_labels):
            return token

    # Last-resort token match against known layout labels.
    if known_labels:
        tokens = [t for t in re.split(r"[^A-Z0-9]+", stem_upper) if t]
        for tok in tokens:
            token = normalize_plate_label(tok)
            if token in known_labels:
                return token

    return None


def load_measurement_table(path: Path, skip_lines: int) -> pd.DataFrame:
    # Header rows vary by instrument/export profile and are caller-configurable.
    return pd.read_csv(path, skiprows=skip_lines, sep=None, engine="python")


def load_layout_table(path: Path) -> pd.DataFrame:
    # Older deployments commonly stored the plate map as an Excel workbook.
    if path.suffix.lower() in {".xlsx", ".xls", ".xlsm"}:
        return pd.read_excel(path)
    return pd.read_csv(path)


def discover_candidate_measurement_files(raw_root: Path) -> list[Path]:
    # Include common export extensions and preserve deterministic ordering.
    out: list[Path] = []
    for ext in ("*.csv", "*.tsv", "*.txt"):
        out.extend(raw_root.rglob(ext))
    return sorted(set(out))


def looks_like_plate_measurement_file(path: Path, skip_lines: int) -> bool:
    """
    Heuristic detection for plate exports when filenames are non-standard.
    """
    try:
        table = load_measurement_table(path, skip_lines)
        vals = flatten_plate_measurements(table)
        return len(vals) >= 384
    except Exception:
        return False


def _fret_correct_from_two_channels(ch1: np.ndarray, ch2: np.ndarray) -> np.ndarray:
    """
    Apply the same TR-FRET correction used in Athena's pipeline.
    """
    odd = np.array([0, 2, 4, 6, 8, 10, 12, 14], dtype=int)   # A, C, E, G, I, K, M, O
    even = np.array([1, 3, 5, 7, 9, 11, 13, 15], dtype=int)  # B, D, F, H, J, L, N, P

    denom = np.nanmean(ch2[even, 0]) - np.nanmean(ch2[even, 23])
    if np.isclose(denom, 0) or np.isnan(denom):
        proportionality = 0.0
    else:
        proportionality = (np.nanmean(ch1[even, 0]) - np.nanmean(ch1[even, 23])) / denom

    ch1_f = ch1 - np.nanmean(ch1[odd, 0])
    ch2_f = ch2 - np.nanmean(ch2[even, 23])
    fret = ch1_f - proportionality * ch2_f
    fret = fret - np.nanmean(fret[odd, 23])
    return fret


def flatten_plate_measurements(table: pd.DataFrame, apply_trfret_correction: bool = False) -> pd.Series:
    """
    Convert a single plate export table into a 384-length numeric vector.

    Preferred format:
    - first column holds row labels (A..P),
    - columns 01..24 hold measurements.
    """
    if table.empty:
        return pd.Series(dtype=float)

    row_label_col = table.columns[0]
    row_labels = table[row_label_col].astype(str).str.strip().str.upper()
    is_plate_row = row_labels.str.fullmatch(r"[A-P]")

    # Support both "01..24" and "1..24" export header styles.
    col_by_idx: dict[int, str] = {}
    for col in table.columns:
        text = str(col).strip()
        if not re.fullmatch(r"\d{1,2}", text):
            continue
        idx = int(text)
        if 1 <= idx <= 24 and idx not in col_by_idx:
            col_by_idx[idx] = col

    if len(col_by_idx) == 24:
        ordered_cols = [col_by_idx[i] for i in range(1, 25)]
        plate = table.loc[is_plate_row, ordered_cols].apply(pd.to_numeric, errors="coerce")
        if plate.shape[0] >= 16:
            # For TR-FRET exports that contain channel-1 + channel-2 blocks, match Athena's
            # correction before flattening. Fall back to the first block if correction is unavailable.
            if apply_trfret_correction and plate.shape[0] >= 32:
                ch1 = plate.iloc[:16, :24].to_numpy(dtype=float)
                ch2 = plate.iloc[16:32, :24].to_numpy(dtype=float)
                if np.isfinite(ch1).all() and np.isfinite(ch2).all():
                    fret = _fret_correct_from_two_channels(ch1, ch2)
                    return pd.Series(fret.reshape(-1), dtype=float)

            # Default behavior: keep first 16 A..P rows from selected result section.
            arr = plate.iloc[:16, :24].to_numpy(dtype=float)
            return pd.Series(arr.reshape(-1), dtype=float)

    # Fallback for unexpected exports: keep numeric cells only.
    numeric = table.apply(pd.to_numeric, errors="coerce")
    vals = numeric.to_numpy().reshape(-1)
    vals = vals[~np.isnan(vals)]
    return pd.Series(vals, dtype=float)


def classify_fret_replicate(path: Path) -> int | None:
    """
    Infer replicate membership from file naming.

    Returns:
    - 1 for A-like files
    - 2 for B-like files
    - None if undecidable
    """
    stem = path.stem
    left = stem.split("_Lilly", 1)[0].strip()
    m = re.search(r"([A-Za-z])(?:\s+rep(?:eat)?)?$", left, flags=re.IGNORECASE)
    if not m:
        return None
    letter = m.group(1).upper()
    if letter == "A":
        return 1
    if letter == "B":
        return 2
    return None


def drop_stale_analysis_columns(layout: pd.DataFrame) -> pd.DataFrame:
    # Keep layout metadata while removing stale computed columns from previous runs.
    drop_prefixes = (
        "Raw_",
        "DeltaNT_",
        "FoldNT_",
        "PercActivation_",
        "Log2FC_",
        "SSMD_",
        "p_value_",
        "Mean_",
        "Hit_strength_",
        "CellTiterGlo_",
    )
    drop_exact = {"Raw_rep1", "Raw_rep2"}
    keep_cols = [
        c
        for c in layout.columns
        if (c not in drop_exact) and (not any(str(c).startswith(prefix) for prefix in drop_prefixes))
    ]
    return layout[keep_cols].copy()


def _layout_plate_order_and_counts(layout: pd.DataFrame) -> tuple[list[str], dict[str, int]]:
    raw = layout["Plate_number_384"].astype(str).str.strip()
    order: list[str] = []
    counts: dict[str, int] = {}
    for value in raw:
        if value == "" or value.lower() in {"nan", "none"}:
            continue
        plate = normalize_plate_label(value)
        if plate not in counts:
            counts[plate] = 0
            order.append(plate)
        counts[plate] += 1
    return order, counts


def _choose_better_chunk(current: pd.Series, candidate: pd.Series, expected_len: int) -> pd.Series:
    # Prefer chunks whose length most closely matches expected plate rows.
    current_dist = abs(len(current) - expected_len)
    candidate_dist = abs(len(candidate) - expected_len)
    if candidate_dist < current_dist:
        return candidate
    return current


def _assemble_replicate_vector(
    plate_order: list[str],
    plate_counts: dict[str, int],
    plate_chunks: dict[str, pd.Series],
    fallback_chunks: list[pd.Series],
) -> tuple[pd.Series, int]:
    pieces: list[pd.Series] = []
    fallback_index = 0

    for plate in plate_order:
        expected = plate_counts.get(plate, 0)
        chunk = plate_chunks.get(plate)
        if chunk is None and fallback_index < len(fallback_chunks):
            chunk = fallback_chunks[fallback_index]
            fallback_index += 1
        values = pd.to_numeric(chunk, errors="coerce") if chunk is not None else pd.Series(dtype=float)
        values = values.reset_index(drop=True)
        if expected > 0:
            values = values.reindex(range(expected))
        pieces.append(values)

    vector = pd.concat(pieces, ignore_index=True) if pieces else pd.Series(dtype=float)
    return vector, fallback_index


def run_merge_cli() -> None:
    # Parse all required paths and optional header-skip overrides.
    parser = argparse.ArgumentParser(description="Integrate raw PrP screen data into a tidy table.")
    parser.add_argument("raw_dir", help="Directory (or file within directory) containing raw assay exports")
    parser.add_argument("layout_csv", help="Plate layout file (CSV or Excel workbook)")
    parser.add_argument("output_csv", help="Integrated output CSV")
    parser.add_argument("--skip-fret", type=int, default=38)
    parser.add_argument("--skip-glo", type=int, default=9)
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging.")
    args = parser.parse_args()
    debug_enabled = DEBUG_ENV_DEFAULT or args.debug

    # Discover assay files recursively with case-insensitive matching and structural fallback.
    raw_input = Path(args.raw_dir)
    raw_dir = raw_input if raw_input.is_dir() else raw_input.parent
    layout_path = Path(args.layout_csv).resolve()
    all_candidates = [p for p in discover_candidate_measurement_files(raw_dir) if p.resolve() != layout_path]

    fret_files = [
        p
        for p in all_candidates
        if any(tag in p.name.lower() for tag in ("fret", "tr-fret", "trfret"))
    ]
    glo_files = [
        p
        for p in all_candidates
        if any(tag in p.name.lower() for tag in ("glo", "celltiter", "ctg"))
    ]

    if not fret_files:
        fret_files = [p for p in all_candidates if p not in glo_files and looks_like_plate_measurement_file(p, args.skip_fret)]
    if not glo_files:
        glo_files = [p for p in all_candidates if p not in fret_files and looks_like_plate_measurement_file(p, args.skip_glo)]

    debug_log(f"Discovered {len(fret_files)} TR-FRET/FRET files and {len(glo_files)} GLO files", debug_enabled)
    debug_log(f"TR-FRET labels: {derive_plate_labels(fret_files)}", debug_enabled)
    debug_log(f"GLO labels: {derive_plate_labels(glo_files)}", debug_enabled)
    if not fret_files:
        raise RuntimeError(
            f"No FRET-like measurement files found under raw_dir '{raw_dir}'. "
            "Searched *.csv/*.tsv/*.txt and attempted structure-based fallback detection."
        )
    if not glo_files:
        print(
            "[02_merge_assay_exports] WARNING: No GLO CSV files found; continuing without CellTiterGlo_raw.",
            file=sys.stderr,
        )

    # Load all files, preserving file order for deterministic concatenation.
    fret_tables = [load_measurement_table(p, args.skip_fret) for p in fret_files]
    glo_tables = [load_measurement_table(p, args.skip_glo) for p in glo_files]
    debug_log(f"Loaded {len(fret_tables)} TR-FRET tables and {len(glo_tables)} GLO tables", debug_enabled)

    # Load annotation/layout table used as row-level output scaffold.
    layout = load_layout_table(layout_path)
    layout = drop_stale_analysis_columns(layout)
    debug_log(f"Layout table rows: {len(layout)}", debug_enabled)

    # Flatten each modality across files into numeric vectors.
    # Prefer explicit plate-label alignment to layout ordering.
    layout_plate_order, layout_plate_counts = _layout_plate_order_and_counts(layout)
    known_layout_labels = set(layout_plate_order)
    rep1_by_plate: dict[str, pd.Series] = {}
    rep2_by_plate: dict[str, pd.Series] = {}
    rep1_fallback_chunks: list[pd.Series] = []
    rep2_fallback_chunks: list[pd.Series] = []
    undecidable_chunks: list[pd.Series] = []

    for path, table in zip(fret_files, fret_tables):
        vals = flatten_plate_measurements(table, apply_trfret_correction=True)
        rep = classify_fret_replicate(path)
        plate_label = infer_plate_label(path, known_layout_labels)
        expected_len = layout_plate_counts.get(plate_label, 384) if plate_label else 384

        if rep == 1:
            if plate_label:
                current = rep1_by_plate.get(plate_label)
                rep1_by_plate[plate_label] = vals if current is None else _choose_better_chunk(current, vals, expected_len)
            else:
                rep1_fallback_chunks.append(vals)
        elif rep == 2:
            if plate_label:
                current = rep2_by_plate.get(plate_label)
                rep2_by_plate[plate_label] = vals if current is None else _choose_better_chunk(current, vals, expected_len)
            else:
                rep2_fallback_chunks.append(vals)
        else:
            undecidable_chunks.append(vals)

    # If names are undecidable, distribute chunks to keep replicate vectors balanced.
    for chunk in undecidable_chunks:
        if len(rep1_fallback_chunks) <= len(rep2_fallback_chunks):
            rep1_fallback_chunks.append(chunk)
        else:
            rep2_fallback_chunks.append(chunk)

    flat_fret_rep1, used_rep1_fallback = _assemble_replicate_vector(
        layout_plate_order, layout_plate_counts, rep1_by_plate, rep1_fallback_chunks
    )
    flat_fret_rep2, used_rep2_fallback = _assemble_replicate_vector(
        layout_plate_order, layout_plate_counts, rep2_by_plate, rep2_fallback_chunks
    )
    if len(flat_fret_rep1) == 0 and len(flat_fret_rep2) == 0 and fret_tables:
        # Final fallback: split by alternating file order.
        alt1 = [flatten_plate_measurements(t, apply_trfret_correction=True) for i, t in enumerate(fret_tables) if i % 2 == 0]
        alt2 = [flatten_plate_measurements(t, apply_trfret_correction=True) for i, t in enumerate(fret_tables) if i % 2 == 1]
        flat_fret_rep1 = pd.concat(alt1, ignore_index=True) if alt1 else pd.Series(dtype=float)
        flat_fret_rep2 = pd.concat(alt2, ignore_index=True) if alt2 else pd.Series(dtype=float)
    debug_log(
        "Aligned TR-FRET by layout plate order: "
        f"rep1 mapped={len(rep1_by_plate)}/{len(layout_plate_order)} "
        f"rep2 mapped={len(rep2_by_plate)}/{len(layout_plate_order)} "
        f"fallback_used rep1={used_rep1_fallback}/{len(rep1_fallback_chunks)} "
        f"rep2={used_rep2_fallback}/{len(rep2_fallback_chunks)}",
        debug_enabled,
    )

    flat_glo = (
        pd.concat([flatten_plate_measurements(t, apply_trfret_correction=False) for t in glo_tables], ignore_index=True)
        if glo_tables
        else pd.Series(dtype=float)
    )
    debug_log(
        f"Flat TR-FRET lengths: rep1={len(flat_fret_rep1)} rep2={len(flat_fret_rep2)}; flat GLO length={len(flat_glo)}",
        debug_enabled,
    )

    # Build output by extending layout with numeric signal columns.
    out = layout.copy()
    if len(flat_fret_rep1) > 0 or len(flat_fret_rep2) > 0:
        aligned_rep1 = pd.to_numeric(flat_fret_rep1, errors="coerce").reindex(range(len(out)))
        aligned_rep2 = pd.to_numeric(flat_fret_rep2, errors="coerce").reindex(range(len(out)))
        out["Raw_rep1"] = aligned_rep1.to_numpy()
        out["Raw_rep2"] = aligned_rep2.to_numpy()
        debug_log("Attached Raw_rep1/Raw_rep2 from inferred replicate file groups", debug_enabled)
    else:
        out["Raw_rep1"] = np.nan
        out["Raw_rep2"] = np.nan
        debug_log("No FRET values attached; Raw_rep1/Raw_rep2 set to NaN", debug_enabled)
    if len(flat_glo) > 0:
        aligned_glo = pd.to_numeric(flat_glo, errors="coerce").reindex(range(len(out)))
        out["CellTiterGlo_raw"] = aligned_glo.to_numpy()
        debug_log("Attached CellTiterGlo_raw column", debug_enabled)

    # Write merged table for subsequent normalization and plotting stages.
    out.to_csv(args.output_csv, index=False)
    debug_log(f"Wrote integrated dataset: {args.output_csv} ({len(out)} rows)", debug_enabled)


if __name__ == "__main__":
    run_merge_cli()
