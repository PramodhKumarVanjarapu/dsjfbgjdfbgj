"""Single-job applicant analytics app (one Google Sheet in, all stats out).

Run with: streamlit run analytics_app.py

Input: ONE Google Sheet link. The workbook is read tab by tab:
  * Application tabs  - every tab that has BOTH a mobile/phone column and a resume-link column
                        (tabs whose name starts with "shortlist" or contains "product" are never counted here)
  * Shortlist tabs    - every tab whose name starts with "shortlist" (Shortlist, Shortlisted Profiles, ...)
  * Product tab       - the tab whose name contains "product"; maps each candidate to a product
All stats are computed on UNIQUE candidates (one row per person).

The master summary sheet link is configured once in .streamlit/secrets.toml:
    [analytics]
    master_summary_sheet_url = "https://docs.google.com/spreadsheets/d/..."
"""
from __future__ import annotations

import hmac
import os
import re
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
import pandas as pd
import streamlit as st

import core
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
    width = len(headers)
    rows = [(r + [""] * width)[:width] for r in values[hdr + 1:]]
    df = pd.DataFrame(rows, columns=headers)
    blank = df.apply(lambda row: all(not str(v).strip() for v in row), axis=1)
    return df.loc[~blank].reset_index(drop=True)


def _find_job_col(df: pd.DataFrame):
    return core.find_col(df, ["jobid", "jdid", "jobcode", "requisitionid"])


# --------------------------------------------------------------------------- #
# Tab classification + loading
# --------------------------------------------------------------------------- #
def _is_shortlist_tab(title: str, keywords=("shortlist",)) -> bool:
    # "Shortlisted", "Shortlist - Round 1", "Final Shortlisted Profiles", "1. Shortlisted" ...
    # `keywords` comes from the sidebar so tabs like "New Profiles" can be added.
    n = core.norm_col(title)
    return any(core.norm_col(k) and core.norm_col(k) in n for k in keywords) and not n.startswith("not")


def _is_product_tab(title: str) -> bool:
    return "product" in core.norm_col(title)


def _standardise(df: pd.DataFrame, tab: str) -> pd.DataFrame:
    """Rename whatever columns a tab uses to the standard schema."""
    det = core.detect_columns(df)
    out = pd.DataFrame(index=df.index)
    for key, std in STD.items():
        out[std] = df[det[key]] if det.get(key) else ""
    # Some exports (e.g. the shortlisted "New Profiles" tab) label the candidate's 10-digit
    # mobile number as "UUID"/"UID". If there is no mobile column and that column is
    # (almost) all phone numbers, treat it as the mobile number.
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


def load_workbook(client, url: str, shortlist_kw=("shortlist",)) -> dict:
    """Read every tab of the single input sheet and sort the tabs into their roles."""
    if not url or "docs.google.com/spreadsheets" not in url:
        raise ValueError("Enter a valid Google Sheets URL.")
    book = client.open_by_url(url.strip())

    apps, shorts, products = [], [], []
    fallback_product = None
    app_tabs, short_tabs, product_tabs, skipped = [], [], [], []

    for ws in book.worksheets():
        df = _worksheet_to_df(ws)
        if df.empty:
            skipped.append(f"{ws.title} (empty)")
            continue
        det = core.detect_columns(df)

        if _is_shortlist_tab(ws.title, shortlist_kw):
            std = _standardise(df, ws.title)
            identity = std["Mobile"].map(core.norm_mobile).notna() | std["Email"].map(core.norm_email).notna() \
                | std["User ID"].map(core.norm_text).notna()
            std = std[identity]
            if std.empty:
                skipped.append(f"{ws.title} (shortlist tab, but no mobile / email / user id found; "
                               f"columns seen: {list(df.columns)[:15]})")
                continue
            # Shortlist products are NOT taken from the shortlist tab itself; they are
            # mapped from the total product data (product tab / total applications).
            std["Product"] = ""
            shorts.append(std)
            short_tabs.append(f"{ws.title} ({len(std):,} rows)")

        elif _is_product_tab(ws.title):
            products.append(df)
            product_tabs.append(f"{ws.title} ({len(df):,} rows)")

        elif det.get("mobile") and det.get("resume"):
            std = _standardise(df, ws.title)
            valid = std["Mobile"].map(core.norm_mobile).notna() & _has_value(std["Resume"])
            std = std[valid]
            if std.empty:
                skipped.append(f"{ws.title} (no rows with both mobile and resume link)")
                continue
            apps.append(std)
            app_tabs.append(f"{ws.title} ({len(std):,} rows)")

        elif det.get("product") and (det.get("mobile") or det.get("uid") or det.get("email")):
            # A product list whose tab is not literally named "product".
            if fallback_product is None:
                fallback_product = (ws.title, df)
            else:
                skipped.append(f"{ws.title} (not an application, shortlist or product tab)")
        else:
            skipped.append(f"{ws.title} (no mobile + resume columns)")

    if not products and fallback_product is not None:
        products.append(fallback_product[1])
        product_tabs.append(f"{fallback_product[0]} (detected by its columns)")

    if not apps:
        raise ValueError("No tab has both a mobile/phone column and a resume-link column with data.")

    return {
        "apps": pd.concat(apps, ignore_index=True),
        "shorts": pd.concat(shorts, ignore_index=True) if shorts else pd.DataFrame(),
        "products": pd.concat(products, ignore_index=True, sort=False) if products else pd.DataFrame(),
        "app_tabs": app_tabs, "short_tabs": short_tabs, "product_tabs": product_tabs, "skipped": skipped,
    }


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
    cols = {k: STD[k] for k in STD}
    prepared = core.prepare_company(std_df, cols, None, {}, None, None, academy_kw, intensive_kw)
    prepared = core.apply_products(prepared, product_lookup, academy_kw, intensive_kw)
    matched = core.match_company(prepared, pools, tr_sum)
    return matched, core.unique_candidates(matched)


