from __future__ import annotations

import gzip
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats
from scipy.optimize import brentq
from scipy.special import digamma, polygamma

DEFAULT_SUBLIBRARY_MAP_CSV = (
    Path(__file__).resolve().parents[1] / "misc" / "supplementary_sublibrary_map.csv"
)


def _candidate_mask(df: pd.DataFrame) -> pd.Series:
    mask = pd.Series(True, index=df.index)
    nt = df.get("Is_NT_ctrl", pd.Series(False, index=df.index)).astype(bool)
    pos = df.get("Is_pos_ctrl", pd.Series(False, index=df.index)).astype(bool)
    is_control = nt | pos

    if "Gene_symbol" in df.columns:
        gene = df["Gene_symbol"].astype(str).str.strip()
        gene_ok = df["Gene_symbol"].notna() & gene.ne("")
        if "Entrez_ID" in df.columns:
            entrez_ok = pd.to_numeric(df["Entrez_ID"], errors="coerce").notna()
            mask &= is_control | gene_ok | entrez_ok
        else:
            mask &= is_control | gene_ok
    elif "Entrez_ID" in df.columns:
        mask &= is_control | pd.to_numeric(df["Entrez_ID"], errors="coerce").notna()

    return mask


def _prepare_volcano_dataframe(df: pd.DataFrame, x_col: str, p_col: str) -> tuple[pd.DataFrame, pd.Series]:
    keep = _candidate_mask(df)
    x = pd.to_numeric(df.loc[keep, x_col], errors="coerce")
    p = pd.to_numeric(df.loc[keep, p_col], errors="coerce")
    y = -np.log10(np.clip(p, 1e-300, 1.0))

    plot_df = df.loc[keep].copy()
    plot_df["_x"] = x
    plot_df["_y"] = y
    plot_df = plot_df[np.isfinite(plot_df["_x"]) & np.isfinite(plot_df["_y"])].copy()
    return plot_df, keep


def _resolve_hit_cutoffs(df: pd.DataFrame, default_p: float = 0.05, default_log2fc: float = 1.0) -> tuple[float, float]:
    p_cutoff = float(default_p)
    log2fc_cutoff = float(default_log2fc)

    if "Hit_p_cutoff" in df.columns:
        p_vals = pd.to_numeric(df["Hit_p_cutoff"], errors="coerce").dropna()
        if not p_vals.empty:
            p_candidate = float(p_vals.iloc[0])
            if np.isfinite(p_candidate) and 0 < p_candidate <= 1:
                p_cutoff = p_candidate

    if "Hit_log2fc_cutoff" in df.columns:
        fc_vals = pd.to_numeric(df["Hit_log2fc_cutoff"], errors="coerce").dropna()
        if not fc_vals.empty:
            fc_candidate = float(fc_vals.iloc[0])
            if np.isfinite(fc_candidate) and fc_candidate > 0:
                log2fc_cutoff = fc_candidate

    return p_cutoff, log2fc_cutoff


def _trigamma_inverse(y: float) -> float:
    if (not np.isfinite(y)) or y <= 0:
        return 1e8

    def _f(x: float) -> float:
        return float(polygamma(1, x) - y)

    lo = 1e-12
    hi = 1.0
    while _f(hi) > 0 and hi < 1e12:
        hi *= 2.0
    if hi >= 1e12:
        return 1e8
    return float(brentq(_f, lo, hi, maxiter=200))


def _compute_limma_moderated_p(
    df: pd.DataFrame,
    rep1_col: str = "Log2FC_rep1",
    rep2_col: str = "Log2FC_rep2",
) -> pd.Series:
    rep1 = pd.to_numeric(df.get(rep1_col), errors="coerce")
    rep2 = pd.to_numeric(df.get(rep2_col), errors="coerce")
    out = pd.Series(np.nan, index=df.index, dtype=float)

    if rep1.isna().all() or rep2.isna().all():
        return out

    mean_vec = (rep1 + rep2) / 2.0
    s2 = ((rep1 - rep2) ** 2) / 2.0

    valid = np.isfinite(s2) & s2.gt(0)
    if int(valid.sum()) < 10:
        return out

    df1 = 1.0
    log_s2 = np.log(pd.to_numeric(s2[valid], errors="coerce"))
    e = log_s2 - (digamma(df1 / 2.0) - np.log(df1 / 2.0))
    e_mean = float(np.nanmean(e))
    var_e = float(np.nanvar(e, ddof=1))
    residual = var_e - float(polygamma(1, df1 / 2.0))

    if (not np.isfinite(residual)) or residual <= 1e-12:
        df0 = 1e8
    else:
        df0 = 2.0 * _trigamma_inverse(residual)
    if (not np.isfinite(df0)) or df0 <= 0:
        df0 = 1e8

    s02 = float(np.exp(e_mean + digamma(df0 / 2.0) - np.log(df0 / 2.0)))
    s2_post = (df0 * s02 + df1 * s2) / (df0 + df1)
    denom = np.sqrt(s2_post / 2.0)
    t_mod = np.divide(
        mean_vec,
        denom,
        out=np.full(len(df), np.nan, dtype=float),
        where=np.isfinite(denom) & denom.gt(0),
    )
    p = 2.0 * stats.t.sf(np.abs(t_mod), df=df0 + df1)
    p = np.clip(p, 1e-300, 1.0)
    out[:] = p
    return out


def _compute_volcano_x_limits(plot_df: pd.DataFrame, log2fc_cutoff: float) -> tuple[float, float]:
    if plot_df.empty:
        return -2.0, 2.0
    data_min = float(plot_df["_x"].min())
    data_max = float(plot_df["_x"].max())
    data_min = min(data_min, -log2fc_cutoff)
    data_max = max(data_max, log2fc_cutoff)
    span = data_max - data_min
    if not np.isfinite(span) or span <= 0:
        span = 1.0
    pad = max(0.10, 0.05 * span)
    return data_min - pad, data_max + pad


def _clean_text_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series("", index=df.index, dtype=object)
    return df[col].fillna("").astype(str).str.strip()


def _display_symbol_series(df: pd.DataFrame) -> pd.Series:
    symbol = _clean_text_series(df, "Gene_symbol")
    pos_mask = df.get("Is_pos_ctrl", pd.Series(False, index=df.index)).astype(bool)
    # Positive controls in this dataset often have blank Gene_symbol but represent PRNP.
    symbol = symbol.mask(symbol.eq("") & pos_mask, "PRNP")
    return symbol


def _build_search_aliases(df: pd.DataFrame, display_symbol: pd.Series) -> list[str]:
    pos_mask = df.get("Is_pos_ctrl", pd.Series(False, index=df.index)).astype(bool)
    nt_mask = df.get("Is_NT_ctrl", pd.Series(False, index=df.index)).astype(bool) & ~pos_mask
    gene_symbol = _clean_text_series(df, "Gene_symbol")
    plasmid = _clean_text_series(df, "Plasmid_ID")
    tss = _clean_text_series(df, "TSS_ID")
    entrez = _clean_text_series(df, "Entrez_ID")

    aliases: list[str] = []
    for idx in df.index:
        keys: list[str] = []
        for raw in (display_symbol.at[idx], gene_symbol.at[idx], plasmid.at[idx], tss.at[idx], entrez.at[idx]):
            text = str(raw).strip()
            if text:
                keys.append(text.upper())
        if bool(pos_mask.at[idx]):
            keys.extend(["PRNP", "POSCTRL", "POSITIVE_CONTROL"])
        if bool(nt_mask.at[idx]):
            keys.extend(["Non-targeting", "NT_CTRL", "NEGATIVE_CONTROL", "NON_TARGETING"])

        deduped: list[str] = []
        seen: set[str] = set()
        for key in keys:
            if key not in seen:
                seen.add(key)
                deduped.append(key)
        aliases.append("|".join(deduped))
    return aliases


def _plate_id_series(df: pd.DataFrame) -> pd.Series:
    plate = _clean_text_series(df, "Plate_number_384")
    return plate.mask(plate.eq(""), "NA")


def _well_coordinate_series(df: pd.DataFrame) -> pd.Series:
    if "Well_number_384" not in df.columns:
        return pd.Series("NA", index=df.index, dtype=object)

    well_num = pd.to_numeric(df["Well_number_384"], errors="coerce")
    coords: list[str] = []
    for value in well_num.tolist():
        if pd.isna(value):
            coords.append("NA")
            continue
        well = int(value)
        if 1 <= well <= 384:
            row_idx = (well - 1) // 24
            col_idx = (well - 1) % 24 + 1
            row = chr(ord("A") + row_idx)
            coords.append(f"{row}{col_idx:02d}")
        else:
            coords.append(str(well))
    return pd.Series(coords, index=df.index, dtype=object)


def _find_column_case_insensitive(columns: list[object], candidates: list[str]) -> str | None:
    lookup = {str(col).strip().lower(): str(col) for col in columns}
    for candidate in candidates:
        hit = lookup.get(candidate.strip().lower())
        if hit:
            return hit
    return None


def _normalize_text_key(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    return text.upper()


def _normalize_entrez_key(value: object) -> str:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return ""
    rounded = float(numeric)
    if np.isfinite(rounded) and rounded.is_integer():
        return str(int(rounded))
    return str(rounded)


def _load_sublibrary_lookup(
    genomics_excel: str | Path | None,
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str], list[str]]:
    gene_map: dict[str, str] = {}
    entrez_map: dict[str, str] = {}
    tss_map: dict[str, str] = {}
    plasmid_map: dict[str, str] = {}
    ordered_sublibraries: list[str] = []

    def _fill_from_table(table: pd.DataFrame) -> bool:
        nonlocal gene_map, entrez_map, tss_map, plasmid_map, ordered_sublibraries
        if table.empty:
            return False

        sub_col = _find_column_case_insensitive(list(table.columns), ["Sublibrary"])
        if sub_col is None:
            return False

        gene_col = _find_column_case_insensitive(
            list(table.columns),
            ["Gene_symbol", "Gene symbol", "GeneSymbol", "Gene", "Symbol"],
        )
        entrez_col = _find_column_case_insensitive(
            list(table.columns),
            ["Entrez_ID", "Entrez ID", "Entrez", "EntrezID"],
        )
        tss_col = _find_column_case_insensitive(
            list(table.columns),
            ["TSS_ID", "TSS ID", "TSS", "TSSID"],
        )
        plasmid_col = _find_column_case_insensitive(
            list(table.columns),
            ["Plasmid_ID", "Plasmid ID", "Plasmid", "PlasmidID"],
        )

        seen_sublibraries = set(ordered_sublibraries)
        for _, row in table.iterrows():
            sublibrary_raw = "" if pd.isna(row[sub_col]) else str(row[sub_col]).strip()
            if not sublibrary_raw:
                continue
            if sublibrary_raw not in seen_sublibraries:
                seen_sublibraries.add(sublibrary_raw)
                ordered_sublibraries.append(sublibrary_raw)

            if gene_col is not None:
                gene_key = _normalize_text_key(row[gene_col])
                if gene_key and gene_key not in gene_map:
                    gene_map[gene_key] = sublibrary_raw

            if entrez_col is not None:
                entrez_key = _normalize_entrez_key(row[entrez_col])
                if entrez_key and entrez_key not in entrez_map:
                    entrez_map[entrez_key] = sublibrary_raw

            if tss_col is not None:
                tss_key = _normalize_text_key(row[tss_col])
                if tss_key and tss_key not in tss_map:
                    tss_map[tss_key] = sublibrary_raw

            if plasmid_col is not None:
                plasmid_key = _normalize_text_key(row[plasmid_col])
                if plasmid_key and plasmid_key not in plasmid_map:
                    plasmid_map[plasmid_key] = sublibrary_raw
        return True

    loaded_from_input = False
    if genomics_excel is not None:
        path = Path(genomics_excel)
        if path.exists():
            try:
                workbook = pd.ExcelFile(path)
                for sheet_name in workbook.sheet_names:
                    try:
                        header = pd.read_excel(path, sheet_name=sheet_name, nrows=0)
                    except Exception:
                        continue
                    if _find_column_case_insensitive(list(header.columns), ["Sublibrary"]) is None:
                        continue
                    try:
                        table = pd.read_excel(path, sheet_name=sheet_name)
                    except Exception:
                        continue
                    if _fill_from_table(table):
                        loaded_from_input = True
            except Exception:
                loaded_from_input = False

    if not loaded_from_input and DEFAULT_SUBLIBRARY_MAP_CSV.exists():
        try:
            table = pd.read_csv(DEFAULT_SUBLIBRARY_MAP_CSV)
            _fill_from_table(table)
        except Exception:
            pass

    return gene_map, entrez_map, tss_map, plasmid_map, ordered_sublibraries


