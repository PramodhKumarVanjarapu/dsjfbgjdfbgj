"""Applicant analytics app (one Google Sheet in, all stats out).

Run with: streamlit run analytics_app.py

Input 1 - the job workbook (ONE Google Sheet link). Tabs are read like this:
  * TA tab   - the tab named "TA": total applications (unique candidates are counted)
  * SP tab   - the tab named "SP": shortlisted profiles
  * Product  - EVERY tab that has a Product column plus a mobile / user id / email column
               is used to map candidates to their product
Input 2 (optional) - the company sheet with interview details and rounds. Round columns are
detected automatically and the stats are split by product and by bucket A, B, C, D.

All stats are computed on UNIQUE candidates (one row per person).

Configuration (environment variables on Render, or .streamlit/secrets.toml locally):
    MASTER_SUMMARY_SHEET_URL   master summary sheet link
    APP_PASSWORD               shared password for the app
    GOOGLE_TOKEN_JSON          contents of token.json (see google_auth.py)
"""
from __future__ import annotations

import hmac
import os
import re
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import altair as alt
import gspread
import pandas as pd
import streamlit as st

import core
import interviews
import pool_view
import rounds
from google_auth import get_google_credentials

st.set_page_config(page_title="Applicant Analytics", page_icon="📊", layout="wide")

DEFAULT_ACADEMY_KEYWORDS = ["ACADEMY"]
DEFAULT_INTENSIVE_KEYWORDS = ["INTENSIVE"]
BUCKETS = list(core.BUCKET_LETTERS)

# Standard column names every tab is converted to before tabs are combined,
# so "Mobile" in one tab and "Phone Number" in another still line up.
STD = {
    "name": "Name", "mobile": "Mobile", "email": "Email", "uid": "User ID",
    "product": "Product", "resume": "Resume", "priority": "Priority",
}


# --------------------------------------------------------------------------- #
# Config / Google Sheets helpers
# --------------------------------------------------------------------------- #
def _secrets_section(name: str):
    try:
        return st.secrets.get(name, {})
    except Exception:
        return {}


def _sheet_config(key: str) -> str:
    """Environment variable first (Render), then .streamlit/secrets.toml [analytics] (local)."""
    env = os.environ.get(key.upper(), "").strip()   # e.g. MASTER_SUMMARY_SHEET_URL
    if env:
        return env
    return str(_secrets_section("analytics").get(key, "")).strip()


def _require_password():
    """Simple shared-password gate; on Render the app refuses to start without one."""
    expected = os.environ.get("APP_PASSWORD", "") or str(_secrets_section("auth").get("password", ""))
    if not expected:
        if os.environ.get("RENDER"):
            st.error("APP_PASSWORD is not set. Add it in the Render environment variables.")
            st.stop()
        return
    if st.session_state.get("_authed"):
        return
    pw = st.text_input("Password", type="password")
    if pw:
        if hmac.compare_digest(pw.encode(), expected.encode()):
            st.session_state["_authed"] = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()


def _worksheet_for_url(client, url: str):
    book = client.open_by_url(url.strip())
    gid_match = re.search(r"[?#&]gid=(\d+)", url)
    if gid_match:
        gid = int(gid_match.group(1))
        for ws in book.worksheets():
            if ws.id == gid:
                return ws
    return book.sheet1


def _worksheet_to_df(ws) -> pd.DataFrame:
    values = ws.get_all_values()
    if not values:
        return pd.DataFrame()
    # The header row is not always row 1 (titles/notes above it): use the first of the
    # top 10 rows that looks like a header (mentions mobile/phone/email/user id/resume/product).
    hdr = 0
    for i, r in enumerate(values[:10]):
        if any(k in core.norm_col(c) for c in r for k in ("mobile", "phone", "email", "userid", "resume", "product")):
            hdr = i
            break
    headers = [str(x).strip() or f"Unnamed: {i + 1}" for i, x in enumerate(values[hdr])]
    # Duplicate headers (e.g. two "Internship Company Name" columns) are made unique.
    seen: dict = {}
    unique = []
    for h in headers:
        seen[h] = seen.get(h, 0) + 1
        unique.append(h if seen[h] == 1 else f"{h} ({seen[h]})")
    width = len(unique)
    rows = [(r + [""] * width)[:width] for r in values[hdr + 1:]]
    df = pd.DataFrame(rows, columns=unique)
    blank = df.apply(lambda row: all(not str(v).strip() for v in row), axis=1)
    return df.loc[~blank].reset_index(drop=True)


