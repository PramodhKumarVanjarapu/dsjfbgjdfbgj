"""Interview / round statistics for the company sheet (built on rounds.py).

analyse_tab() turns one company-sheet tab into ONE row per unique candidate (the row with the most
round information is kept, as in the round-funnel mode), attaches product category, bucket A-D and TR
info with the same core functions the TA / SA stats use, and classifies every round with
rounds.classify (Cleared / Rejected / Pending / No-show / Dropped / Other).
"""
from __future__ import annotations

import re

import pandas as pd

import core
import rounds

CAT_ORDER = ["Intensive", "Academy", "External"]
OFFER = "Hired / Offer"
CLEARED_ALL = "Cleared all rounds"
IN_PROCESS = "In process"
REJECTED = "Rejected"
NO_SHOW = "No-show / Dropped"
OTHER = "Other"
NOT_STARTED = "Not started"
_HIRED_RX = re.compile(r"hired|\boffer|joined|onboard")  # same rule as the stage mode


def label(col) -> str:
    """Column header with line breaks / repeated spaces collapsed."""
    return " ".join(str(col).split())


def _identity_mask(std: pd.DataFrame) -> pd.Series:
    return (std["Mobile"].map(core.norm_mobile).notna() | std["Email"].map(core.norm_email).notna()
            | std["User ID"].map(core.norm_text).notna())


def _in(mobiles: pd.Series, allowed) -> pd.Series:
    return mobiles.map(lambda v: isinstance(v, str) and v in allowed)


def _outcome(vals: list, hired: bool) -> str:
    if hired:
        return OFFER
    filled = [i for i, v in enumerate(vals) if v != ""]
    if not filled:
        return NOT_STARTED
    last = max(filled)
    s = vals[last]
    if s == "Rejected":
        return REJECTED
    if s in ("No-show", "Dropped"):
        return NO_SHOW
    if s == "Other":
        return OTHER
    if s == "Cleared":
        return CLEARED_ALL if last == len(vals) - 1 else IN_PROCESS  # next round not reported yet
    return IN_PROCESS  # Pending


# --------------------------------------------------------------------------- #
# Main analysis
# --------------------------------------------------------------------------- #
def analyse_tab(df, std, round_cols, STD, product_lookup, academy_kw, intensive_kw, pools, tr_sum,
                extra_pass=(), extra_fail=(), applied_mobiles=frozenset(), shortlisted_mobiles=frozenset()):
    """One company-sheet tab -> dict(cand, stages, raw, round_cols, ...) or None."""
    round_cols = list(round_cols)
    mask = _identity_mask(std)
    rows_without_id = int((~mask).sum())
    if not mask.any() or not round_cols:
        return None

    base = std[mask].reset_index(drop=True)
    raw_rows = df.loc[mask, round_cols].reset_index(drop=True)

    prepared = core.prepare_company(base, dict(STD), None, {}, None, None, academy_kw, intensive_kw)
    prepared = core.apply_products(prepared, product_lookup, academy_kw, intensive_kw)
    matched = core.match_company(prepared, pools, tr_sum)

    stages_all = pd.DataFrame(
        {c: raw_rows[c].map(lambda v: rounds.classify(v, extra_pass, extra_fail)) for c in round_cols},
        index=matched.index)

    # Hired / offered: a Cleared cell whose text says hired / offer / joined / onboard (any row of the person)
    hired_row = pd.Series(False, index=matched.index)
    for c in round_cols:
        text = raw_rows[c].fillna("").astype(str).str.strip().str.lower()
        hired_row |= (stages_all[c] == "Cleared") & text.str.contains(_HIRED_RX)
    hired_person = hired_row.groupby(matched["_person"]).any()

    # One row per candidate: keep the row with the most round information
    filled = (stages_all != "").sum(axis=1)
    order = filled.sort_values(ascending=False, kind="stable").index
    keep = matched.loc[order].drop_duplicates("_person").index.sort_values()

    cand = matched.loc[keep].reset_index(drop=True)
    stages = stages_all.loc[keep].reset_index(drop=True)
    raw = raw_rows.loc[keep].fillna("").astype(str).apply(lambda col: col.str.strip()).reset_index(drop=True)
    hired = cand["_person"].map(hired_person).fillna(False).astype(bool)

    cand["In TA"] = _in(cand["Mobile"], applied_mobiles).values
    cand["In SA"] = _in(cand["Mobile"], shortlisted_mobiles).values
    cand["Furthest cleared"] = rounds.furthest_cleared(stages, round_cols).values
    cand["Latest outcome"] = rounds.current_outcome(stages, round_cols).values
    cand["Outcome"] = [_outcome([row[c] for c in round_cols], bool(h))
                       for (_, row), h in zip(stages.iterrows(), hired)]

    return {"cand": cand, "stages": stages, "raw": raw, "round_cols": round_cols,
            "duplicates_merged": int(len(matched) - len(cand)), "rows_without_id": rows_without_id}


# --------------------------------------------------------------------------- #
# Headline + funnel
# --------------------------------------------------------------------------- #
def _interviewed(stages: pd.DataFrame) -> pd.Series:
    return stages.isin(["Cleared", "Rejected"]).any(axis=1)


