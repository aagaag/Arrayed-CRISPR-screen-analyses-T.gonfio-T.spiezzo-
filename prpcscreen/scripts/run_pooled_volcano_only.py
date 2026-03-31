from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def debug_log(message: str, enabled: bool) -> None:
    if enabled:
        print(f"[run_pooled_volcano_only] {message}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a pooled-only CRISPR workflow that outputs just the interactive volcano plot."
    )
    parser.add_argument("input_table", help="CSV/TSV/TXT/XLSX pooled table.")
    parser.add_argument("--output-dir", default="/srv/crispr/output", help="Output directory.")
    parser.add_argument("--genomics-excel", default=None, help="Optional genomics workbook for sublibrary metadata.")
    parser.add_argument("--sheet", default=None, help="Optional Excel sheet for the pooled input workbook.")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging.")
    return parser


def run_step(label: str, cmd: list[str], debug_enabled: bool) -> None:
    debug_log(f"Starting {label}: {' '.join(cmd)}", debug_enabled)
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line.rstrip("\n"))
    rc = proc.wait()
    if rc != 0:
        raise SystemExit(f"{label} failed with exit code {rc}")
    debug_log(f"Finished {label}", debug_enabled)


def main() -> None:
    args = build_parser().parse_args()
    debug_enabled = bool(args.debug)

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    if output_dir.exists():
        shutil.rmtree(output_dir)
    figures_dir = output_dir / "figures"
    output_dir.mkdir(parents=True, exist_ok=True)
    figures_dir.mkdir(parents=True, exist_ok=True)

    integrated_csv = output_dir / "01_integrated.csv"
    analyzed_csv = output_dir / "02_analyzed.csv"
    hits_csv = output_dir / "03_hits.csv"
    volcano_html = figures_dir / "candidate_volcano_interactive.html"
    temp_flashlight_png = output_dir / "_pooled_volcano_scratch.png"

    python = sys.executable

    compute_cmd = [
        python,
        "prpcscreen/scripts/compute_pooled_metrics.py",
        str(args.input_table),
        str(analyzed_csv),
        "--integrated_csv",
        str(integrated_csv),
        "--hits_csv",
        str(hits_csv),
    ]
    if args.sheet:
        compute_cmd.extend(["--sheet", str(args.sheet)])
    if debug_enabled:
        compute_cmd.append("--debug")

    volcano_cmd = [
        python,
        "prpcscreen/scripts/plot_candidate_landscape.py",
        str(analyzed_csv),
        str(temp_flashlight_png),
        "--volcano_html",
        str(volcano_html),
    ]
    if args.genomics_excel:
        volcano_cmd.extend(["--genomics_excel", str(args.genomics_excel)])
    if debug_enabled:
        volcano_cmd.append("--debug")

    run_step("Compute pooled metrics", compute_cmd, debug_enabled)
    run_step("Generate pooled interactive volcano", volcano_cmd, debug_enabled)

    for scratch in output_dir.glob("_pooled_volcano_scratch*"):
        if scratch.is_file():
            scratch.unlink()

    print("Pooled volcano pipeline completed.")
    print(f"Volcano HTML: {volcano_html}")


if __name__ == "__main__":
    main()
