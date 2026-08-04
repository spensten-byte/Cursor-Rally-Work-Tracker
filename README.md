# Rally

Databricks-hosted Streamlit app for the Process & Solutions Enablement organization.
Each of the seven pillars submits Zoom AI meeting notes via its own tab; the app
generates an executive one-pager, automatically maintains a per-pillar project memory,
and rolls everything up into a single leadership summary on demand.

## Pillars

- Demand Excellence
- Order Capture & Promise Excellence
- Distribution Excellence
- Supply Excellence
- Inventory Deployment & Fulfillment Excellence
- Process Intelligence
- Process Enablement

## Prerequisites (confirm with Databricks admin)

| Requirement | Notes |
|---|---|
| **CAN CREATE APPS** | Deploy Streamlit as a Databricks App |
| **Foundation Model API** | Serving endpoint e.g. `databricks-claude-sonnet-4-6` |
| **Unity Catalog Volume** | Default `/Volumes/development/team_na_pase_process_intelligence/pase_work_tracker` |
| **Personal Access Token** | For local dev and UC file API |

Workspace: `https://nike-sole-react.cloud.databricks.com`

## Quick start (local)

```powershell
cd pase-work-tracker
copy .env.example .env
# Edit .env with DATABRICKS_HOST, DATABRICKS_TOKEN, PASE_TRACKER_VOLUME

pip install -r requirements.txt
streamlit run app.py
```

Without Databricks credentials, storage falls back to `./data/pase_work_tracker/`.

## Environment variables

See [`.env.example`](.env.example):

- `DATABRICKS_HOST` — workspace URL
- `DATABRICKS_TOKEN` — PAT with FM API + UC volume access (optional inside a Databricks App; SDK default auth is used)
- `DATABRICKS_MODEL_ENDPOINT` — FM serving endpoint name
- `PASE_TRACKER_VOLUME` — UC volume root path (legacy `MEETING_SUMMARY_VOLUME` still honored)
- `PASE_TRACKER_LOCAL_DIR` — local fallback directory (legacy `MEETING_SUMMARY_LOCAL_DIR` still honored)
- `CONTEXT_SUMMARY_COUNT` — prior summaries included as context per pillar (default 3)
- `MOCK_LLM=1` — disable LLM (tests only)
- `PASE_TRACKER_ADMIN_EMAILS` — comma-separated emails (matched against `X-Forwarded-User`) that may edit LLM prompts from the settings popover. Everyone else gets a read-only view + download. Edits persist to `{PASE_TRACKER_VOLUME}/prompts_overrides/` and survive redeploys; the in-repo `prompts/` files act as defaults whenever no override exists.

## Storage layout

```
{PASE_TRACKER_VOLUME or local_dir}/
  uploads/{pillar_slug}/...
  summaries/{pillar_slug}/...
  registries/project_registry_{pillar_slug}.json
```

Legacy flat-layout summaries from the prior Meeting Summary Agent are surfaced
under the Process Intelligence pillar for backward compatibility.

## Deploy to Databricks Apps

1. Create the UC volume (one-time):

   ```bash
   databricks volumes create development team_na_pase_process_intelligence pase_work_tracker MANAGED
   ```

2. Grant the app's service principal read/write on the volume.

3. Deploy the bundle and start the app:

   ```bash
   databricks auth login --host https://nike-sole-react.cloud.databricks.com
   databricks bundle deploy -t dev
   databricks bundle run pase_work_tracker -t dev
   ```

Users authenticate via Databricks SSO when opening the app URL.

## Workflow

1. **Pick your pillar tab** — Demand, Order Capture, Distribution, Supply, Inventory & Fulfillment, Process Intel, or Process Enable.
2. **Generate** — Upload `.txt`, `.md`, `.docx`, `.pdf`, `.xlsx`, or `.xls`, or paste notes; add manual context; click **Generate one-pager**. The project registry updates automatically.
3. **Edit (optional)** — Expand **Edit sections before downloading**, correct anything, and click **Re-render from edits**.
4. **History** — Browse and download prior one-pagers for this pillar.
5. **Leadership Rollup** — In the last tab, pick the latest summary from each pillar and synthesize a single cross-team leadership one-pager.

## Tests

```powershell
cd pase-work-tracker
py -m pytest tests/ -v
```

Golden test validates the May 6 fixture render (no LLM required).
