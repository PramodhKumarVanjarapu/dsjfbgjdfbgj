"""Interview / round statistics for the company sheet.

analyse_tab() turns one company-sheet tab into ONE row per unique candidate (matched by mobile,
then email / user id), attaches product category + bucket A-D + TR info with the same core
functions the TA / SA stats use, and works out each candidate's stage in every round.
The other functions build the tables shown on the "Interviews & rounds" tab.
"""
from __future__ import annotations

import pandas as pd

import core
import rounds
from rounds import BLANK, CLEARED, IN_PROCESS, NO_SHOW, OFFER, OTHER, PASSED, REJECTED

CAT_ORDER = ["Intensive", "Academy", "External"]
CLEARED_ALL = "Cleared all rounds"
NOT_STARTED = "Not started"
OUTCOMES = [OFFER, CLEARED_ALL, IN_PROCESS, REJECTED, NO_SHOW, OTHER, NOT_STARTED]


def label(col) -> str:
    """Column header with line breaks / repeated spaces collapsed."""
    return " ".join(str(col).split())


def _identity_mask(std: pd.DataFrame) -> pd.Series:
    return (std["Mobile"].map(core.norm_mobile).notna() | std["Email"].map(core.norm_email).notna()
            | std["User ID"].map(core.norm_text).notna())


def _mobile_in(mobiles, frozenset_) -> pd.Series:
    return mobiles.map(lambda v: isinstance(v, str) and v in frozenset_)


def _outcome(row: list, offer_seen: bool) -> str:
    filled = [i for i, v in enumerate(row) if v != BLANK]
    if offer_seen:
        return OFFER
    if not filled:
        return NOT_STARTED
    last = row[max(filled)]
    if last == REJECTED:
        return REJECTED
    if last == NO_SHOW:
        return NO_SHOW
    if last == OTHER:
        return OTHER
    if last == CLEARED:
        return CLEARED_ALL if max(filled) == len(row) - 1 else IN_PROCESS  # cleared, next round not reported yet
    return IN_PROCESS


# --------------------------------------------------------------------------- #
# Main analysis
# --------------------------------------------------------------------------- #
def analyse_tab(df, std, round_cols, STD, product_lookup, academy_kw, intensive_kw, pools, tr_sum,
                extra_pass=(), extra_fail=(), applied_mobiles=frozenset(), shortlisted_mobiles=frozenset()):
    """One company-sheet tab -> dict(cand, stages, raw, duplicates_merged, rows_without_id, round_cols) or None."""
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

    stage_rows = pd.DataFrame(
        {c: raw_rows[c].map(lambda v: rounds.classify(v, extra_pass, extra_fail)) for c in round_cols},
        index=matched.index)

    # One row per person: in every round keep the most progressed status among duplicate rows.
    person = matched["_person"]
    cand = core.unique_candidates(matched)
    first_pos = person.groupby(person, sort=False).head(1).index  # same group order as unique_candidates
    stages = pd.DataFrame(index=range(len(cand)), columns=round_cols, dtype=object)
    raw = pd.DataFrame(index=range(len(cand)), columns=round_cols, dtype=object)
    for c in round_cols:
        rank = stage_rows[c].map(rounds.STAGE_RANK)
        pick = rank.groupby(person, sort=False).idxmin()
        pick = pick.loc[person.loc[first_pos].values]
        stages[c] = stage_rows.loc[pick.values, c].values
        raw[c] = raw_rows.loc[pick.values, c].fillna("").astype(str).str.strip().values
    duplicates_merged = int(len(matched) - len(cand))

    cand["In TA"] = _mobile_in(cand["Mobile"], applied_mobiles).values
    cand["In SA"] = _mobile_in(cand["Mobile"], shortlisted_mobiles).values

    furthest, outcome = [], []
    for _, srow in stages.iterrows():
        vals = [srow[c] for c in round_cols]
        passed = [i for i, v in enumerate(vals) if v in PASSED]
        furthest.append(label(round_cols[max(passed)]) if passed else "None")
        outcome.append(_outcome(vals, any(v == OFFER for v in vals)))
    cand["Furthest cleared"] = furthest
    cand["Outcome"] = outcome

    return {"cand": cand, "stages": stages, "raw": raw, "round_cols": round_cols,
            "duplicates_merged": duplicates_merged, "rows_without_id": rows_without_id}


# --------------------------------------------------------------------------- #
# Headline + funnel
# --------------------------------------------------------------------------- #
def headline(res) -> dict:
    cand, stages = res["cand"], res["stages"]
    out = cand["Outcome"]
    interviewed = stages.apply(lambda col: col.isin([CLEARED, REJECTED, OFFER])).any(axis=1)
    return {
        "Candidates": int(len(cand)),
        "Shortlisted (in SA)": int(cand["In SA"].sum()),
        "Applied (in TA)": int(cand["In TA"].sum()),
        "Interviewed": int(interviewed.sum()),
        "Rejected": int(out.eq(REJECTED).sum()),
        "Hired / Offer": int(out.eq(OFFER).sum()),
        "Cleared all rounds": int(out.eq(CLEARED_ALL).sum()),
        "In process": int(out.eq(IN_PROCESS).sum()),
        "No-show / Dropped": int(out.eq(NO_SHOW).sum()),
    }