def _find_job_col(df: pd.DataFrame):
    return core.find_col(df, ["jobid", "jdid", "jobcode", "requisitionid"])


# --------------------------------------------------------------------------- #
# Tab loading
# --------------------------------------------------------------------------- #
def _standardise(df: pd.DataFrame, tab: str) -> pd.DataFrame:
    """Rename whatever columns a tab uses to the standard schema."""
    det = core.detect_columns(df)
    out = pd.DataFrame(index=df.index)
    for key, std in STD.items():
        out[std] = df[det[key]] if det.get(key) else ""
    # Some exports (e.g. the shortlisted tab) label the candidate's 10-digit mobile number
    # as "UUID"/"UID". If there is no mobile column and that column is (almost) all phone
    # numbers, treat it as the mobile number.
    if not det.get("mobile") and det.get("uid") and len(df):
        uid_vals = df[det["uid"]]
        if uid_vals.map(lambda v: core.norm_mobile(v) is not None).mean() >= 0.8:
            out["Mobile"] = uid_vals
            out["User ID"] = ""
    job_col = _find_job_col(df)
    out["Job ID"] = df[job_col] if job_col else ""
    out["_Source Tab"] = tab
    return out


def _has_value(series: pd.Series) -> pd.Series:
    return ~series.map(core._is_junk)


def _identity_mask(std: pd.DataFrame) -> pd.Series:
    return (std["Mobile"].map(core.norm_mobile).notna() | std["Email"].map(core.norm_email).notna()
            | std["User ID"].map(core.norm_text).notna())


def _name_keys(text: str) -> set:
    return {core.norm_col(x) for x in text.split(",") if core.norm_col(x)}


def load_workbook(client, url: str, ta_names: str = "TA", sa_names: str = "SP", require_resume: bool = True) -> dict:
    """Read every tab of the job workbook: TA = applications, SP = shortlisted, any tab with a Product column = product mapping."""
    if not url or "docs.google.com/spreadsheets" not in url:
        raise ValueError("Enter a valid Google Sheets URL.")
    book = client.open_by_url(url.strip())
    ta_keys, sa_keys = _name_keys(ta_names), _name_keys(sa_names)

    apps, shorts, products = [], [], []
    ta_tabs, sa_tabs, product_tabs, skipped, all_titles = [], [], [], [], []

    for ws in book.worksheets():
        all_titles.append(ws.title)
        df = _worksheet_to_df(ws)
        if df.empty:
            skipped.append(f"{ws.title} (empty)")
            continue
        det = core.detect_columns(df)
        key = core.norm_col(ws.title)
        is_ta, is_sa = key in ta_keys, key in sa_keys
        std = _standardise(df, ws.title) if (is_ta or is_sa or det.get("product")) else None

        # ---- product mapping: EVERY tab with a Product column + an identity column ----
        if std is not None and det.get("product"):
            pf = std[_has_value(std["Product"]) & _identity_mask(std)]
            if len(pf):
                products.append(pf)
                product_tabs.append(f"{ws.title} ({len(pf):,} rows with a product)")

        # ---- TA: total applications ----
        if is_ta:
            valid = std["Mobile"].map(core.norm_mobile).notna()
            if require_resume and det.get("resume"):
                valid &= _has_value(std["Resume"])
            ta = std[valid]
            if ta.empty:
                skipped.append(f"{ws.title} (TA tab, but no rows with a valid mobile" +
                               (" and resume link" if require_resume and det.get("resume") else "") + ")")
            else:
                apps.append(ta)
                ta_tabs.append(f"{ws.title} ({len(ta):,} of {len(df):,} rows)")

        # ---- SP: shortlisted profiles ----
        elif is_sa:
            sa = std[_identity_mask(std)]
            if sa.empty:
                skipped.append(f"{ws.title} (SP tab, but no mobile / email / user id found; "
                               f"columns seen: {list(df.columns)[:15]})")
            else:
                shorts.append(sa)
                sa_tabs.append(f"{ws.title} ({len(sa):,} rows)")

        elif not (std is not None and det.get("product")):
            skipped.append(f"{ws.title} (not TA / SP and no Product column)")

    if not apps:
        raise ValueError(f"No tab named '{ta_names}' with usable application rows was found. "
                         f"Tabs in this workbook: {all_titles}")

    return {
        "apps": pd.concat(apps, ignore_index=True),
        "shorts": pd.concat(shorts, ignore_index=True) if shorts else pd.DataFrame(),
        "products": pd.concat(products, ignore_index=True, sort=False) if products else pd.DataFrame(),
        "ta_tabs": ta_tabs, "sa_tabs": sa_tabs, "product_tabs": product_tabs, "skipped": skipped,
    }