def _build_sublibrary_series(plot_df: pd.DataFrame, genomics_excel: str | Path | None) -> tuple[pd.Series, list[str]]:
    sublibrary = pd.Series("Unknown", index=plot_df.index, dtype=object)
    pos_mask = plot_df.get("Is_pos_ctrl", pd.Series(False, index=plot_df.index)).astype(bool)
    nt_mask = plot_df.get("Is_NT_ctrl", pd.Series(False, index=plot_df.index)).astype(bool) & ~pos_mask
    gene_mask = ~(pos_mask | nt_mask)

    sublibrary.loc[pos_mask] = "Positive controls"
    sublibrary.loc[nt_mask] = "Negative controls"

    gene_map, entrez_map, tss_map, plasmid_map, ordered_sublibraries = _load_sublibrary_lookup(genomics_excel)

    if gene_mask.any():
        gene_symbol = _clean_text_series(plot_df, "Gene_symbol").str.upper()
        tss_id = _clean_text_series(plot_df, "TSS_ID").str.upper()
        plasmid_id = _clean_text_series(plot_df, "Plasmid_ID").str.upper()
        if "Entrez_ID" in plot_df.columns:
            entrez_key = pd.to_numeric(plot_df["Entrez_ID"], errors="coerce").map(
                lambda value: "" if pd.isna(value) else (str(int(value)) if float(value).is_integer() else str(float(value)))
            )
        else:
            entrez_key = pd.Series("", index=plot_df.index, dtype=object)

        for idx in plot_df.index[gene_mask]:
            found = (
                gene_map.get(gene_symbol.at[idx], "")
                or tss_map.get(tss_id.at[idx], "")
                or plasmid_map.get(plasmid_id.at[idx], "")
                or entrez_map.get(str(entrez_key.at[idx]), "")
            )
            if found:
                sublibrary.at[idx] = found

    ordered_options = list(ordered_sublibraries)
    if not ordered_options and gene_mask.any():
        values = [str(v).strip() for v in sublibrary.loc[gene_mask].tolist() if str(v).strip() and str(v).strip() != "Unknown"]
        seen: set[str] = set()
        ordered_options = []
        for value in values:
            if value not in seen:
                seen.add(value)
                ordered_options.append(value)

    if gene_mask.any() and (sublibrary.loc[gene_mask] == "Unknown").any() and "Unknown" not in ordered_options:
        ordered_options.append("Unknown")

    return sublibrary, ordered_options


def volcano_plot(df: pd.DataFrame, x_col: str = "Mean_log2", p_col: str = "p_value_log2"):
    plot_df, keep = _prepare_volcano_dataframe(df, x_col=x_col, p_col=p_col)

    pos_mask = plot_df.get("Is_pos_ctrl", pd.Series(False, index=plot_df.index)).astype(bool)
    nt_mask = plot_df.get("Is_NT_ctrl", pd.Series(False, index=plot_df.index)).astype(bool) & ~pos_mask
    gene_mask = ~(pos_mask | nt_mask)

    fig, ax = plt.subplots(figsize=(6.3, 5.1))

    # Prefer cutoffs embedded in analyzed outputs; otherwise use historical defaults.
    p_cutoff, log2fc_cutoff = _resolve_hit_cutoffs(df, default_p=0.05, default_log2fc=1.0)
    y_cutoff = -np.log10(p_cutoff)

    # Fit x-limits to current data while keeping cutoff lines visible.
    x_min, x_max = _compute_volcano_x_limits(plot_df, log2fc_cutoff=log2fc_cutoff)

    y_min, y_max = -0.05, 2.25
    ax.set_xlim(x_min, x_max)
    ax.set_ylim(y_min, y_max)
    ax.fill_between([x_min, -log2fc_cutoff], y_cutoff, y_max, color="#EBEBEB", zorder=0)
    ax.fill_between([log2fc_cutoff, x_max], y_cutoff, y_max, color="#EBEBEB", zorder=0)

    # Reference lines.
    ax.axhline(0, color="grey", ls=":", lw=0.6, zorder=1)
    ax.axvline(0, color="grey", ls=":", lw=0.6, zorder=1)
    ax.axhline(y_cutoff, color="#E8E8E8", lw=0.6, zorder=1)
    ax.axvline(-log2fc_cutoff, color="#E8E8E8", lw=0.6, zorder=1)
    ax.axvline(log2fc_cutoff, color="#E8E8E8", lw=0.6, zorder=1)

    # R-style defaults: genes black, Non-targeting blue, positive controls red.
    ax.scatter(plot_df.loc[gene_mask, "_x"], plot_df.loc[gene_mask, "_y"], s=3, alpha=0.30, c="#000000", label="Genes")
    ax.scatter(plot_df.loc[pos_mask, "_x"], plot_df.loc[pos_mask, "_y"], s=3, alpha=0.50, c="#de2d26", label="Positive controls")
    ax.scatter(plot_df.loc[nt_mask, "_x"], plot_df.loc[nt_mask, "_y"], s=3, alpha=0.50, c="#3182bd", label="Negative controls")

    # Emphasize gene hits in black.
    hit_mask = gene_mask & (np.abs(plot_df["_x"]) >= log2fc_cutoff) & (plot_df["_y"] >= y_cutoff)
    ax.scatter(plot_df.loc[hit_mask, "_x"], plot_df.loc[hit_mask, "_y"], s=3, alpha=1.0, c="#000000", zorder=4)

    ax.set_xlabel(x_col)
    ax.set_ylabel(f"-log10({p_col})")
    removed = int((~keep).sum())
    ax.set_title("Volcano plot")
    if removed:
        ax.text(0.99, 0.01, f"Filtered rows: {removed}", transform=ax.transAxes, ha="right", va="bottom", fontsize=8, color="#555555")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(frameon=False, fontsize=7.5, loc="center left", bbox_to_anchor=(1.02, 0.5))
    return fig, ax


