"""Interview-round helpers: what a status word means, which columns are rounds, order checks.

Every cell of a round column ("Cleared", "Not Cleared", "No show", "Scheduled", "Offered" ...)
is turned into one of a small set of stage values:

    Cleared | Rejected | In process | No-show / Dropped | Hired / Offer | Other | ""  (blank = not reached)

"Hired / Offer" also counts as having cleared that round.
"""
from __future__ import annotations

import re

import pandas as pd

import core

CLEARED = "Cleared"
REJECTED = "Rejected"
IN_PROCESS = "In process"
NO_SHOW = "No-show / Dropped"
OFFER = "Hired / Offer"
OTHER = "Other"
BLANK = ""

STAGE_VALUES = [CLEARED, REJECTED, IN_PROCESS, NO_SHOW, OFFER, OTHER]
PASSED = (CLEARED, OFFER)  # stages that count as "cleared this round"

# Most progressed first: used to merge duplicate rows of the same person.
STAGE_RANK = {OFFER: 0, CLEARED: 1, IN_PROCESS: 2, REJECTED: 3, NO_SHOW: 4, OTHER: 5, BLANK: 6}

_NO_SHOW_RE = re.compile(
    r"\b(no show|noshow|absent|not attended|did not attend|didn t attend|didnt attend|drop|dropped|"
    r"dropped out|withdrawn|withdrew|not interested|declined|backed out|unreachable|not reachable|"
    r"not responding|no response|not joined|did not join|opted out)\b")
_REJECT_RE = re.compile(
    r"\b(reject|rejected|not clear|not cleared|fail|failed|not selected|not qualified|not shortlisted|"
    r"not offered|not eligible|no hire|not a fit|not fit|not suitable|screened out|eliminated|disqualified)\b")
_PENDING_RE = re.compile(
    r"\b(yet to|to be|awaiting|awaited|pending|scheduled|schedule|rescheduled|on hold|hold|tbd|upcoming|"
    r"planned|in progress|in process|ongoing|waiting)\b")
_OFFER_RE = re.compile(r"\b(offer|offered|offer released|hired|joined|onboarded|placed)\b")
_CLEARED_RE = re.compile(
    r"\b(clear|cleared|pass|passed|selected|qualified|shortlisted|approved|positive|recommended|"
    r"moved to next|next round|proceed)\b")
_ATTENDED_RE = re.compile(r"\b(attended|completed|interviewed|done)\b")

_cache: dict = {}


def _flat(value) -> str:
    """'No-Show ' / 'Not  Cleared' -> 'no show' / 'not cleared'."""
    s = core.norm_status(value)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def classify(value, extra_pass=(), extra_fail=()) -> str:
    """Map one cell to a stage value. Blank / junk cells give BLANK."""
    s = _flat(value)
    if not s:
        return BLANK
    key = (s, tuple(extra_pass), tuple(extra_fail))
    if key in _cache:
        return _cache[key]

    def has(words):
        return any(w and re.search(r"\b" + re.escape(re.sub(r"[^a-z0-9]+", " ", w.lower()).strip()) + r"\b", s)
                   for w in words)

    if has(extra_fail):
        out = REJECTED
    elif has(extra_pass):
        out = CLEARED
    elif _NO_SHOW_RE.search(s):
        out = NO_SHOW
    elif _REJECT_RE.search(s):
        out = REJECTED
    elif _PENDING_RE.search(s):
        out = IN_PROCESS
    elif _OFFER_RE.search(s):
        out = OFFER
    elif _CLEARED_RE.search(s):
        out = CLEARED
    elif _ATTENDED_RE.search(s):
        out = IN_PROCESS  # interview happened, result not stated yet
    else:
        out = OTHER
    if len(_cache) < 50000:
        _cache[key] = out
    return out


# Columns that are never rounds.
_NOT_ROUND = (
    "mobile", "phone", "contact", "mail", "name", "userid", "uid", "resume", "cv", "product", "priority",
    "college", "company", "date", "time", "link", "url", "remark", "comment", "feedback", "interviewer",
    "panel", "reason", "venue", "location", "mode", "jobid", "slot", "city", "salary", "ctc", "branch",
    "degree", "year", "score", "rating", "unnamed", "bucket", "cycle",
)
_ROUND_HINT = (
    "round", "interview", "screen", "technical", "managerial", "assignment", "status", "result", "outcome",
    "stage", "final", "offer", "selection", "discussion", "fitment", "cultural", "assessment", "test",
)


def _looks_like_round_name(norm: str) -> bool:
    return any(h in norm for h in _ROUND_HINT) or bool(re.match(r"^(hr|[lr]\d)", norm))


def detect_round_columns(df: pd.DataFrame) -> list:
    """Columns that hold interview-round statuses, in sheet order.

    A column qualifies when most of its filled cells are recognisable status words
    (60%, or 30% if the header also sounds like a round / status / result).
    """
    found = []
    for col in df.columns:
        norm = core.norm_col(col)
        if not norm or any(x in norm for x in _NOT_ROUND):
            continue
        vals = df[col].map(_flat)
        vals = vals[vals != ""]
        if vals.empty:
            continue
        recognised = (vals.map(classify) != OTHER).mean()
        if recognised >= (0.3 if _looks_like_round_name(norm) else 0.6):
            found.append(col)
    return found


def out_of_order(stages: pd.DataFrame, round_cols) -> pd.Series:
    """True for candidates who have a status in a later round while an earlier round isn't Cleared."""
    cols = list(round_cols)
    flags = []
    for _, row in stages[cols].iterrows():
        vals = [row[c] for c in cols]
        filled = [i for i, v in enumerate(vals) if v != BLANK]
        if not filled:
            flags.append(False)
            continue
        last = max(filled)
        flags.append(any(vals[i] not in PASSED for i in range(last)))
    return pd.Series(flags, index=stages.index, dtype=bool)