def load_company_sheet(client, url: str, only_linked_tab: bool = False) -> dict:
    """Company sheet with interview details: every tab that has a candidate identity AND round columns."""
    if not url or "docs.google.com/spreadsheets" not in url:
        raise ValueError("Enter a valid Google Sheets URL for the company sheet.")
    book = client.open_by_url(url.strip())
    gid = re.search(r"[?#&]gid=(\d+)", url)
    tabs, skipped = [], []
    for ws in book.worksheets():
        if only_linked_tab and gid and ws.id != int(gid.group(1)):
            continue
        df = _worksheet_to_df(ws)
        if df.empty:
            skipped.append(f"{ws.title} (empty)")
            continue
        std = _standardise(df, ws.title)
        if not _identity_mask(std).any():
            skipped.append(f"{ws.title} (no mobile / email / user id column)")
            continue
        round_cols = rounds.detect_round_columns(df)
        if not round_cols:
            skipped.append(f"{ws.title} (no round / status columns)")
            continue
        tabs.append({"title": ws.title, "df": df, "std": std, "rounds": round_cols})
    if not tabs:
        raise ValueError("No tab in the company sheet has both candidate contact columns and round columns. "
                         + (f"Skipped: {skipped}" if skipped else ""))
    return {"tabs": tabs, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Matching + stats pipeline
# --------------------------------------------------------------------------- #
def _load_local_pools() -> dict:
    """Bucket (offline attended pool) and TR (assessment pool) lookup CSVs."""
    def _find(name: str) -> Path:
        # data/ folder (local or private repo), or Render "Secret Files" (/etc/secrets)
        for folder in (Path(__file__).parent / "data", Path("/etc/secrets")):
            if (folder / name).exists():
                return folder / name
        return Path(__file__).parent / "data" / name

    bucket_path = _find("offline_attended_pool.csv")
    tr_path = _find("assessment_pool_main.csv")
    missing = [str(p) for p in (bucket_path, tr_path) if not p.exists()]
    if missing:
        raise FileNotFoundError("Missing lookup CSV file(s): " + ", ".join(missing))
    return core.build_pools(core.read_csv_bytes(bucket_path.read_bytes()),
                            core.read_csv_bytes(tr_path.read_bytes()))


def _process(std_df: pd.DataFrame, product_lookup, academy_kw, intensive_kw, pools, tr_sum):
    """Standardised rows -> (row-level matched frame, unique-candidate frame)."""
    if std_df.empty:
        return pd.DataFrame(), pd.DataFrame()
    prepared = core.prepare_company(std_df, dict(STD), None, {}, None, None, academy_kw, intensive_kw)
    prepared = core.apply_products(prepared, product_lookup, academy_kw, intensive_kw)
    matched = core.match_company(prepared, pools, tr_sum)
    return matched, core.unique_candidates(matched)


def _mobile_set(df: pd.DataFrame) -> frozenset:
    if df is None or df.empty or "Mobile" not in df:
        return frozenset()
    return frozenset(v for v in df["Mobile"] if isinstance(v, str))


def _append_summary(client, master_url: str, row: dict):
    if not master_url:
        raise RuntimeError("Missing MASTER_SUMMARY_SHEET_URL.")
    ws = _worksheet_for_url(client, master_url)
    existing = ws.get_all_values()
    headers = list(row.keys())
    if not existing:
        ws.append_row(headers, value_input_option="USER_ENTERED")
    else:
        existing_headers = [str(x).strip() for x in existing[0]]
        missing = [h for h in headers if h not in existing_headers]
        if missing:
            existing_headers.extend(missing)
            ws.update(range_name="1:1", values=[existing_headers])
        headers = existing_headers
    ws.append_row([row.get(h, "") for h in headers], value_input_option="USER_ENTERED")


def _summary_row(result: dict, tr_rule: str) -> dict:
    app_u, short_u = result["apps_unique"], result["short_unique"]
    row = {
        "Run timestamp (IST)": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds"),
        "Job ID": result["job_id"],
        "Sheet URL": result["sheet_url"],
        "TR cleared rule": tr_rule,
        "Application rows": len(result["apps_matched"]),
        "Shortlist rows": len(result["short_matched"]),
    }
    for prefix, cand in (("Applications", app_u), ("Shortlist", short_u)):
        s = core.candidate_stats(cand, prefix)
        for key, val in s.items():
            if key != "Dataset":
                row[f"{prefix} {key}"] = val
        bt = core.bucket_table(cand).set_index("Bucket")
        for bucket in BUCKETS:
            row[f"{prefix} Academy Bucket {bucket}"] = int(bt.at[bucket, "Candidates"])
            row[f"{prefix} Academy Bucket {bucket} TR cleared"] = int(bt.at[bucket, "TR cleared"])
    if result.get("company_url"):
        row["Company sheet URL"] = result["company_url"]
    for title, res in result.get("company_results", {}).items():
        row.update(interviews.master_columns(title, res))
    return row


# --------------------------------------------------------------------------- #
# Interviews & rounds tab
# --------------------------------------------------------------------------- #
def _render_interviews(result, pools, tr_sum, academy_kw, intensive_kw, extra_pass, extra_fail):
    tabs = result.get("company_tabs")
    if not tabs:
        st.info("Paste the company sheet link (interview details and rounds) in the form above and run again "
                "to see round-wise stats here.")
        return
    if result.get("company_skipped"):
        st.caption("Company sheet tabs skipped: " + "; ".join(result["company_skipped"]))

    names = [t["title"] for t in tabs]
    pick = st.selectbox("Company sheet tab", names, key="iv_tab")
    tb = tabs[names.index(pick)]
    round_cols = st.multiselect(
        "Rounds in funnel order (auto-detected — add, remove or re-order)", list(tb["df"].columns),
        default=tb["rounds"], format_func=interviews.label, key=f"iv_rounds_{pick}")
    if not round_cols:
        st.warning("No round columns selected. Pick them in the box above.")
        return

    res = interviews.analyse_tab(
        tb["df"], tb["std"], round_cols, dict(STD), result["product_lookup"], academy_kw, intensive_kw,
        pools, tr_sum, extra_pass, extra_fail,
        applied_mobiles=_mobile_set(result["apps_unique"]), shortlisted_mobiles=_mobile_set(result["short_unique"]))
    if res is None:
        st.error("No candidate in this tab has a usable mobile / email / user id.")
        return
    cand, stages = res["cand"], res["stages"]

    h = interviews.headline(res)
    r1 = st.columns(4)
    for col, key in zip(r1, ["Candidates", "Shortlisted (in SP)", "Applied (in TA)", "Interviewed"]):
        col.metric(key, f"{h[key]:,}")
    r2 = st.columns(5)
    for col, key in zip(r2, ["Rejected", "Hired / Offer", "Cleared all rounds", "In process", "No-show / Dropped"]):
        col.metric(key, f"{h[key]:,}")
    if res["duplicates_merged"]:
        st.caption(f"{res['duplicates_merged']} duplicate rows merged (same mobile / email).")

    f_tab, p_tab, b_tab, pool_tab, c_tab, s_tab, q_tab = st.tabs(
        ["Round funnel", "By product", "By bucket (A–D)", "Pool coverage", "Candidates", "Status mapping",
         "Data checks"])

    with f_tab:
        funnel = interviews.round_funnel(res)
        st.markdown("**All candidates in the company sheet**")
        st.dataframe(funnel, hide_index=True, use_container_width=True)
        if len(funnel):
            chart_df = pd.concat([pd.DataFrame({"Round": ["Candidates"], "Cleared": [h["Candidates"]]}),
                                  funnel[["Round", "Cleared"]]])
            bars = alt.Chart(chart_df).mark_bar().encode(
                y=alt.Y("Round:N", sort=None, title=None), x=alt.X("Cleared:Q", title="Candidates cleared"),
                tooltip=["Round", "Cleared"]).properties(height=40 * len(chart_df) + 20)
            st.altair_chart(bars + bars.mark_text(align="left", dx=4).encode(text="Cleared:Q"),
                            use_container_width=True)
        st.caption("Reached = has any status in the round. Cleared % of total is out of all candidates in the sheet.")
        if h["Shortlisted (in SP)"]:
            st.markdown("**Only shortlisted candidates (in SP)**")
            st.dataframe(interviews.round_funnel(res, only_shortlisted=True), hide_index=True,
                         use_container_width=True)
        else:
            st.caption("None of the company-sheet candidates matched the SP (shortlisted) tab by mobile.")

    with p_tab:
        st.markdown("**By product: who is in the company sheet, outcomes, then each round cleared**")
        st.dataframe(interviews.breakdown(cand, stages, round_cols, interviews.product_groups(cand)),
                     use_container_width=True)

    with b_tab:
        st.caption("Buckets A–D apply to Academy candidates only; the last two columns hold Academy candidates "
                   "outside A–D.")
        st.dataframe(interviews.breakdown(cand, stages, round_cols, interviews.bucket_groups(cand)),
                     use_container_width=True)

    with pool_tab:
        pool_view.render_pool_metrics(pools, tr_sum, cand)
        st.caption("How much of the placement pool is in this company sheet, and from which product.")
        st.dataframe(pool_view.pool_coverage(pools, tr_sum, tr_rule, cand), hide_index=True,
                     use_container_width=True)

    with c_tab:
        view = pd.DataFrame({
            "Name": cand["Name"], "Mobile": cand["Mobile"], "Category": cand["Category"],
            "Bucket": cand["Bucket"], "TR Cleared": cand["TR Cleared"], "In TA": cand["In TA"], "In SP": cand["In SP"],
            **{interviews.label(c): res["raw"][c] for c in round_cols},
            "Furthest cleared": cand["Furthest cleared"], "Latest outcome": cand["Latest outcome"],
            "Outcome": cand["Outcome"],
        })
        f1, f2, f3 = st.columns(3)
        sel_cat = f1.multiselect("Category", interviews.CAT_ORDER, default=interviews.CAT_ORDER, key="iv_cat")
        only_sa = f2.checkbox("Shortlisted (SP) only", key="iv_sa")
        search = f3.text_input("Search name / mobile", key="iv_search")
        v = view[view["Category"].isin(sel_cat)]
        if only_sa:
            v = v[v["In SP"]]
        if search:
            s = search.strip().lower()
            v = v[v["Name"].fillna("").str.lower().str.contains(s, regex=False)
                  | v["Mobile"].fillna("").str.contains(s, regex=False)]
        st.caption(f"{len(v):,} candidates")
        st.dataframe(v, hide_index=True, use_container_width=True)
        st.download_button("⬇️ Download these candidates (CSV)", v.to_csv(index=False).encode("utf-8"),
                           file_name="company_sheet_candidates.csv", mime="text/csv", key="iv_dl")

    with s_tab:
        st.caption("Every value found in the round columns and how it was counted. Fix wrong ones with the "
                   "extra status words in the sidebar.")
        sm = interviews.status_mapping(tb["df"], round_cols, extra_pass, extra_fail)
        st.dataframe(sm, hide_index=True, use_container_width=True)
        other = sm[sm["Counted as"] == "Other"]
        if len(other):
            st.warning(f"{len(other)} value(s) weren't recognised and are counted as 'Other'.")

    with q_tab:
        ooo = rounds.out_of_order(stages, round_cols)
        st.table(pd.DataFrame({
            "Check": ["Rows without a usable mobile / email / user id", "Duplicate rows merged",
                      "Candidates in the company sheet but not in TA (never applied)",
                      "Candidates with a later-round status while an earlier round isn't Cleared"],
            "Count": [res["rows_without_id"], res["duplicates_merged"], int((~cand["In TA"]).sum()), int(ooo.sum())],
        }))
        st.markdown("**Where the product came from**")
        st.dataframe(cand["Product Source"].value_counts().rename_axis("Source").reset_index(name="Candidates"),
                     hide_index=True, use_container_width=True)
        st.caption("Columns not used as rounds: " +
                   ", ".join(interviews.label(c) for c in tb["df"].columns if c not in round_cols))


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
st.title("📊 Applicant Analytics — Single Job")
st.caption("Paste the job workbook link (tabs TA and SP are read automatically; products come from every tab that has "
           "a Product column). Optionally add the company sheet for interview and round stats.")

with st.sidebar:
    st.header("Analytics rules")
    academy_text = st.text_input("Academy product keyword(s)", "ACADEMY")
    intensive_text = st.text_input("Intensive product keyword(s)", "INTENSIVE")
    tr_rule = st.selectbox("TR Cleared rule", core.TR_RULES, index=0)
    cleared_values = st.multiselect("Statuses counted as cleared",
                                    ["cleared", "clear", "passed", "selected"], default=["cleared"])
    st.header("Tab names")
    ta_text = st.text_input("Total applications tab", "TA", help="Exact tab name (case and spaces ignored). "
                                                                 "Comma-separate to allow several names.")
    sa_text = st.text_input("Shortlisted profiles tab", "SP")
    require_resume = st.checkbox("Count only applications with a resume link", value=True,
                                 help="Applies when the TA tab has a resume column.")
    st.header("Interview rounds")
    extra_pass = [w.strip().lower() for w in st.text_input("Extra words counted as Cleared", "").split(",") if w.strip()]
    extra_fail = [w.strip().lower() for w in st.text_input("Extra words counted as Rejected", "").split(",") if w.strip()]

_require_password()

if not _sheet_config("master_summary_sheet_url"):
    st.error("Set the MASTER_SUMMARY_SHEET_URL environment variable (or `analytics.master_summary_sheet_url` "
             "in `.streamlit/secrets.toml` when running locally) first.")
    st.stop()

try:
    client = gspread.authorize(get_google_credentials())
except Exception as exc:
    st.error(f"Couldn't initialise Google Sheets credentials: {exc}")
    st.stop()

with st.spinner("Loading the bucket and TR CSV lookup files…"):
    try:
        pools = _load_local_pools()
    except Exception as exc:
        st.error(f"Couldn't load bucket/TR CSV files: {exc}")
        st.stop()

academy_kw = [x.strip().upper() for x in academy_text.split(",") if x.strip()] or DEFAULT_ACADEMY_KEYWORDS
intensive_kw = [x.strip().upper() for x in intensive_text.split(",") if x.strip()] or DEFAULT_INTENSIVE_KEYWORDS
tr_sum = core.tr_summary(pools, tr_rule, [x.strip().lower() for x in cleared_values])

with st.form("job_inputs"):
    st.subheader("Input sheets")
    sheet_url = st.text_input("Job workbook link (tabs TA, SP and product details)",
                              placeholder="Paste the Google Sheet URL")
    company_url = st.text_input("Company sheet link — interview details & rounds (optional)",
                                placeholder="Paste the company's tracking sheet URL")
    only_linked = st.checkbox("Company sheet: read only the tab in the link (gid)", value=False)
    job_id = st.text_input("Job ID (optional)", placeholder="Leave blank to read it from the sheet")
    run_analysis = st.form_submit_button("Fetch sheets and calculate stats", type="primary")

if run_analysis:
    if not sheet_url.strip():
        st.error("Please paste the job workbook link.")
        st.stop()
    try:
        with st.spinner("Reading all tabs…"):
            wb = load_workbook(client, sheet_url, ta_text, sa_text, require_resume)

        job_ids = list(dict.fromkeys(str(x).strip() for x in wb["apps"]["Job ID"] if str(x).strip()))
        resolved_job = job_id.strip()
        if not resolved_job:
            if len(job_ids) == 1:
                resolved_job = job_ids[0]
            elif len(job_ids) > 1:
                st.warning(f"Found {len(job_ids)} different Job IDs in the sheet. "
                           "Enter the intended Job ID above and run again.")
                st.stop()

        # Product for every candidate comes from ALL tabs that have a Product column.
        product_lookup = {}
        if not wb["products"].empty:
            product_lookup = core.build_product_lookup(wb["products"], academy_kw, intensive_kw,
                                                       source_col="_Source Tab")

        apps_matched, apps_unique = _process(wb["apps"], product_lookup, academy_kw, intensive_kw, pools, tr_sum)
        short_matched, short_unique = _process(wb["shorts"], product_lookup, academy_kw, intensive_kw, pools, tr_sum)

        company_tabs, company_skipped, company_results = None, [], {}
        if company_url.strip():
            with st.spinner("Reading the company sheet…"):
                cs = load_company_sheet(client, company_url, only_linked)
            company_tabs, company_skipped = cs["tabs"], cs["skipped"]
            for t in company_tabs:  # default rounds, used for the master summary row
                r = interviews.analyse_tab(
                    t["df"], t["std"], t["rounds"], dict(STD), product_lookup, academy_kw, intensive_kw,
                    pools, tr_sum, extra_pass, extra_fail,
                    applied_mobiles=_mobile_set(apps_unique), shortlisted_mobiles=_mobile_set(short_unique))
                if r is not None:
                    company_results[t["title"]] = r

        st.session_state["analytics_result"] = {
            "job_id": resolved_job, "sheet_url": sheet_url.strip(), "company_url": company_url.strip(),
            "ta_tabs": wb["ta_tabs"], "sa_tabs": wb["sa_tabs"], "product_tabs": wb["product_tabs"],
            "skipped": wb["skipped"], "product_lookup": product_lookup,
            "apps_matched": apps_matched, "short_matched": short_matched,
            "apps_unique": apps_unique, "short_unique": short_unique,
            "company_tabs": company_tabs, "company_skipped": company_skipped, "company_results": company_results,
        }
    except Exception as exc:
        st.error(f"Couldn't process the sheet: {type(exc).__name__}: {exc}")
        with st.expander("Detailed error information", expanded=True):
            st.code(traceback.format_exc(), language="text")
        st.info("Check that the sheet URLs are correct, the signed-in Google account has access, "
                "and the expected tabs/columns are present.")
        st.stop()

result = st.session_state.get("analytics_result")
if result:
    app_u, short_u = result["apps_unique"], result["short_unique"]
    st.divider()
    st.subheader(f"Results · Job ID: {result['job_id'] or 'Not provided'}")

    if not result["sa_tabs"]:
        st.warning(f"No usable tab named '{sa_text}' was found, so shortlist numbers are zero.")
    if not result["product_tabs"]:
        st.warning("No tab with a Product column was found. Everyone is counted as External.")

    st.markdown("### Overall summary (unique candidates)")
    st.dataframe(pd.DataFrame([
        core.candidate_stats(app_u, "Total applications (TA)"),
        core.candidate_stats(short_u, "Shortlisted profiles (SP)"),
    ]), use_container_width=True, hide_index=True)

    t1, t2, t3, t4, t5 = st.tabs(["Product-wise", "Bucket-wise (A–D)", "Candidate details", "Data checks",
                                  "Interviews & rounds"])

    with t1:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Total applications — product-wise**")
            st.dataframe(core.product_table(app_u), use_container_width=True, hide_index=True)
        with c2:
            st.markdown("**Shortlisted profiles — product-wise**")
            st.dataframe(core.product_table(short_u), use_container_width=True, hide_index=True)

    with t2:
        st.caption("Buckets A–D apply to Academy candidates only. Each table adds up to the Academy total; "
                   "candidates outside A–D appear in the last two rows.")
        funnel = core.bucket_funnel(app_u, short_u)
        cols = st.columns(4)
        for col, bucket in zip(cols, BUCKETS):
            r = funnel[funnel["Bucket"] == bucket].iloc[0]
            col.metric(f"Bucket {bucket}", f"{int(r['Applied']):,} applied",
                       f"{int(r['Shortlisted']):,} shortlisted · {r['Shortlist rate %']}%", delta_color="off")
        st.markdown("**Bucket funnel — applied vs shortlisted**")
        st.dataframe(funnel, use_container_width=True, hide_index=True)
        b1, b2 = st.columns(2)
        with b1:
            st.markdown("**Total applications — Academy buckets**")
            st.dataframe(core.bucket_table(app_u), use_container_width=True, hide_index=True)
        with b2:
            st.markdown("**Shortlisted profiles — Academy buckets**")
            st.dataframe(core.bucket_table(short_u), use_container_width=True, hide_index=True)

    with t3:
        st.markdown("**Total applications (unique candidates)**")
        st.dataframe(app_u, use_container_width=True, hide_index=True)
        st.markdown("**Shortlisted profiles (unique candidates)**")
        st.dataframe(short_u, use_container_width=True, hide_index=True)

    with t4:
        st.markdown("**Tab used as total applications (TA)**")
        st.write(result["ta_tabs"] or "None")
        st.markdown("**Tab used as shortlisted (SP)**")
        st.write(result["sa_tabs"] or "None")
        st.markdown("**Tabs used for product mapping** (any tab with a Product column)")
        st.write(result["product_tabs"] or "None")
        if result["skipped"]:
            st.markdown("**Tabs skipped**")
            st.write(result["skipped"])
        st.markdown("**Row counts before de-duplication**")
        st.write({"Application rows": len(result["apps_matched"]), "Unique applicants": len(app_u),
                  "Shortlist rows": len(result["short_matched"]), "Unique shortlisted": len(short_u)})
        for label, m in (("Applications", result["apps_matched"]), ("Shortlist", result["short_matched"])):
            if not m.empty:
                st.markdown(f"**{label} — where the product came from**")
                st.dataframe(m["Product Source"].value_counts().rename_axis("Source").reset_index(name="Rows"),
                             use_container_width=True, hide_index=True)
                st.markdown(f"**{label} — how the bucket was matched**")
                st.dataframe(m["Bucket Match By"].fillna("Not in bucket pool").value_counts()
                             .rename_axis("Matched by").reset_index(name="Rows"),
                             use_container_width=True, hide_index=True)

    with t5:
        try:
            _render_interviews(result, pools, tr_sum, academy_kw, intensive_kw, extra_pass, extra_fail)
        except Exception as exc:
            st.error(f"Couldn't build the interview stats: {type(exc).__name__}: {exc}")
            st.code(traceback.format_exc(), language="text")

    if st.button("Append this summary to master Google Sheet", type="primary", key="append_summary"):
        try:
            _append_summary(client, _sheet_config("master_summary_sheet_url"), _summary_row(result, tr_rule))
            st.success("Summary appended to the configured master Google Sheet.")
        except Exception as exc:
            st.error(f"Could not append the summary: {exc}. Check that the signed-in account can edit the master sheet.")