def write_interactive_volcano_html(
    df: pd.DataFrame,
    output_html: str | Path,
    x_col: str = "Mean_log2",
    p_col: str = "p_value_log2",
    genomics_excel: str | Path | None = None,
    write_gzip_sidecar: bool = True,
) -> Path:
    keep = _candidate_mask(df)
    x = pd.to_numeric(df.loc[keep, x_col], errors="coerce")
    p_student = pd.to_numeric(df.loc[keep, p_col], errors="coerce")
    if "p_value_limma_moderated" in df.columns:
        p_limma_full = pd.to_numeric(df["p_value_limma_moderated"], errors="coerce")
    else:
        p_limma_full = _compute_limma_moderated_p(df, rep1_col="Log2FC_rep1", rep2_col="Log2FC_rep2")
    p_limma = pd.to_numeric(p_limma_full.loc[keep], errors="coerce")

    plot_df = df.loc[keep].copy()
    plot_df["_x"] = x
    plot_df["_p_student"] = p_student
    plot_df["_p_limma"] = p_limma
    plot_df["_y_student"] = -np.log10(np.clip(p_student, 1e-300, 1.0))
    plot_df["_y_limma"] = -np.log10(np.clip(p_limma, 1e-300, 1.0))
    plot_df = plot_df[
        np.isfinite(plot_df["_x"])
        & np.isfinite(plot_df["_y_student"])
        & np.isfinite(plot_df["_y_limma"])
    ].copy()

    pos_mask = plot_df.get("Is_pos_ctrl", pd.Series(False, index=plot_df.index)).astype(bool)
    nt_mask = plot_df.get("Is_NT_ctrl", pd.Series(False, index=plot_df.index)).astype(bool) & ~pos_mask
    gene_mask = ~(pos_mask | nt_mask)
    plot_df["_sublibrary"], sublibrary_options = _build_sublibrary_series(plot_df, genomics_excel)

    p_cutoff, log2fc_cutoff = _resolve_hit_cutoffs(df, default_p=0.05, default_log2fc=1.0)
    y_cutoff = -np.log10(p_cutoff)
    x_min, x_max = _compute_volcano_x_limits(plot_df, log2fc_cutoff=log2fc_cutoff)
    y_min = -0.05
    if plot_df.empty:
        y_max = 2.25
    else:
        y_max = float(
            max(
                2.25,
                np.nanmax(
                    [
                        float(plot_df["_y_student"].max()),
                        float(plot_df["_y_limma"].max()),
                    ]
                )
                + 0.10,
            )
        )

    hit_mask_student = gene_mask & (np.abs(plot_df["_x"]) >= log2fc_cutoff) & (plot_df["_y_student"] >= y_cutoff)
    hit_mask_limma = gene_mask & (np.abs(plot_df["_x"]) >= log2fc_cutoff) & (plot_df["_y_limma"] >= y_cutoff)

    def _trace_payload(mask: pd.Series) -> dict[str, list | float]:
        sub = plot_df.loc[mask].copy()
        symbol = _display_symbol_series(sub)
        plate = _plate_id_series(sub)
        coord = _well_coordinate_series(sub)
        sublibrary = sub["_sublibrary"].fillna("Unknown").astype(str)
        if "Entrez_ID" in sub.columns:
            entrez_series = pd.to_numeric(sub["Entrez_ID"], errors="coerce").map(
                lambda value: "" if pd.isna(value) else (str(int(value)) if float(value).is_integer() else str(float(value)))
            )
        else:
            entrez_series = pd.Series("", index=sub.index, dtype=object)
        customdata = [
            [a, p, c, float(ps), float(pl), s, e]
            for a, p, c, ps, pl, s, e in zip(
                _build_search_aliases(sub, symbol),
                plate.tolist(),
                coord.tolist(),
                sub["_p_student"].astype(float).tolist(),
                sub["_p_limma"].astype(float).tolist(),
                sublibrary.tolist(),
                entrez_series.astype(str).tolist(),
            )
        ]
        return {
            "x": sub["_x"].astype(float).tolist(),
            "y_student": sub["_y_student"].astype(float).tolist(),
            "y_limma": sub["_y_limma"].astype(float).tolist(),
            "gene_symbol": symbol.tolist(),
            "sublibrary": sublibrary.tolist(),
            "customdata": customdata,
        }

    genes_payload = _trace_payload(gene_mask)
    pos_payload = _trace_payload(pos_mask)
    nt_payload = _trace_payload(nt_mask)
    hit_payload_student = _trace_payload(hit_mask_student)
    hit_payload_limma = _trace_payload(hit_mask_limma)

    y_title_student = "-log10(p): primary p_value_log2 column"
    y_title_limma = "-log10(p): limma moderated t (empirical-Bayes variance shrinkage)"

    traces = [
        {
            "x": genes_payload["x"],
            "y": genes_payload["y_student"],
            "_x_all": genes_payload["x"],
            "_y_student_all": genes_payload["y_student"],
            "_y_limma_all": genes_payload["y_limma"],
            "_text_all": genes_payload["gene_symbol"],
            "_customdata_all": genes_payload["customdata"],
            "_sublibrary_all": genes_payload["sublibrary"],
            "text": genes_payload["gene_symbol"],
            "customdata": genes_payload["customdata"],
            "mode": "markers",
            "name": "Genes",
            "marker": {"color": "#d6d6d6", "size": 3, "opacity": 0.30},
            "hovertemplate": (
                f"Gene: %{{text}}<br>Sublibrary: %{{customdata[5]}}<br>Plate: %{{customdata[1]}}<br>Coordinate: %{{customdata[2]}}"
                f"<br>{x_col}: %{{x:.3f}}<br>p (primary): %{{customdata[3]:.3g}}<br>p (limma): %{{customdata[4]:.3g}}"
                "<br>p-value: %{y:.3f}<extra></extra>"
            ),
        },
        {
            "x": pos_payload["x"],
            "y": pos_payload["y_student"],
            "_y_student": pos_payload["y_student"],
            "_y_limma": pos_payload["y_limma"],
            "text": pos_payload["gene_symbol"],
            "customdata": pos_payload["customdata"],
            "mode": "markers",
            "name": "Positive controls",
            "marker": {"color": "#de2d26", "size": 3, "opacity": 0.50},
            "hovertemplate": (
                "Positive control"
                "<br>Sublibrary: %{customdata[5]}"
                "<br>Plate: %{customdata[1]}"
                "<br>Coordinate: %{customdata[2]}"
                f"<br>{x_col}: %{{x:.3f}}<br>p (primary): %{{customdata[3]:.3g}}<br>p (limma): %{{customdata[4]:.3g}}"
                "<br>p-value: %{y:.3f}<extra></extra>"
            ),
        },
        {
            "x": nt_payload["x"],
            "y": nt_payload["y_student"],
            "_y_student": nt_payload["y_student"],
            "_y_limma": nt_payload["y_limma"],
            "text": nt_payload["gene_symbol"],
            "customdata": nt_payload["customdata"],
            "mode": "markers",
            "name": "Negative controls",
            "marker": {"color": "#3182bd", "size": 3, "opacity": 0.50},
            "hovertemplate": (
                "Negative control"
                "<br>Sublibrary: %{customdata[5]}"
                "<br>Plate: %{customdata[1]}"
                "<br>Coordinate: %{customdata[2]}"
                f"<br>{x_col}: %{{x:.3f}}<br>p (primary): %{{customdata[3]:.3g}}<br>p (limma): %{{customdata[4]:.3g}}"
                "<br>p-value: %{y:.3f}<extra></extra>"
            ),
        },
        {
            "x": hit_payload_student["x"],
            "y": hit_payload_student["y_student"],
            "text": hit_payload_student["gene_symbol"],
            "customdata": hit_payload_student["customdata"],
            "_x_student": hit_payload_student["x"],
            "_y_student": hit_payload_student["y_student"],
            "_text_student": hit_payload_student["gene_symbol"],
            "_customdata_student": hit_payload_student["customdata"],
            "_x_limma": hit_payload_limma["x"],
            "_y_limma": hit_payload_limma["y_limma"],
            "_text_limma": hit_payload_limma["gene_symbol"],
            "_customdata_limma": hit_payload_limma["customdata"],
            "_sublibrary_student": hit_payload_student["sublibrary"],
            "_sublibrary_limma": hit_payload_limma["sublibrary"],
            "mode": "markers",
            "name": "Gene hits",
            "marker": {"color": "#000000", "size": 3, "opacity": 1.0},
            "hovertemplate": (
                "gene: %{text}"
                "<br>Sublibrary: %{customdata[5]}"
                "<br>Plate: %{customdata[1]}"
                "<br>Coordinate: %{customdata[2]}"
                f"<br>{x_col}: %{{x:.3f}}<br>p (primary): %{{customdata[3]:.3g}}<br>p (limma): %{{customdata[4]:.3g}}"
                "<br>p-value: %{y:.3f}<extra></extra>"
            ),
            "showlegend": False,
        },
        {
            "x": [],
            "y": [],
            "text": [],
            "customdata": [],
            "mode": "markers",
            "name": "Top-ranked genes",
            "marker": {"symbol": "circle-open", "size": 6.6, "color": "#111111", "line": {"width": 2, "color": "#111111"}},
            "hovertemplate": (
                "gene: %{text}"
                "<br>Plate: %{customdata[0]}"
                "<br>Coordinate: %{customdata[1]}"
                f"<br>{x_col}: %{{x:.3f}}<br>p-value: %{{y:.3f}}<extra></extra>"
            ),
            "showlegend": False,
            "visible": False,
        },
        {
            "x": [],
            "y": [],
            "text": [],
            "mode": "text",
            "textposition": "top center",
            "textfont": {"size": 11, "color": "#111111"},
            "name": "Top-ranked gene labels",
            "hoverinfo": "skip",
            "showlegend": False,
            "visible": False,
        },
    ]

    highlight_group_defaults = [
        {"label": "1", "color": "#f59e0b"},
        {"label": "2", "color": "#10b981"},
        {"label": "3", "color": "#2563eb"},
        {"label": "4", "color": "#ef4444"},
    ]
    highlight_group_trace_starts: list[int] = []
    top_marker_trace = len(traces)
    top_label_trace = top_marker_trace + 1
    traces = traces[:-2]
    for group in highlight_group_defaults:
        highlight_group_trace_starts.append(len(traces))
        traces.extend(
            [
                {
                    "x": [],
                    "y": [],
                    "mode": "markers",
                    "name": f"Selected genes ({group['label']} outline)",
                    "marker": {"symbol": "circle", "size": 3, "color": group["color"], "opacity": 0},
                    "hoverinfo": "skip",
                    "showlegend": False,
                    "visible": False,
                },
                {
                    "x": [],
                    "y": [],
                    "text": [],
                    "mode": "markers",
                    "name": f"Selected genes ({group['label']})",
                    "marker": {"symbol": "circle", "size": 3, "color": group["color"], "opacity": 1},
                    "hovertemplate": f"%{{text}}<br>{x_col}: %{{x:.3f}}<br>p-value: %{{y:.3f}}<extra></extra>",
                    "showlegend": False,
                    "visible": False,
                },
                {
                    "x": [],
                    "y": [],
                    "text": [],
                    "mode": "text",
                    "textposition": "top center",
                    "textfont": {"size": 11, "color": group["color"]},
                    "name": f"Selected gene labels ({group['label']})",
                    "hoverinfo": "skip",
                    "showlegend": False,
                    "visible": False,
                },
            ]
        )
    top_marker_trace = len(traces)
    top_label_trace = top_marker_trace + 1
    traces.extend(
        [
            {
                "x": [],
                "y": [],
                "text": [],
                "customdata": [],
                "mode": "markers",
                "name": "Top-ranked genes",
                "marker": {"symbol": "circle-open", "size": 6.6, "color": "#111111", "line": {"width": 2, "color": "#111111"}},
                "hovertemplate": (
                    "gene: %{text}"
                    "<br>Plate: %{customdata[0]}"
                    "<br>Coordinate: %{customdata[1]}"
                    f"<br>{x_col}: %{{x:.3f}}<br>p-value: %{{y:.3f}}<extra></extra>"
                ),
                "showlegend": False,
                "visible": False,
            },
            {
                "x": [],
                "y": [],
                "text": [],
                "mode": "text",
                "textposition": "top center",
                "textfont": {"size": 11, "color": "#111111"},
                "name": "Top-ranked gene labels",
                "hoverinfo": "skip",
                "showlegend": False,
                "visible": False,
            },
        ]
    )

    layout = {
        "title": {"text": "Volcano plot"},
        "paper_bgcolor": "#f6f6f6",
        "plot_bgcolor": "#ffffff",
        "margin": {"l": 70, "r": 40, "t": 60, "b": 60},
        "xaxis": {"title": x_col, "range": [x_min, x_max], "zeroline": False},
        "yaxis": {"title": y_title_student, "range": [y_min, y_max], "zeroline": False},
        "shapes": [
            {"type": "rect", "x0": x_min, "x1": -log2fc_cutoff, "y0": y_cutoff, "y1": y_max, "fillcolor": "#EBEBEB", "opacity": 1, "line": {"width": 0}, "layer": "below"},
            {"type": "rect", "x0": log2fc_cutoff, "x1": x_max, "y0": y_cutoff, "y1": y_max, "fillcolor": "#EBEBEB", "opacity": 1, "line": {"width": 0}, "layer": "below"},
            {"type": "line", "x0": x_min, "x1": x_max, "y0": 0, "y1": 0, "line": {"color": "grey", "width": 1, "dash": "dot"}},
            {"type": "line", "x0": 0, "x1": 0, "y0": y_min, "y1": y_max, "line": {"color": "grey", "width": 1, "dash": "dot"}},
            {"type": "line", "x0": x_min, "x1": x_max, "y0": y_cutoff, "y1": y_cutoff, "line": {"color": "#E8E8E8", "width": 1}},
            {"type": "line", "x0": -log2fc_cutoff, "x1": -log2fc_cutoff, "y0": y_min, "y1": y_max, "line": {"color": "#E8E8E8", "width": 1}},
            {"type": "line", "x0": log2fc_cutoff, "x1": log2fc_cutoff, "y0": y_min, "y1": y_max, "line": {"color": "#E8E8E8", "width": 1}},
        ],
        "showlegend": True,
        "legend": {"orientation": "v", "x": 1.02, "y": 0.5, "xanchor": "left", "yanchor": "middle"},
    }

    traces_json = json.dumps(traces, separators=(",", ":"))
    layout_json = json.dumps(layout, separators=(",", ":"))
    y_title_student_json = json.dumps(y_title_student, separators=(",", ":"))
    y_title_limma_json = json.dumps(y_title_limma, separators=(",", ":"))
    sublibrary_options_json = json.dumps(sublibrary_options, separators=(",", ":"))
    x_min_json = json.dumps(float(x_min), separators=(",", ":"))
    x_max_json = json.dumps(float(x_max), separators=(",", ":"))
    y_min_json = json.dumps(float(y_min), separators=(",", ":"))
    y_max_json = json.dumps(float(y_max), separators=(",", ":"))
    p_cutoff_json = json.dumps(float(p_cutoff), separators=(",", ":"))
    log2fc_cutoff_json = json.dumps(float(log2fc_cutoff), separators=(",", ":"))
    highlight_group_trace_starts_json = json.dumps(highlight_group_trace_starts, separators=(",", ":"))
    highlight_group_defaults_json = json.dumps(highlight_group_defaults, separators=(",", ":"))

    html = (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "  <title>Interactive Volcano Plot</title>\n"
        "  <script src=\"https://cdn.plot.ly/plotly-2.35.2.min.js\"></script>\n"
        "  <style>\n"
        "    body { margin: 0; font-family: Segoe UI, Arial, sans-serif; background: #f6f6f6; color: #1f1f1f; }\n"
        "    .wrap { max-width: 1400px; margin: 0 auto; padding: 16px; }\n"
        "    .controls { display: flex; gap: 18px; flex-wrap: wrap; align-items: center; margin-bottom: 10px; font-size: 14px; }\n"
        "    .controls label { display: inline-flex; align-items: center; gap: 4px; user-select: none; }\n"
        "    .gene-controls input { min-width: 320px; border: 1px solid #d0d7de; border-radius: 6px; padding: 6px 8px; font-size: 13px; }\n"
        "    .gene-controls input.color-picker { min-width: 44px; width: 44px; height: 32px; padding: 2px; }\n"
        "    .gene-controls input.threshold-input { min-width: 0; width: 88px; }\n"
        "    .gene-controls input.highlight-input { min-width: 420px; }\n"
        "    .gene-controls select { border: 1px solid #d0d7de; border-radius: 6px; padding: 6px 8px; font-size: 13px; background: #ffffff; }\n"
        "    .gene-controls button { border: 1px solid #c9ced6; border-radius: 6px; background: #ffffff; padding: 6px 10px; cursor: pointer; font-size: 13px; }\n"
        "    .gene-controls button:hover { background: #f4f6f8; }\n"
        "    #gene-status { color: #475569; font-size: 13px; min-height: 1em; }\n"
        "    #sublibrary-status { color: #475569; font-size: 13px; min-height: 1em; }\n"
        "    #top-status { color: #475569; font-size: 13px; min-height: 1em; }\n"
        "    #export-status { color: #475569; font-size: 13px; min-height: 1em; }\n"
        "    #gene-info-text { color: #475569; font-size: 13px; min-height: 1em; }\n"
        "    .gene-links { display: inline-flex; gap: 8px; align-items: center; }\n"
        "    .gene-links a { font-size: 13px; color: #1d4ed8; text-decoration: none; display: none; }\n"
        "    .gene-links a:hover { text-decoration: underline; }\n"
        "    #volcano { width: 100%; height: 78vh; min-height: 620px; border: 1px solid #d8d8d8; background: #ffffff; }\n"
        "    .math-legend { margin-top: 10px; border: 1px solid #d8d8d8; background: #ffffff; padding: 10px 12px; font-size: 12px; line-height: 1.45; }\n"
        "    .math-legend ul { margin: 6px 0 0 18px; }\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <div class=\"wrap\">\n"
        "    <div class=\"controls\">\n"
        "      <label><input id=\"ymode-student\" type=\"radio\" name=\"ymode\"> Primary p-value column (p_value_log2)</label>\n"
        "      <label><input id=\"ymode-limma\" type=\"radio\" name=\"ymode\" checked> limma moderated p (empirical-Bayes moderated t)</label>\n"
        "    </div>\n"
        "    <div class=\"controls\">\n"
        "      <label><input id=\"toggle-genes\" type=\"checkbox\" checked> Experimental genes</label>\n"
        "      <label><input id=\"toggle-pos\" type=\"checkbox\" checked> Positive controls</label>\n"
        "      <label><input id=\"toggle-neg\" type=\"checkbox\" checked> Negative controls</label>\n"
        "    </div>\n"
        "    <div class=\"controls gene-controls\">\n"
        "      <label><input id=\"toggle-threshold-shading\" type=\"checkbox\" checked>Shade significant / effect-size quadrants</label>\n"
        "      <label for=\"effect-cutoff\">Effect-size threshold</label>\n"
        "      <input id=\"effect-cutoff\" class=\"threshold-input\" type=\"number\" min=\"0\" step=\"0.05\" value=\""
        + str(float(log2fc_cutoff))
        + "\" />\n"
        "      <label for=\"p-cutoff\">Significance p-value threshold</label>\n"
        "      <input id=\"p-cutoff\" class=\"threshold-input\" type=\"number\" min=\"0.000000000001\" max=\"1\" step=\"0.001\" value=\""
        + str(float(p_cutoff))
        + "\" />\n"
        "      <span id=\"threshold-status\"></span>\n"
        "    </div>\n"
        "    <div class=\"controls gene-controls\">\n"
        "      <label for=\"color-genes\">Experimental gene color</label>\n"
        "      <input id=\"color-genes\" class=\"color-picker\" type=\"color\" value=\"#d6d6d6\" />\n"
        "      <label for=\"color-pos\">Positive control color</label>\n"
        "      <input id=\"color-pos\" class=\"color-picker\" type=\"color\" value=\"#000000\" />\n"
        "      <label for=\"color-neg\">Negative control color</label>\n"
        "      <input id=\"color-neg\" class=\"color-picker\" type=\"color\" value=\"#000000\" />\n"
        "    </div>\n"
        "    <div class=\"controls gene-controls\">\n"
        "      <label for=\"sublibrary-filter\">Gene sublibrary</label>\n"
        "      <select id=\"sublibrary-filter\"></select>\n"
        "      <span id=\"sublibrary-status\"></span>\n"
        "    </div>\n"
        "    <div class=\"controls gene-controls\">\n"
        "      <label for=\"gene-input-1\">Highlight genes 1</label>\n"
        "      <input id=\"gene-input-1\" class=\"highlight-input\" type=\"text\" placeholder=\"GENE1, GENE2; GENE3 GENE4\" />\n"
        "      <label for=\"gene-color-1\">Color</label>\n"
        "      <input id=\"gene-color-1\" class=\"color-picker\" type=\"color\" value=\"#f59e0b\" />\n"
        "      <label for=\"gene-size-1\">Size</label>\n"
        "      <select id=\"gene-size-1\" class=\"mini-select\"><option value=\"1\" selected>1x</option><option value=\"2\">2x</option><option value=\"3\">3x</option><option value=\"4\">4x</option></select>\n"
        "      <button id=\"gene-apply-1\" type=\"button\">Apply</button>\n"
        "      <button id=\"gene-clear-1\" type=\"button\">Clear</button>\n"
        "      <span id=\"gene-status-1\"></span>\n"
        "    </div>\n"
        "    <div class=\"controls gene-controls\">\n"
        "      <label for=\"gene-input-2\">Highlight genes 2</label>\n"
        "      <input id=\"gene-input-2\" class=\"highlight-input\" type=\"text\" placeholder=\"GENE1, GENE2; GENE3 GENE4\" />\n"
        "      <label for=\"gene-color-2\">Color</label>\n"
        "      <input id=\"gene-color-2\" class=\"color-picker\" type=\"color\" value=\"#10b981\" />\n"
        "      <label for=\"gene-size-2\">Size</label>\n"
        "      <select id=\"gene-size-2\" class=\"mini-select\"><option value=\"1\" selected>1x</option><option value=\"2\">2x</option><option value=\"3\">3x</option><option value=\"4\">4x</option></select>\n"
        "      <button id=\"gene-apply-2\" type=\"button\">Apply</button>\n"
        "      <button id=\"gene-clear-2\" type=\"button\">Clear</button>\n"
        "      <span id=\"gene-status-2\"></span>\n"
        "    </div>\n"
        "    <div class=\"controls gene-controls\">\n"
        "      <label for=\"gene-input-3\">Highlight genes 3</label>\n"
        "      <input id=\"gene-input-3\" class=\"highlight-input\" type=\"text\" placeholder=\"GENE1, GENE2; GENE3 GENE4\" />\n"
        "      <label for=\"gene-color-3\">Color</label>\n"
        "      <input id=\"gene-color-3\" class=\"color-picker\" type=\"color\" value=\"#2563eb\" />\n"
        "      <label for=\"gene-size-3\">Size</label>\n"
        "      <select id=\"gene-size-3\" class=\"mini-select\"><option value=\"1\" selected>1x</option><option value=\"2\">2x</option><option value=\"3\">3x</option><option value=\"4\">4x</option></select>\n"
        "      <button id=\"gene-apply-3\" type=\"button\">Apply</button>\n"
        "      <button id=\"gene-clear-3\" type=\"button\">Clear</button>\n"
        "      <span id=\"gene-status-3\"></span>\n"
        "    </div>\n"
        "    <div class=\"controls gene-controls\">\n"
        "      <label for=\"gene-input-4\">Highlight genes 4</label>\n"
        "      <input id=\"gene-input-4\" class=\"highlight-input\" type=\"text\" placeholder=\"GENE1, GENE2; GENE3 GENE4\" />\n"
        "      <label for=\"gene-color-4\">Color</label>\n"
        "      <input id=\"gene-color-4\" class=\"color-picker\" type=\"color\" value=\"#ef4444\" />\n"
        "      <label for=\"gene-size-4\">Size</label>\n"
        "      <select id=\"gene-size-4\" class=\"mini-select\"><option value=\"1\" selected>1x</option><option value=\"2\">2x</option><option value=\"3\">3x</option><option value=\"4\">4x</option></select>\n"
        "      <button id=\"gene-apply-4\" type=\"button\">Apply</button>\n"
        "      <button id=\"gene-clear-4\" type=\"button\">Clear</button>\n"
        "      <span id=\"gene-status-4\"></span>\n"
        "    </div>\n"
        "    <div class=\"controls gene-controls\">\n"
        "      <label for=\"top-mode\">Auto-label top genes</label>\n"
        "      <select id=\"top-mode\">\n"
        "        <option value=\"effect_balanced\">Largest |effect| (balanced +/-)</option>\n"
        "        <option value=\"smallest_p\">Smallest p-value</option>\n"
        "        <option value=\"combined\" selected>Strongest combo |effect| * -log10(p)</option>\n"
        "      </select>\n"
        "      <select id=\"top-n\">\n"
        "        <option value=\"10\">Top 10</option>\n"
        "        <option value=\"20\">Top 20</option>\n"
        "        <option value=\"50\">Top 50</option>\n"
        "      </select>\n"
        "      <button id=\"top-apply\" type=\"button\">Apply</button>\n"
        "      <button id=\"top-clear\" type=\"button\">Clear</button>\n"
        "      <span id=\"top-status\"></span>\n"
        "    </div>\n"
        "    <div class=\"controls gene-controls\">\n"
        "      <label for=\"export-quality\">Publication SVG</label>\n"
        "      <select id=\"export-quality\">\n"
        "        <option value=\"low\">Low (1200x800)</option>\n"
        "        <option value=\"medium\" selected>Medium (1800x1200)</option>\n"
        "        <option value=\"high\">High (2400x1600)</option>\n"
        "      </select>\n"
        "      <button id=\"export-svg\" type=\"button\">Export SVG</button>\n"
        "      <span id=\"export-status\"></span>\n"
        "    </div>\n"
        "    <div class=\"controls gene-controls\">\n"
        "      <strong>Gene info</strong>\n"
        "      <span id=\"gene-info-text\">Click a point to open external gene resources.</span>\n"
        "      <span class=\"gene-links\">\n"
        "        <a id=\"gene-link-ncbi\" href=\"#\" target=\"_blank\" rel=\"noopener noreferrer\">NCBI Gene</a>\n"
        "        <a id=\"gene-link-uniprot\" href=\"#\" target=\"_blank\" rel=\"noopener noreferrer\">UniProt</a>\n"
        "      </span>\n"
        "    </div>\n"
        "    <div id=\"volcano\"></div>\n"
        "    <div class=\"math-legend\">\n"
        "      <strong>Legend and math</strong>\n"
        "      <ul>\n"
        "        <li><strong>Y-axis mode: Primary p</strong> uses the analyzed table column <code>p_value_log2</code>.</li>\n"
        "        <li><strong>Y-axis mode: limma p</strong> uses empirical-Bayes moderated t-statistics (variance shrinkage across genes, limma-style).</li>\n"
        "        <li><strong>Top labels: Largest |effect| (balanced +/-)</strong> picks genes with largest <code>|Mean_log2|</code>, forcing both negative and positive sides when possible.</li>\n"
        "        <li><strong>Top labels: Smallest p-value</strong> picks lowest p under the currently selected Y-axis model.</li>\n"
        "        <li><strong>Top labels: Strongest combo</strong> uses score <code>|Mean_log2| * -log10(p_active)</code>. This is a ranking score, not a z-value.</li>\n"
        "        <li><strong>Gene sublibrary filter</strong> limits experimental genes and gene-hit overlays to one predefined sublibrary from the genomics workbook; controls remain controlled by category toggles.</li>\n"
        "        <li><strong>Manual highlight</strong> labels only user-entered genes; auto-top and manual highlight are independent overlays.</li>\n"
        "        <li><strong>Export SVG</strong> saves the currently visible state (Y mode, category toggles, labels/highlights) as a publication-ready vector figure.</li>\n"
        "      </ul>\n"
        "    </div>\n"
        "  </div>\n"
        "  <script>\n"
        "    const traces = "
        + traces_json
        + ";\n"
        "    const layout = "
        + layout_json
        + ";\n"
        "    const Y_TITLE_STUDENT = "
        + y_title_student_json
        + ";\n"
        "    const Y_TITLE_LIMMA = "
        + y_title_limma_json
        + ";\n"
        "    const SUBLIBRARY_OPTIONS = "
        + sublibrary_options_json
        + ";\n"
        "    const X_MIN = "
        + x_min_json
        + ";\n"
        "    const X_MAX = "
        + x_max_json
        + ";\n"
        "    const Y_MIN = "
        + y_min_json
        + ";\n"
        "    const Y_MAX = "
        + y_max_json
        + ";\n"
        "    const DEFAULT_P_CUTOFF = "
        + p_cutoff_json
        + ";\n"
        "    const DEFAULT_LOG2FC_CUTOFF = "
        + log2fc_cutoff_json
        + ";\n"
        "    const HIGHLIGHT_GROUP_TRACE_STARTS = "
        + highlight_group_trace_starts_json
        + ";\n"
        "    const HIGHLIGHT_GROUP_DEFAULTS = "
        + highlight_group_defaults_json
        + ";\n"
        "    Plotly.newPlot('volcano', traces, layout, {responsive: true});\n"
        "    const volcanoDiv = document.getElementById('volcano');\n"
        "    const TRACE_GENES = 0;\n"
        "    const TRACE_POS = 1;\n"
        "    const TRACE_NEG = 2;\n"
        "    const TRACE_HITS = 3;\n"
        "    const TRACE_TOP_MARKERS = HIGHLIGHT_GROUP_TRACE_STARTS[HIGHLIGHT_GROUP_TRACE_STARTS.length - 1] + 3;\n"
        "    const TRACE_TOP_LABELS = TRACE_TOP_MARKERS + 1;\n"
        "    const YMODE_STUDENT = 'student';\n"
        "    const YMODE_LIMMA = 'limma';\n"
        "    const DEFAULT_CATEGORY_COLORS = {\n"
        "      genes: '#d6d6d6',\n"
        "      pos: '#1d4ed8',\n"
        "      neg: '#dc2626',\n"
        "    };\n"
        "    const PRIMARY_MARKER_SIZE = Number((traces[TRACE_GENES] && traces[TRACE_GENES].marker && traces[TRACE_GENES].marker.size) || 3);\n"
        "    let currentYMode = YMODE_LIMMA;\n"
        "    let topLabelsActive = false;\n"
        "    function readHighlightScale(group) {\n"
        "      const raw = Number(document.getElementById(group.sizeId).value || 1);\n"
        "      return [1, 2, 3, 4].includes(raw) ? raw : 1;\n"
        "    }\n"
        "    function readEffectCutoff() {\n"
        "      const raw = Number(document.getElementById('effect-cutoff').value);\n"
        "      if (Number.isFinite(raw) && raw >= 0) return raw;\n"
        "      return DEFAULT_LOG2FC_CUTOFF;\n"
        "    }\n"
        "    function readPCutoff() {\n"
        "      const raw = Number(document.getElementById('p-cutoff').value);\n"
        "      if (Number.isFinite(raw) && raw > 0 && raw <= 1) return raw;\n"
        "      return DEFAULT_P_CUTOFF;\n"
        "    }\n"
        "    function currentYCutoff() {\n"
        "      return -Math.log10(readPCutoff());\n"
        "    }\n"
        "    function buildThresholdShapes() {\n"
        "      const effectCutoff = readEffectCutoff();\n"
        "      const yCutoff = currentYCutoff();\n"
        "      const showShading = document.getElementById('toggle-threshold-shading').checked;\n"
        "      const shapes = [];\n"
        "      if (showShading) {\n"
        "        shapes.push({type: 'rect', x0: X_MIN, x1: -effectCutoff, y0: yCutoff, y1: Y_MAX, fillcolor: '#EBEBEB', opacity: 1, line: {width: 0}, layer: 'below'});\n"
        "        shapes.push({type: 'rect', x0: effectCutoff, x1: X_MAX, y0: yCutoff, y1: Y_MAX, fillcolor: '#EBEBEB', opacity: 1, line: {width: 0}, layer: 'below'});\n"
        "        shapes.push({type: 'line', x0: X_MIN, x1: X_MAX, y0: yCutoff, y1: yCutoff, line: {color: '#E8E8E8', width: 1}});\n"
        "        shapes.push({type: 'line', x0: -effectCutoff, x1: -effectCutoff, y0: Y_MIN, y1: Y_MAX, line: {color: '#E8E8E8', width: 1}});\n"
        "        shapes.push({type: 'line', x0: effectCutoff, x1: effectCutoff, y0: Y_MIN, y1: Y_MAX, line: {color: '#E8E8E8', width: 1}});\n"
        "      }\n"
        "      shapes.push({type: 'line', x0: X_MIN, x1: X_MAX, y0: 0, y1: 0, line: {color: 'grey', width: 1, dash: 'dot'}});\n"
        "      shapes.push({type: 'line', x0: 0, x1: 0, y0: Y_MIN, y1: Y_MAX, line: {color: 'grey', width: 1, dash: 'dot'}});\n"
        "      return shapes;\n"
        "    }\n"
        "    function rebuildHitPayloads() {\n"
        "      const effectCutoff = readEffectCutoff();\n"
        "      const yCutoff = currentYCutoff();\n"
        "      const genes = traces[TRACE_GENES];\n"
        "      const build = (useLimma) => {\n"
        "        const xs = genes._x_all || [];\n"
        "        const ys = useLimma ? (genes._y_limma_all || []) : (genes._y_student_all || []);\n"
        "        const txt = genes._text_all || [];\n"
        "        const custom = genes._customdata_all || [];\n"
        "        const sub = genes._sublibrary_all || [];\n"
        "        const out = {x: [], y: [], text: [], customdata: [], sublibrary: []};\n"
        "        const n = Math.min(xs.length, ys.length, txt.length, custom.length, sub.length);\n"
        "        for (let i = 0; i < n; i += 1) {\n"
        "          const x = Number(xs[i]);\n"
        "          const y = Number(ys[i]);\n"
        "          if (!Number.isFinite(x) || !Number.isFinite(y)) continue;\n"
        "          if (Math.abs(x) < effectCutoff || y < yCutoff) continue;\n"
        "          out.x.push(x);\n"
        "          out.y.push(y);\n"
        "          out.text.push(txt[i]);\n"
        "          out.customdata.push(custom[i]);\n"
        "          out.sublibrary.push(sub[i]);\n"
        "        }\n"
        "        return out;\n"
        "      };\n"
        "      const student = build(false);\n"
        "      const limma = build(true);\n"
        "      const hit = traces[TRACE_HITS];\n"
        "      hit._x_student = student.x;\n"
        "      hit._y_student = student.y;\n"
        "      hit._text_student = student.text;\n"
        "      hit._customdata_student = student.customdata;\n"
        "      hit._sublibrary_student = student.sublibrary;\n"
        "      hit._x_limma = limma.x;\n"
        "      hit._y_limma = limma.y;\n"
        "      hit._text_limma = limma.text;\n"
        "      hit._customdata_limma = limma.customdata;\n"
        "      hit._sublibrary_limma = limma.sublibrary;\n"
        "    }\n"
        "    function updateThresholdControls(silent = false) {\n"
        "      const pCutoff = readPCutoff();\n"
        "      const effectCutoff = readEffectCutoff();\n"
        "      const yCutoff = currentYCutoff();\n"
        "      Plotly.relayout('volcano', {shapes: buildThresholdShapes()});\n"
        "      rebuildHitPayloads();\n"
        "      applyGeneSublibraryFilter();\n"
        "      if (!silent) {\n"
        "        document.getElementById('threshold-status').textContent = `Thresholds: |effect| >= ${effectCutoff.toFixed(3)}, p <= ${pCutoff.toPrecision(3)} (-log10=${yCutoff.toFixed(3)}).`;\n"
        "      }\n"
        "    }\n"
        "    function populateSublibraryFilter() {\n"
        "      const sel = document.getElementById('sublibrary-filter');\n"
        "      sel.innerHTML = '';\n"
        "      const allOpt = document.createElement('option');\n"
        "      allOpt.value = '__ALL__';\n"
        "      allOpt.textContent = 'All sublibraries';\n"
        "      sel.appendChild(allOpt);\n"
        "      for (const name of (SUBLIBRARY_OPTIONS || [])) {\n"
        "        const opt = document.createElement('option');\n"
        "        opt.value = name;\n"
        "        opt.textContent = name;\n"
        "        sel.appendChild(opt);\n"
        "      }\n"
        "      sel.value = '__ALL__';\n"
        "    }\n"
        "    function getSelectedSublibrary() {\n"
        "      const sel = document.getElementById('sublibrary-filter');\n"
        "      return sel && sel.value ? sel.value : '__ALL__';\n"
        "    }\n"
        "    function filterArraysBySublibrary(xVals, yVals, textVals, customVals, subVals, selected) {\n"
        "      if (selected === '__ALL__') {\n"
        "        return {\n"
        "          x: (xVals || []).map((v) => Number(v)),\n"
        "          y: (yVals || []).map((v) => Number(v)),\n"
        "          text: (textVals || []).slice(),\n"
        "          customdata: (customVals || []).slice(),\n"
        "        };\n"
        "      }\n"
        "      const outX = [];\n"
        "      const outY = [];\n"
        "      const outText = [];\n"
        "      const outCustom = [];\n"
        "      const xs = xVals || [];\n"
        "      const ys = yVals || [];\n"
        "      const txt = textVals || [];\n"
        "      const custom = customVals || [];\n"
        "      const sub = subVals || [];\n"
        "      const n = Math.min(xs.length, ys.length, txt.length, custom.length, sub.length);\n"
        "      for (let i = 0; i < n; i += 1) {\n"
        "        if ((sub[i] || 'Unknown') !== selected) continue;\n"
        "        outX.push(Number(xs[i]));\n"
        "        outY.push(Number(ys[i]));\n"
        "        outText.push(txt[i]);\n"
        "        outCustom.push(custom[i]);\n"
        "      }\n"
        "      return { x: outX, y: outY, text: outText, customdata: outCustom };\n"
        "    }\n"
        "    function applyGeneSublibraryFilter() {\n"
        "      const selected = getSelectedSublibrary();\n"
        "      const useLimma = currentYMode === YMODE_LIMMA;\n"
        "      const genes = traces[TRACE_GENES];\n"
        "      const geneFiltered = filterArraysBySublibrary(\n"
        "        genes._x_all || [],\n"
        "        useLimma ? (genes._y_limma_all || []) : (genes._y_student_all || []),\n"
        "        genes._text_all || [],\n"
        "        genes._customdata_all || [],\n"
        "        genes._sublibrary_all || [],\n"
        "        selected\n"
        "      );\n"
        "      genes.x = geneFiltered.x;\n"
        "      genes.y = geneFiltered.y;\n"
        "      genes.text = geneFiltered.text;\n"
        "      genes.customdata = geneFiltered.customdata;\n"
        "      Plotly.restyle('volcano', {x: [genes.x], y: [genes.y], text: [genes.text], customdata: [genes.customdata]}, [TRACE_GENES]);\n"
        "      const hit = traces[TRACE_HITS];\n"
        "      const hitFiltered = filterArraysBySublibrary(\n"
        "        useLimma ? (hit._x_limma || []) : (hit._x_student || []),\n"
        "        useLimma ? (hit._y_limma || []) : (hit._y_student || []),\n"
        "        useLimma ? (hit._text_limma || []) : (hit._text_student || []),\n"
        "        useLimma ? (hit._customdata_limma || []) : (hit._customdata_student || []),\n"
        "        useLimma ? (hit._sublibrary_limma || []) : (hit._sublibrary_student || []),\n"
        "        selected\n"
        "      );\n"
        "      hit.x = hitFiltered.x;\n"
        "      hit.y = hitFiltered.y;\n"
        "      hit.text = hitFiltered.text;\n"
        "      hit.customdata = hitFiltered.customdata;\n"
        "      Plotly.restyle('volcano', {x: [hit.x], y: [hit.y], text: [hit.text], customdata: [hit.customdata]}, [TRACE_HITS]);\n"
        "      const totalGenes = (genes._x_all || []).length;\n"
        "      const shownGenes = genes.x.length;\n"
        "      const label = selected === '__ALL__' ? 'All sublibraries' : selected;\n"
        "      const status = selected === '__ALL__'\n"
        "        ? `${label}: ${shownGenes} gene points visible.`\n"
        "        : `${label}: ${shownGenes}/${totalGenes} gene points visible.`;\n"
        "      document.getElementById('sublibrary-status').textContent = status;\n"
        "      geneIndex = buildGeneIndex();\n"
        "      applyAllGeneHighlights();\n"
        "      clearTopLabels('');\n"
        "      applyVisibility();\n"
        "    }\n"
        "    function clearGeneInfo(message = 'Click a point to open external gene resources.') {\n"
        "      document.getElementById('gene-info-text').textContent = message;\n"
        "      const aNcbi = document.getElementById('gene-link-ncbi');\n"
        "      const aUni = document.getElementById('gene-link-uniprot');\n"
        "      aNcbi.style.display = 'none';\n"
        "      aUni.style.display = 'none';\n"
        "      aNcbi.removeAttribute('href');\n"
        "      aUni.removeAttribute('href');\n"
        "    }\n"
        "    function normalizeEntrez(value) {\n"
        "      if (value === null || value === undefined) return '';\n"
        "      const raw = String(value).trim();\n"
        "      if (!raw || raw.toLowerCase() === 'nan') return '';\n"
        "      const n = Number(raw);\n"
        "      if (Number.isFinite(n) && Math.floor(n) === n) return String(n);\n"
        "      return raw;\n"
        "    }\n"
        "    function pointSymbolFromClick(point) {\n"
        "      const symbolRaw = point && point.text ? String(point.text).trim() : '';\n"
        "      if (symbolRaw) return symbolRaw;\n"
        "      const cd = point && point.customdata;\n"
        "      if (Array.isArray(cd) && cd.length > 0) {\n"
        "        const aliasRaw = (cd[0] || '').toString();\n"
        "        const first = aliasRaw.split('|').map((k) => k.trim()).filter(Boolean)[0] || '';\n"
        "        return first;\n"
        "      }\n"
        "      return '';\n"
        "    }\n"
        "    function setGeneInfoLinks(symbol, entrez) {\n"
        "      const aNcbi = document.getElementById('gene-link-ncbi');\n"
        "      const aUni = document.getElementById('gene-link-uniprot');\n"
        "      const safeSymbol = (symbol || '').trim();\n"
        "      const safeEntrez = normalizeEntrez(entrez);\n"
        "      if (!safeSymbol && !safeEntrez) {\n"
        "        clearGeneInfo('No gene metadata available for this point.');\n"
        "        return;\n"
        "      }\n"
        "      let ncbiUrl = '';\n"
        "      if (safeEntrez) {\n"
        "        ncbiUrl = `https://www.ncbi.nlm.nih.gov/gene/${encodeURIComponent(safeEntrez)}`;\n"
        "      } else {\n"
        "        const q = `${safeSymbol}[Symbol] AND Homo sapiens[Organism]`;\n"
        "        ncbiUrl = `https://www.ncbi.nlm.nih.gov/gene/?term=${encodeURIComponent(q)}`;\n"
        "      }\n"
        "      aNcbi.href = ncbiUrl;\n"
        "      aNcbi.style.display = 'inline';\n"
        "      if (safeSymbol) {\n"
        "        const uq = `gene:${safeSymbol} AND organism_id:9606`;\n"
        "        aUni.href = `https://www.uniprot.org/uniprotkb?query=${encodeURIComponent(uq)}`;\n"
        "        aUni.style.display = 'inline';\n"
        "      } else {\n"
        "        aUni.style.display = 'none';\n"
        "        aUni.removeAttribute('href');\n"
        "      }\n"
        "      const label = safeSymbol || `Entrez ${safeEntrez}`;\n"
        "      document.getElementById('gene-info-text').textContent = `Gene info for ${label}`;\n"
        "    }\n"
        "    function onVolcanoClick(evt) {\n"
        "      if (!evt || !evt.points || evt.points.length === 0) {\n"
        "        clearGeneInfo();\n"
        "        return;\n"
        "      }\n"
        "      const point = evt.points[0];\n"
        "      const symbol = pointSymbolFromClick(point);\n"
        "      const cd = point.customdata;\n"
        "      const entrez = (Array.isArray(cd) && cd.length > 6) ? cd[6] : '';\n"
        "      setGeneInfoLinks(symbol, entrez);\n"
        "    }\n"
        "    function parseGeneInput(raw) {\n"
        "      if (!raw) return [];\n"
        "      const tokens = raw\n"
        "        .split(/[\\s,;]+/)\n"
        "        .map((t) => t.trim())\n"
        "        .filter(Boolean)\n"
        "        .map((t) => t.toUpperCase());\n"
        "      return [...new Set(tokens)];\n"
        "    }\n"
        "    function buildGeneIndex() {\n"
        "      const idx = new Map();\n"
        "      const absorb = (trace, categoryLabel) => {\n"
        "        const x = trace.x || [];\n"
        "        const y = trace.y || [];\n"
        "        const txt = trace.text || [];\n"
        "        const custom = trace.customdata || [];\n"
        "        for (let i = 0; i < x.length; i += 1) {\n"
        "          const symbolRaw = (txt[i] || '').toString().trim();\n"
        "          const pointCustom = custom[i];\n"
        "          const aliasRaw = Array.isArray(pointCustom) ? (pointCustom[0] || '') : (pointCustom || '');\n"
        "          const aliases = (aliasRaw.toString())\n"
        "            .split('|')\n"
        "            .map((k) => k.trim().toUpperCase())\n"
        "            .filter(Boolean);\n"
        "          if (aliases.length === 0 && symbolRaw) aliases.push(symbolRaw.toUpperCase());\n"
        "          if (aliases.length === 0) continue;\n"
        "          const rec = {\n"
        "            x: Number(x[i]),\n"
        "            y: Number(y[i]),\n"
        "            gene: symbolRaw || aliases[0],\n"
        "            category: categoryLabel,\n"
        "            plate: Array.isArray(pointCustom) ? (pointCustom[1] || 'NA') : 'NA',\n"
        "            coord: Array.isArray(pointCustom) ? (pointCustom[2] || 'NA') : 'NA',\n"
        "          };\n"
        "          for (const key of aliases) {\n"
        "            const prev = idx.get(key);\n"
        "            if (!prev || rec.y > prev.y) idx.set(key, rec);\n"
        "          }\n"
        "        }\n"
        "      };\n"
        "      absorb(traces[TRACE_GENES], 'Genes');\n"
        "      absorb(traces[TRACE_POS], 'Positive controls');\n"
        "      absorb(traces[TRACE_NEG], 'Negative controls');\n"
        "      return idx;\n"
        "    }\n"
        "    let geneIndex = buildGeneIndex();\n"
        "    const HIGHLIGHT_GROUPS = HIGHLIGHT_GROUP_DEFAULTS.map((group, idx) => ({\n"
        "      index: idx + 1,\n"
        "      inputId: `gene-input-${idx + 1}`,\n"
        "      colorId: `gene-color-${idx + 1}`,\n"
        "      sizeId: `gene-size-${idx + 1}`,\n"
        "      statusId: `gene-status-${idx + 1}`,\n"
        "      applyId: `gene-apply-${idx + 1}`,\n"
        "      clearId: `gene-clear-${idx + 1}`,\n"
        "      outerTrace: HIGHLIGHT_GROUP_TRACE_STARTS[idx],\n"
        "      centerTrace: HIGHLIGHT_GROUP_TRACE_STARTS[idx] + 1,\n"
        "      labelTrace: HIGHLIGHT_GROUP_TRACE_STARTS[idx] + 2,\n"
        "      defaultColor: group.color,\n"
        "    }));\n"
        "    function markerColorFor(trace, fallback) {\n"
        "      const marker = trace && trace.marker ? trace.marker : {};\n"
        "      const color = marker.color;\n"
        "      return (typeof color === 'string' && color) ? color : fallback;\n"
        "    }\n"
        "    function syncColorInputs() {\n"
        "      document.getElementById('color-genes').value = markerColorFor(traces[TRACE_GENES], DEFAULT_CATEGORY_COLORS.genes);\n"
        "      document.getElementById('color-pos').value = markerColorFor(traces[TRACE_POS], DEFAULT_CATEGORY_COLORS.pos);\n"
        "      document.getElementById('color-neg').value = markerColorFor(traces[TRACE_NEG], DEFAULT_CATEGORY_COLORS.neg);\n"
        "    }\n"
        "    function applyCategoryColors() {\n"
        "      const geneColor = document.getElementById('color-genes').value || DEFAULT_CATEGORY_COLORS.genes;\n"
        "      const posColor = document.getElementById('color-pos').value || DEFAULT_CATEGORY_COLORS.pos;\n"
        "      const negColor = document.getElementById('color-neg').value || DEFAULT_CATEGORY_COLORS.neg;\n"
        "      traces[TRACE_GENES].marker = traces[TRACE_GENES].marker || {};\n"
        "      traces[TRACE_HITS].marker = traces[TRACE_HITS].marker || {};\n"
        "      traces[TRACE_POS].marker = traces[TRACE_POS].marker || {};\n"
        "      traces[TRACE_NEG].marker = traces[TRACE_NEG].marker || {};\n"
        "      traces[TRACE_GENES].marker.color = geneColor;\n"
        "      traces[TRACE_HITS].marker.color = geneColor;\n"
        "      traces[TRACE_POS].marker.color = posColor;\n"
        "      traces[TRACE_NEG].marker.color = negColor;\n"
        "      Plotly.restyle('volcano', {'marker.color': [geneColor]}, [TRACE_GENES, TRACE_HITS]);\n"
        "      Plotly.restyle('volcano', {'marker.color': [posColor]}, [TRACE_POS]);\n"
        "      Plotly.restyle('volcano', {'marker.color': [negColor]}, [TRACE_NEG]);\n"
        "    }\n"
        "    function clearGeneHighlights(group, statusMessage = '') {\n"
        "      Plotly.restyle('volcano', {x: [[]], y: [[]], visible: [false]}, [group.outerTrace]);\n"
        "      Plotly.restyle('volcano', {x: [[]], y: [[]], text: [[]], visible: [false]}, [group.centerTrace]);\n"
        "      Plotly.restyle('volcano', {x: [[]], y: [[]], text: [[]], visible: [false]}, [group.labelTrace]);\n"
        "      document.getElementById(group.statusId).textContent = statusMessage;\n"
        "    }\n"
        "    function applyAllGeneHighlights() {\n"
        "      for (const group of HIGHLIGHT_GROUPS) applyGeneHighlights(group, true);\n"
        "    }\n"
        "    function applyYMode() {\n"
        "      currentYMode = document.getElementById('ymode-limma').checked ? YMODE_LIMMA : YMODE_STUDENT;\n"
        "      const useLimma = currentYMode === YMODE_LIMMA;\n"
        "      for (const idx of [TRACE_POS, TRACE_NEG]) {\n"
        "        const tr = traces[idx];\n"
        "        const yVals = useLimma ? (tr._y_limma || []) : (tr._y_student || []);\n"
        "        tr.y = yVals;\n"
        "        Plotly.restyle('volcano', {y: [yVals]}, [idx]);\n"
        "      }\n"
        "      Plotly.relayout('volcano', {'yaxis.title.text': useLimma ? Y_TITLE_LIMMA : Y_TITLE_STUDENT});\n"
        "      applyGeneSublibraryFilter();\n"
        "    }\n"
        "    function syncTopLabelVisibility() {\n"
        "      Plotly.restyle('volcano', {visible: [topLabelsActive]}, [TRACE_TOP_MARKERS]);\n"
        "      Plotly.restyle('volcano', {visible: [topLabelsActive]}, [TRACE_TOP_LABELS]);\n"
        "    }\n"
        "    function isTraceVisible(traceId) {\n"
        "      const trace = traces[traceId] || {};\n"
        "      return trace.visible !== false && trace.visible !== 'legendonly';\n"
        "    }\n"
        "    function traceDisplayColor(trace, fallback = '#111111') {\n"
        "      const marker = trace && trace.marker ? trace.marker : {};\n"
        "      const textfont = trace && trace.textfont ? trace.textfont : {};\n"
        "      if (typeof textfont.color === 'string' && textfont.color) return textfont.color;\n"
        "      if (typeof marker.color === 'string' && marker.color) return marker.color;\n"
        "      return fallback;\n"
        "    }\n"
        "    function buildPointsFromTrace(trace, categoryLabel = 'Genes') {\n"
        "      const xs = trace.x || [];\n"
        "      const ys = trace.y || [];\n"
        "      const txt = trace.text || [];\n"
        "      const custom = trace.customdata || [];\n"
        "      const pointColor = traceDisplayColor(trace);\n"
        "      const out = [];\n"
        "      for (let i = 0; i < xs.length; i += 1) {\n"
        "        const gene = (txt[i] || '').toString().trim();\n"
        "        if (!gene) continue;\n"
        "        const x = Number(xs[i]);\n"
        "        const y = Number(ys[i]);\n"
        "        if (!Number.isFinite(x) || !Number.isFinite(y)) continue;\n"
        "        const pointCustom = custom[i];\n"
        "        const indexed = geneIndex.get(gene.toUpperCase());\n"
        "        const plate = Array.isArray(pointCustom) ? (pointCustom[1] || 'NA') : ((indexed && indexed.plate) || 'NA');\n"
        "        const coord = Array.isArray(pointCustom) ? (pointCustom[2] || 'NA') : ((indexed && indexed.coord) || 'NA');\n"
        "        out.push({\n"
        "          gene,\n"
        "          x,\n"
        "          y,\n"
        "          plate,\n"
        "          coord,\n"
        "          color: pointColor,\n"
        "          category: (indexed && indexed.category) || categoryLabel,\n"
        "        });\n"
        "      }\n"
        "      return out;\n"
        "    }\n"
        "    function buildGenePointPool() {\n"
        "      const out = [];\n"
        "      if (isTraceVisible(TRACE_GENES)) out.push(...buildPointsFromTrace(traces[TRACE_GENES], 'Genes'));\n"
        "      if (isTraceVisible(TRACE_POS)) out.push(...buildPointsFromTrace(traces[TRACE_POS], 'Positive controls'));\n"
        "      if (isTraceVisible(TRACE_NEG)) out.push(...buildPointsFromTrace(traces[TRACE_NEG], 'Negative controls'));\n"
        "      for (const group of HIGHLIGHT_GROUPS) {\n"
        "        if (!isTraceVisible(group.labelTrace)) continue;\n"
        "        out.push(...buildPointsFromTrace(traces[group.labelTrace], `Highlight genes ${group.index}`));\n"
        "      }\n"
        "      return out;\n"
        "    }\n"
        "    function dedupeBest(points, scoreFn) {\n"
        "      const best = new Map();\n"
        "      for (const p of points) {\n"
        "        const key = p.gene.toUpperCase();\n"
        "        const score = scoreFn(p);\n"
        "        const prev = best.get(key);\n"
        "        if (!prev || score > prev.score) {\n"
        "          best.set(key, { point: p, score });\n"
        "        }\n"
        "      }\n"
        "      return Array.from(best.values()).map((v) => v.point);\n"
        "    }\n"
        "    function selectTopGenePoints(mode, n) {\n"
        "      const pool = buildGenePointPool();\n"
        "      if (!pool.length || n <= 0) return [];\n"
        "      const byAbsEffect = (a, b) => (Math.abs(b.x) - Math.abs(a.x)) || (b.y - a.y);\n"
        "      if (mode === 'effect_balanced') {\n"
        "        const uniq = dedupeBest(pool, (p) => Math.abs(p.x));\n"
        "        const pos = uniq.filter((p) => p.x >= 0).sort(byAbsEffect);\n"
        "        const neg = uniq.filter((p) => p.x < 0).sort(byAbsEffect);\n"
        "        const selected = [];\n"
        "        const each = Math.floor(n / 2);\n"
        "        const takeNeg = Math.min(each, neg.length);\n"
        "        const takePos = Math.min(each, pos.length);\n"
        "        selected.push(...neg.slice(0, takeNeg));\n"
        "        selected.push(...pos.slice(0, takePos));\n"
        "        let iNeg = takeNeg;\n"
        "        let iPos = takePos;\n"
        "        while (selected.length < n) {\n"
        "          const nextPos = iPos < pos.length ? pos[iPos] : null;\n"
        "          const nextNeg = iNeg < neg.length ? neg[iNeg] : null;\n"
        "          if (!nextPos && !nextNeg) break;\n"
        "          if (nextPos && (!nextNeg || Math.abs(nextPos.x) >= Math.abs(nextNeg.x))) {\n"
        "            selected.push(nextPos);\n"
        "            iPos += 1;\n"
        "          } else {\n"
        "            selected.push(nextNeg);\n"
        "            iNeg += 1;\n"
        "          }\n"
        "        }\n"
        "        return selected.slice(0, n);\n"
        "      }\n"
        "      if (mode === 'smallest_p') {\n"
        "        const uniq = dedupeBest(pool, (p) => p.y);\n"
        "        uniq.sort((a, b) => (b.y - a.y) || (Math.abs(b.x) - Math.abs(a.x)));\n"
        "        return uniq.slice(0, n);\n"
        "      }\n"
        "      const uniq = dedupeBest(pool, (p) => Math.abs(p.x) * p.y);\n"
        "      uniq.sort((a, b) => ((Math.abs(b.x) * b.y) - (Math.abs(a.x) * a.y)) || (b.y - a.y));\n"
        "      return uniq.slice(0, n);\n"
        "    }\n"
        "    function clearTopLabels(statusMessage = '') {\n"
        "      topLabelsActive = false;\n"
        "      Plotly.restyle('volcano', {x: [[]], y: [[]], text: [[]], customdata: [[]], visible: [false]}, [TRACE_TOP_MARKERS]);\n"
        "      Plotly.restyle('volcano', {x: [[]], y: [[]], text: [[]], visible: [false]}, [TRACE_TOP_LABELS]);\n"
        "      document.getElementById('top-status').textContent = statusMessage;\n"
        "    }\n"
        "    function applyTopLabels() {\n"
        "      const mode = document.getElementById('top-mode').value;\n"
        "      const n = Number(document.getElementById('top-n').value || 10);\n"
        "      const selected = selectTopGenePoints(mode, n);\n"
        "      const xVals = selected.map((p) => p.x);\n"
        "      const yVals = selected.map((p) => p.y);\n"
        "      const labels = selected.map((p) => p.gene);\n"
        "      const custom = selected.map((p) => [p.plate, p.coord]);\n"
        "      const colors = selected.map((p) => p.color || '#111111');\n"
        "      topLabelsActive = selected.length > 0;\n"
        "      Plotly.restyle('volcano', {x: [xVals], y: [yVals], text: [labels], customdata: [custom], 'marker.color': [colors], 'marker.line.color': [colors], visible: [topLabelsActive]}, [TRACE_TOP_MARKERS]);\n"
        "      Plotly.restyle('volcano', {x: [xVals], y: [yVals], text: [labels], 'textfont.color': [colors], visible: [topLabelsActive]}, [TRACE_TOP_LABELS]);\n"
        "      syncTopLabelVisibility();\n"
        "      const modeName = mode === 'effect_balanced'\n"
        "        ? 'largest |effect| (balanced +/-)'\n"
        "        : (mode === 'smallest_p' ? 'smallest p-value (active Y mode)' : 'strongest combo |effect| * -log10(p_active)');\n"
        "      document.getElementById('top-status').textContent = `Auto-labeled ${selected.length} gene(s) using ${modeName}.`;\n"
        "    }\n"
        "    function applyGeneHighlights(group, silent = false) {\n"
        "      const raw = document.getElementById(group.inputId).value;\n"
        "      const requested = parseGeneInput(raw);\n"
        "      if (requested.length === 0) {\n"
        "        clearGeneHighlights(group, '');\n"
        "        if (topLabelsActive) applyTopLabels();\n"
        "        return;\n"
        "      }\n"
        "      const found = [];\n"
        "      const missing = [];\n"
        "      for (const sym of requested) {\n"
        "        if (geneIndex.has(sym)) found.push(geneIndex.get(sym));\n"
        "        else missing.push(sym);\n"
        "      }\n"
        "      const xVals = found.map((r) => r.x);\n"
        "      const yVals = found.map((r) => r.y);\n"
        "      const centerText = found.map((r) => `${r.gene} (${r.category})`);\n"
        "      const labelText = found.map((r) => r.gene);\n"
        "      const hasPoints = found.length > 0;\n"
        "      const groupColor = document.getElementById(group.colorId).value || group.defaultColor;\n"
        "      const markerSize = PRIMARY_MARKER_SIZE * readHighlightScale(group);\n"
        "      Plotly.restyle('volcano', {x: [[]], y: [[]], visible: [false]}, [group.outerTrace]);\n"
        "      Plotly.restyle('volcano', {'x': [xVals], 'y': [yVals], 'text': [centerText], 'marker.color': [groupColor], 'marker.size': [markerSize], visible: [hasPoints]}, [group.centerTrace]);\n"
        "      Plotly.restyle('volcano', {'x': [xVals], 'y': [yVals], 'text': [labelText], 'textfont.color': [groupColor], visible: [hasPoints]}, [group.labelTrace]);\n"
        "      let msg = `Highlighted ${found.length} gene(s).`;\n"
        "      msg += ` Marker size: ${readHighlightScale(group)}x.`;\n"
        "      if (missing.length > 0) msg += ` Not found: ${missing.join(', ')}`;\n"
        "      if (!silent) document.getElementById(group.statusId).textContent = msg;\n"
        "      if (topLabelsActive) applyTopLabels();\n"
        "    }\n"
        "    function applyVisibility() {\n"
        "      const genesOn = document.getElementById('toggle-genes').checked;\n"
        "      const posOn = document.getElementById('toggle-pos').checked;\n"
        "      const negOn = document.getElementById('toggle-neg').checked;\n"
        "      Plotly.restyle('volcano', {visible: genesOn ? true : 'legendonly'}, [TRACE_GENES, TRACE_HITS]);\n"
        "      Plotly.restyle('volcano', {visible: posOn ? true : 'legendonly'}, [TRACE_POS]);\n"
        "      Plotly.restyle('volcano', {visible: negOn ? true : 'legendonly'}, [TRACE_NEG]);\n"
        "      if (topLabelsActive) applyTopLabels();\n"
        "      else syncTopLabelVisibility();\n"
        "    }\n"
        "    function getExportPreset() {\n"
        "      const mode = document.getElementById('export-quality').value;\n"
        "      if (mode === 'low') return { label: 'low', width: 1200, height: 800 };\n"
        "      if (mode === 'high') return { label: 'high', width: 2400, height: 1600 };\n"
        "      return { label: 'medium', width: 1800, height: 1200 };\n"
        "    }\n"
        "    function exportPublicationSvg() {\n"
        "      const preset = getExportPreset();\n"
        "      const yModeLabel = currentYMode === YMODE_LIMMA ? 'limma' : 'primary';\n"
        "      const filename = `candidate_volcano_${yModeLabel}_${preset.label}`;\n"
        "      document.getElementById('export-status').textContent = `Exporting ${preset.label} SVG...`;\n"
        "      Plotly.downloadImage('volcano', {\n"
        "        format: 'svg',\n"
        "        filename,\n"
        "        width: preset.width,\n"
        "        height: preset.height,\n"
        "        scale: 1,\n"
        "      }).then(() => {\n"
        "        document.getElementById('export-status').textContent = `Saved ${filename}.svg (${preset.width}x${preset.height}).`;\n"
        "      }).catch((err) => {\n"
        "        const msg = (err && err.message) ? err.message : 'Export failed.';\n"
        "        document.getElementById('export-status').textContent = msg;\n"
        "      });\n"
        "    }\n"
        "    document.getElementById('toggle-genes').addEventListener('change', applyVisibility);\n"
        "    document.getElementById('toggle-pos').addEventListener('change', applyVisibility);\n"
        "    document.getElementById('toggle-neg').addEventListener('change', applyVisibility);\n"
        "    document.getElementById('toggle-threshold-shading').addEventListener('change', () => updateThresholdControls());\n"
        "    document.getElementById('effect-cutoff').addEventListener('change', () => updateThresholdControls());\n"
        "    document.getElementById('p-cutoff').addEventListener('change', () => updateThresholdControls());\n"
        "    document.getElementById('color-genes').addEventListener('input', applyCategoryColors);\n"
        "    document.getElementById('color-pos').addEventListener('input', applyCategoryColors);\n"
        "    document.getElementById('color-neg').addEventListener('input', applyCategoryColors);\n"
        "    document.getElementById('sublibrary-filter').addEventListener('change', applyGeneSublibraryFilter);\n"
        "    document.getElementById('ymode-student').addEventListener('change', applyYMode);\n"
        "    document.getElementById('ymode-limma').addEventListener('change', applyYMode);\n"
        "    for (const group of HIGHLIGHT_GROUPS) {\n"
        "      document.getElementById(group.applyId).addEventListener('click', () => applyGeneHighlights(group));\n"
        "      document.getElementById(group.clearId).addEventListener('click', () => {\n"
        "        document.getElementById(group.inputId).value = '';\n"
        "        clearGeneHighlights(group, '');\n"
        "        if (topLabelsActive) applyTopLabels();\n"
        "      });\n"
        "      document.getElementById(group.inputId).addEventListener('keydown', (ev) => {\n"
        "        if (ev.key === 'Enter') {\n"
        "          ev.preventDefault();\n"
        "          applyGeneHighlights(group);\n"
        "        }\n"
        "      });\n"
        "      document.getElementById(group.colorId).addEventListener('input', () => applyGeneHighlights(group, true));\n"
        "      document.getElementById(group.sizeId).addEventListener('change', () => applyGeneHighlights(group, true));\n"
        "    }\n"
        "    document.getElementById('top-apply').addEventListener('click', applyTopLabels);\n"
        "    document.getElementById('top-clear').addEventListener('click', () => {\n"
        "      clearTopLabels('');\n"
        "    });\n"
        "    document.getElementById('export-svg').addEventListener('click', exportPublicationSvg);\n"
        "    volcanoDiv.on('plotly_click', onVolcanoClick);\n"
        "    clearGeneInfo();\n"
        "    populateSublibraryFilter();\n"
        "    syncColorInputs();\n"
        "    updateThresholdControls(true);\n"
        "    applyYMode();\n"
        "  </script>\n"
        "</body>\n"
        "</html>\n"
    )

    output_path = Path(output_html)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    if write_gzip_sidecar:
        gzip_path = output_path.with_suffix(output_path.suffix + ".gz")
        payload = html.encode("utf-8")
        with gzip_path.open("wb") as raw_fh:
            with gzip.GzipFile(fileobj=raw_fh, mode="wb", compresslevel=9, mtime=0) as gz_fh:
                gz_fh.write(payload)
    return output_path


def flashlight_plot(df: pd.DataFrame, rank_col: str = "Mean_log2"):
    keep = _candidate_mask(df)
    vals = pd.to_numeric(df.loc[keep, rank_col], errors="coerce")
    vals = vals.dropna()
    order = np.argsort(vals.to_numpy())
    fig, ax = plt.subplots(figsize=(7, 2.8))
    ax.plot(np.arange(len(order)), vals.to_numpy()[order], lw=1.0)
    ax.set_xlabel("Rank")
    ax.set_ylabel(rank_col)
    ax.set_title("Flashlight plot")
    return fig, ax