def headline(res) -> dict:
    cand, stages = res["cand"], res["stages"]
    out = cand["Outcome"]
    return {
        "Candidates": int(len(cand)),
        "Shortlisted (in SA)": int(cand["In SA"].sum()),
        "Applied (in TA)": int(cand["In TA"].sum()),
        "Interviewed": int(_interviewed(stages).sum()),
        "Rejected": int(out.eq(REJECTED).sum()),
        "Hired / Offer": int(out.eq(OFFER).sum()),
        "Cleared all rounds": int(out.eq(CLEARED_ALL).sum()),
        "In process": int(out.eq(IN_PROCESS).sum()),
        "No-show / Dropped": int(out.eq(NO_SHOW).sum()),
    }


def round_funnel(res, only_shortlisted: bool = False) -> pd.DataFrame:
    """Per round: Reached, Cleared, Rejected, Pending, No-show, Dropped, Other + clear rates."""
    stages = res["stages"]
    if only_shortlisted:
        stages = stages[res["cand"]["In SA"].values]
    return rounds.funnel_table(stages, res["round_cols"], len(stages))


# --------------------------------------------------------------------------- #
# Breakdowns (columns = groups, rows = metrics)
# --------------------------------------------------------------------------- #
def product_groups(cand) -> dict:
    cat = cand["Category"].fillna("External")
    return {c: cat.eq(c) for c in CAT_ORDER}


def bucket_groups(cand) -> dict:
    """Academy candidates only: Bucket A, B, C, D, then Academy outside A-D."""
    academy = cand["Category"].fillna("External").eq("Academy")
    letters = cand["Bucket"].map(core.norm_bucket)
    in_pool = cand["In Bucket"].astype(bool)
    groups = {f"Bucket {b}": academy & letters.eq(b) for b in core.BUCKET_LETTERS}
    groups["Other bucket (not A-D)"] = academy & in_pool & ~letters.isin(core.BUCKET_LETTERS)
    groups["Not in bucket"] = academy & ~in_pool
    return groups


def _pct(n, d) -> float:
    return round(100 * n / d, 1) if d else 0.0


def _group_metrics(cand, stages, round_cols, mask) -> dict:
    c, s = cand[mask], stages[mask]
    n, out = len(c), c["Outcome"]
    m = {
        "Candidates": n,
        "In TA (applied)": int(c["In TA"].sum()),
        "In SA (shortlisted)": int(c["In SA"].sum()),
        "Bucket students": int(c["In Bucket"].sum()),
        "Assessment cleared": int(c["In Assessment Pool"].sum()),
        "TR cleared": int(c["TR Cleared"].sum()),
        "Interviewed": int(_interviewed(s).sum()) if n else 0,
        "Hired / Offer": int(out.eq(OFFER).sum()),
        "Cleared all rounds": int(out.eq(CLEARED_ALL).sum()),
        "In process": int(out.eq(IN_PROCESS).sum()),
        "Rejected": int(out.eq(REJECTED).sum()),
        "No-show / Dropped": int(out.eq(NO_SHOW).sum()),
        "Not started": int(out.eq(NOT_STARTED).sum()),
    }
    for rc in round_cols:
        m[f"{label(rc)} — cleared"] = int(s[rc].eq("Cleared").sum()) if n else 0
    m["Hired / Offer % of candidates"] = _pct(m["Hired / Offer"], n)
    return m


def breakdown(cand, stages, round_cols, groups: dict) -> pd.DataFrame:
    """Metrics as rows, one column per group (plus 'All' for the product split)."""
    cols, all_mask = {}, pd.Series(False, index=cand.index)
    for name, mask in groups.items():
        mask = mask.reindex(cand.index, fill_value=False).astype(bool)
        cols[name] = _group_metrics(cand, stages, round_cols, mask)
        all_mask |= mask
    if set(groups) == set(CAT_ORDER):
        cols["All"] = _group_metrics(cand, stages, round_cols, all_mask)
    table = pd.DataFrame(cols)
    table.index.name = "Metric"
    # Strings keep the table renderable (counts and % rows share a column).
    return table.apply(lambda col: [f"{v:,.1f}%" if "%" in idx else f"{int(v):,}"
                                    for idx, v in zip(table.index, col)])


# --------------------------------------------------------------------------- #
# Status mapping + master-sheet columns
# --------------------------------------------------------------------------- #
def status_mapping(df, round_cols, extra_pass=(), extra_fail=()) -> pd.DataFrame:
    """Every distinct value in each round column and how it was counted."""
    rows = []
    for c in round_cols:
        vc = df[c].dropna().astype(str).str.strip()
        vc = vc[vc != ""].value_counts()
        for val, cnt in vc.items():
            rows.append({"Round": label(c), "Value": val,
                         "Counted as": rounds.classify(val, extra_pass, extra_fail) or "(blank)", "Rows": int(cnt)})
    return pd.DataFrame(rows, columns=["Round", "Value", "Counted as", "Rows"])


def master_columns(title: str, res) -> dict:
    """Flat columns for the master summary row, prefixed with the company-sheet tab name."""
    p = f"Interview [{title}]"
    out = {f"{p} {k}": v for k, v in headline(res).items()}
    for _, r in round_funnel(res).iterrows():
        out[f"{p} {r['Round']} reached"] = int(r["Reached"])
        out[f"{p} {r['Round']} cleared"] = int(r["Cleared"])
    cand, stages, round_cols = res["cand"], res["stages"], res["round_cols"]
    for prefix, groups in (("Product ", product_groups(cand)), ("", bucket_groups(cand))):
        for name, mask in groups.items():
            m = _group_metrics(cand, stages, round_cols, mask.astype(bool))
            out[f"{p} {prefix}{name} candidates"] = m["Candidates"]
            out[f"{p} {prefix}{name} hired/offer"] = m["Hired / Offer"]
    return out