def _pct(n, d) -> float:
    return round(100 * n / d, 1) if d else 0.0


def round_funnel(res, only_shortlisted: bool = False) -> pd.DataFrame:
    """One row per round: how many entered, cleared, rejected ... and the conversion rates."""
    cand, stages, round_cols = res["cand"], res["stages"], res["round_cols"]
    keep = cand["In SA"].values if only_shortlisted else slice(None)
    st = stages[keep] if only_shortlisted else stages
    total = len(st)
    rows, prev_cleared = [], total
    for c in round_cols:
        s = st[c]
        entered = int((s != BLANK).sum())
        cleared = int(s.isin(PASSED).sum())
        rows.append({
            "Round": label(c),
            "Entered": entered,
            "Cleared": cleared,
            "Rejected": int(s.eq(REJECTED).sum()),
            "In process": int(s.eq(IN_PROCESS).sum()),
            "No-show / Dropped": int(s.eq(NO_SHOW).sum()),
            "Other": int(s.eq(OTHER).sum()),
            "Cleared % of entered": _pct(cleared, entered),
            "Cleared % of previous round": _pct(cleared, prev_cleared),
        })
        prev_cleared = cleared
    return pd.DataFrame(rows)


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


def _group_metrics(cand, stages, round_cols, mask) -> dict:
    c, s = cand[mask], stages[mask]
    n = len(c)
    out = c["Outcome"]
    m = {
        "Candidates": n,
        "In TA (applied)": int(c["In TA"].sum()),
        "In SA (shortlisted)": int(c["In SA"].sum()),
        "TR cleared": int(c["TR Cleared"].sum()),
        "Interviewed": int(s.apply(lambda col: col.isin([CLEARED, REJECTED, OFFER])).any(axis=1).sum()) if n else 0,
        "Hired / Offer": int(out.eq(OFFER).sum()),
        "Cleared all rounds": int(out.eq(CLEARED_ALL).sum()),
        "In process": int(out.eq(IN_PROCESS).sum()),
        "Rejected": int(out.eq(REJECTED).sum()),
        "No-show / Dropped": int(out.eq(NO_SHOW).sum()),
        "Not started": int(out.eq(NOT_STARTED).sum()),
    }
    for rc in round_cols:
        m[f"{label(rc)} — cleared"] = int(s[rc].isin(PASSED).sum()) if n else 0
    m["Hired / Offer % of candidates"] = _pct(m["Hired / Offer"], n)
    return m


def breakdown(cand, stages, round_cols, groups: dict) -> pd.DataFrame:
    """Metrics as rows, one column per group (plus 'All' for product groups, which add up to everything)."""
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
    """Every distinct value in the round columns and how it was counted."""
    seen: dict = {}
    for c in round_cols:
        for v in df[c]:
            key = rounds._flat(v)
            if not key:
                continue
            rec = seen.setdefault(key, {"Value": str(v).strip(), "Rows": 0, "Columns": set()})
            rec["Rows"] += 1
            rec["Columns"].add(label(c))
    rows = [{"Value": r["Value"], "Counted as": rounds.classify(r["Value"], extra_pass, extra_fail),
             "Rows": r["Rows"], "In columns": ", ".join(sorted(r["Columns"]))} for r in seen.values()]
    out = pd.DataFrame(rows, columns=["Value", "Counted as", "Rows", "In columns"])
    return out.sort_values(["Counted as", "Rows"], ascending=[True, False]).reset_index(drop=True)


def master_columns(title: str, res) -> dict:
    """Flat columns for the master summary sheet row, prefixed with the company-sheet tab name."""
    p = f"Interview [{title}]"
    out = {f"{p} {k}": v for k, v in headline(res).items()}
    for _, r in round_funnel(res).iterrows():
        out[f"{p} {r['Round']} entered"] = int(r["Entered"])
        out[f"{p} {r['Round']} cleared"] = int(r["Cleared"])
    cand, stages, round_cols = res["cand"], res["stages"], res["round_cols"]
    for prefix, groups in (("Product ", product_groups(cand)), ("", bucket_groups(cand))):
        for name, mask in groups.items():
            m = _group_metrics(cand, stages, round_cols, mask.astype(bool))
            out[f"{p} {prefix}{name} candidates"] = m["Candidates"]
            out[f"{p} {prefix}{name} hired/offer"] = m["Hired / Offer"]
    return out