def _append_summary(client, master_url: str, row: dict):
    if not master_url:
        raise RuntimeError("Missing analytics.master_summary_sheet_url in Streamlit secrets.")
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
    return row


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
st.title("📊 Applicant Analytics — Single Job")
st.caption("Paste one Google Sheet link. Applications, shortlist and product tabs are detected automatically. "
           "The master summary sheet is configured once in secrets.")

with st.sidebar:
    st.header("Analytics rules")
    academy_text = st.text_input("Academy product keyword(s)", "ACADEMY")
    intensive_text = st.text_input("Intensive product keyword(s)", "INTENSIVE")
    shortlist_text = st.text_input("Shortlist tab name keyword(s)", "shortlist, new profiles",
                                   help="Any tab whose name contains one of these is read as shortlisted profiles.")
    tr_rule = st.selectbox("TR Cleared rule", core.TR_RULES, index=0)
    cleared_values = st.multiselect("Statuses counted as cleared",
                                    ["cleared", "clear", "passed", "selected"], default=["cleared"])

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
    st.subheader("Input sheet")
    sheet_url = st.text_input("Google Sheet link (all tabs in one workbook)",
                              placeholder="Paste the sheet URL containing applications, shortlist and product tabs")
    job_id = st.text_input("Job ID (optional)", placeholder="Leave blank to read it from the sheet")
    run_analysis = st.form_submit_button("Fetch sheet and calculate stats", type="primary")

if run_analysis:
    if not sheet_url.strip():
        st.error("Please paste the Google Sheet link.")
        st.stop()
    try:
        with st.spinner("Reading all tabs…"):
            shortlist_kw = [x.strip() for x in shortlist_text.split(",") if x.strip()] or ["shortlist"]
            wb = load_workbook(client, sheet_url, shortlist_kw)

        job_ids = list(dict.fromkeys(str(x).strip() for x in wb["apps"]["Job ID"] if str(x).strip()))
        resolved_job = job_id.strip()
        if not resolved_job:
            if len(job_ids) == 1:
                resolved_job = job_ids[0]
            elif len(job_ids) > 1:
                st.warning(f"Found {len(job_ids)} different Job IDs in the sheet. "
                           "Enter the intended Job ID above and run again.")
                st.stop()

        product_lookup = {}
        if not wb["products"].empty:
            product_lookup = core.build_product_lookup(wb["products"], academy_kw, intensive_kw)
        # Total applications' own product column fills anyone the product tab doesn't cover.
        if (~wb["apps"]["Product"].map(core._is_junk)).any():
            apps_lookup = core.build_product_lookup(wb["apps"], academy_kw, intensive_kw,
                                                    source="Total applications tab")
            for key, val in apps_lookup.items():
                product_lookup.setdefault(key, val)

        apps_matched, apps_unique = _process(wb["apps"], product_lookup, academy_kw, intensive_kw, pools, tr_sum)
        short_matched, short_unique = _process(wb["shorts"], product_lookup, academy_kw, intensive_kw, pools, tr_sum)

        st.session_state["analytics_result"] = {
            "job_id": resolved_job, "sheet_url": sheet_url.strip(),
            "app_tabs": wb["app_tabs"], "short_tabs": wb["short_tabs"],
            "product_tabs": wb["product_tabs"], "skipped": wb["skipped"],
            "apps_matched": apps_matched, "short_matched": short_matched,
            "apps_unique": apps_unique, "short_unique": short_unique,
        }
    except Exception as exc:
        st.error(f"Couldn't process the sheet: {type(exc).__name__}: {exc}")
        with st.expander("Detailed error information", expanded=True):
            st.code(traceback.format_exc(), language="text")
        st.info("Check that the sheet URL is correct, the signed-in Google account has access, "
                "and the expected columns/tabs are present.")
        st.stop()

result = st.session_state.get("analytics_result")
if result:
    app_u, short_u = result["apps_unique"], result["short_unique"]
    st.divider()
    st.subheader(f"Results · Job ID: {result['job_id'] or 'Not provided'}")

    if not result["short_tabs"]:
        st.warning("No tab whose name starts with 'Shortlist' was found, so shortlist numbers are zero.")
    if not result["product_tabs"]:
        st.warning("No product tab found. Products were only taken from the application tabs' own product "
                   "column; everything without one is counted as External.")

    st.markdown("### Overall summary (unique candidates)")
    st.dataframe(pd.DataFrame([
        core.candidate_stats(app_u, "Total applications"),
        core.candidate_stats(short_u, "Shortlisted profiles"),
    ]), use_container_width=True, hide_index=True)

    t1, t2, t3, t4 = st.tabs(["Product-wise", "Bucket-wise (A–D)", "Candidate details", "Data checks"])

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
        st.markdown("**Tabs used as applications** (mobile + resume link present)")
        st.write(result["app_tabs"] or "None")
        st.markdown("**Tabs used as shortlist**")
        st.write(result["short_tabs"] or "None")
        st.markdown("**Tabs used as product mapping**")
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

    if st.button("Append this summary to master Google Sheet", type="primary", key="append_summary"):
        try:
            _append_summary(client, _sheet_config("master_summary_sheet_url"), _summary_row(result, tr_rule))
            st.success("Summary appended to the configured master Google Sheet.")
        except Exception as exc:
            st.error(f"Could not append the summary: {exc}. Check that the signed-in account can edit the master sheet.")
