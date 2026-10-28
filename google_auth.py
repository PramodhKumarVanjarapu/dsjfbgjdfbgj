"""Google sign-in for the analytics app.

Two modes:

1. HOSTED (Render): set the environment variable GOOGLE_TOKEN_JSON to the full contents of a
   token.json created on your own computer. Nothing is written to disk, so restarts and
   redeploys never lose the login. The access token is refreshed in memory from the refresh token.

2. LOCAL: if GOOGLE_TOKEN_JSON is not set, the original browser sign-in is used and the result is
   saved to token.json (needs oauth_client.json next to this file). Run the app locally once to
   create token.json, then copy its contents into GOOGLE_TOKEN_JSON on Render.
"""
import json
import os
import secrets
from pathlib import Path

import streamlit as st
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

BASE_DIR = Path(__file__).parent
CLIENT_SECRET_FILE = BASE_DIR / "oauth_client.json"
TOKEN_FILE = BASE_DIR / "token.json"
PENDING_FILE = BASE_DIR / "oauth_pending.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

REDIRECT_URI = "http://localhost:8501"


@st.cache_resource(show_spinner=False)
def _env_credentials(token_json: str) -> Credentials:
    """Build credentials once per process from the GOOGLE_TOKEN_JSON variable."""
    return Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)


def get_google_credentials():
    # ---- Hosted mode: token supplied through an environment variable ----
    env_token = os.environ.get("GOOGLE_TOKEN_JSON", "").strip()
    if env_token:
        try:
            credentials = _env_credentials(env_token)
            if not credentials.valid:
                credentials.refresh(Request())
        except Exception as exc:
            st.error(
                "GOOGLE_TOKEN_JSON is set but could not be used "
                f"({type(exc).__name__}: {exc}). Create a fresh token.json locally and paste it again."
            )
            st.stop()
        return credentials

    # ---- Local mode: original browser sign-in with files on disk ----
    if not CLIENT_SECRET_FILE.exists() and not TOKEN_FILE.exists():
        st.error(
            "No Google login configured. Set the GOOGLE_TOKEN_JSON environment variable "
            "(hosted) or place oauth_client.json beside google_auth.py (local)."
        )
        st.stop()

    # Reuse saved credentials
    if TOKEN_FILE.exists():
        credentials = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            TOKEN_FILE.write_text(credentials.to_json())

        if credentials.valid:
            return credentials

    # Handle Google's callback
    if "code" in st.query_params:
        if not PENDING_FILE.exists():
            st.error("OAuth login details are missing. Start a fresh login.")
            st.query_params.clear()
            st.stop()

        pending = json.loads(PENDING_FILE.read_text())

        returned_state = st.query_params.get("state")
        if not returned_state or not secrets.compare_digest(returned_state, pending["state"]):
            st.error("OAuth state mismatch. Start a fresh login.")
            st.query_params.clear()
            st.stop()

        flow = Flow.from_client_secrets_file(
            str(CLIENT_SECRET_FILE),
            scopes=SCOPES,
            state=pending["state"],
            redirect_uri=REDIRECT_URI,
            autogenerate_code_verifier=False,
        )
        flow.code_verifier = pending["code_verifier"]

        flow.fetch_token(code=st.query_params["code"])
        credentials = flow.credentials

        TOKEN_FILE.write_text(credentials.to_json())
        PENDING_FILE.unlink(missing_ok=True)
        st.query_params.clear()

        return credentials

    # Start a fresh login
    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_FILE),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        autogenerate_code_verifier=True,
    )

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    PENDING_FILE.write_text(json.dumps({
        "state": state,
        "code_verifier": flow.code_verifier,
    }))

    st.info("Sign in with your NxtWave Google account.")
    st.link_button("Sign in with Google", authorization_url)
    st.stop()
