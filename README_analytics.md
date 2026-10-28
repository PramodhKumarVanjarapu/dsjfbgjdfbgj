# Applicant Analytics (separate Streamlit app)

## What it does
- Reads a total-applications sheet and a shortlisted-profiles sheet for one job.
- Uses the same local bucket and assessment/TR CSV files already used by your existing `app.py` (`data/offline_attended_pool.csv` and `data/assessment_pool_main.csv`) to match candidates.
- Shows product-wise and TR-cleared statistics for applications and shortlisted profiles.
- Shows Academy-only bucket A/B/C/D counts and TR-cleared counts. Non-Academy profiles are excluded from bucket statistics.
- Appends a run summary to a fixed master Google Sheet. The master URL is not requested in the UI.

## Setup

1. Install packages:
   ```bash
   pip install -r requirements_analytics.txt
   ```
2. Create `.streamlit/secrets.toml` beside the app:
   ```toml
   [gcp_service_account]
   type = "service_account"
   project_id = "YOUR_PROJECT_ID"
   private_key_id = "YOUR_PRIVATE_KEY_ID"
   private_key = "-----BEGIN PRIVATE KEY-----\nYOUR_PRIVATE_KEY\n-----END PRIVATE KEY-----\n"
   client_email = "YOUR_SERVICE_ACCOUNT_EMAIL"
   client_id = "YOUR_CLIENT_ID"
   token_uri = "https://oauth2.googleapis.com/token"
   auth_uri = "https://accounts.google.com/o/oauth2/auth"
   auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
   client_x509_cert_url = "YOUR_CLIENT_CERTIFICATE_URL"

   [analytics]
   master_summary_sheet_url = "https://docs.google.com/spreadsheets/d/YOUR_MASTER_SHEET_ID/edit#gid=0"
   ```
   Copy the real values from your service-account JSON. Do not commit `secrets.toml` or credentials to Git.
3. Copy the two existing lookup CSVs used by `app.py` into the new project's `data/` folder:
   - `offline_attended_pool.csv` (bucket details)
   - `assessment_pool_main.csv` (TR status details)
4. Share the master summary sheet with the service account email (`client_email`) and give it **Editor** access. The application and shortlisted sheets must also be accessible to that account.
5. Run:
   ```bash
   streamlit run analytics_app.py
   ```
6. In the app, paste the two changing links (applications and shortlist), provide the Job ID if it cannot be inferred, review results, then click **Append this summary to master Google Sheet**.

The application spreadsheet may contain multiple tabs. The app combines all tabs that have both a phone/mobile column and a resume-link column, and counts only rows where both fields are populated. Product should be present in those application tabs; if the shortlist sheet lacks Product, the app tries to match shortlist candidates back to the combined applications data. The bucket and TR CSVs are local lookup files, not extra Google Sheet links.
