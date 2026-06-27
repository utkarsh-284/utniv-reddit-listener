# Deploying the Reddit Listener on GitHub Actions

Runs `python run.py` every 30 minutes on GitHub's servers. Built to survive Reddit's
aggressive throttling of datacenter (Azure) IPs: few requests, patient jittered backoff on
429/403, graceful degradation, and a Slack warning if a run can't reach Reddit (never silent).

---

## Step 0 — Pick the repo (one decision)

**Option A — Standalone public repo `utniv-reddit-listener` (RECOMMENDED).**
Just the listener code. Keeps your business docs out of GitHub, and the public URL doubles as
the **source-code link for your Reddit Data API request**. Clean home for an app with its own CI.

**Option B — Your existing `Utniv` repo (must be PRIVATE).**
No restructuring, but the whole business-docs repo goes to GitHub — it **must be private**, and it
can't serve as the Data API source link (reviewers can't see a private repo).

The steps below are for **Option A**. (For Option B: push the Utniv repo private, keep the workflow
at `.github/workflows/reddit-listener.yml` with `working-directory: reddit-listener` as-is, set the
same secrets, done.)

---

## Step 1 — Create the standalone repo (Option A)

From the `reddit-listener/` folder. The workflow moves to the new repo's root `.github/workflows/`
and drops `working-directory` (code is now at the repo root).

```bash
cd reddit-listener
git init -b main
mkdir -p .github/workflows
# copy the workflow in, removing the working-directory line (code is at root here):
#   - delete the two `defaults:/run:/working-directory: reddit-listener` lines
#   - change cache-dependency-path to: requirements.txt
cp ../.github/workflows/reddit-listener.yml .github/workflows/reddit-listener.yml
#   (then edit those two spots — or use the standalone version noted at the bottom)

git add .
git commit -m "Reddit listening engine (RSS, Supabase, Slack)"
gh repo create utniv-reddit-listener --public --source=. --remote=origin --push
```

`.env` is gitignored, so **no secrets are committed.** Verify with `git status` before pushing.

---

## Step 2 — Add the secrets (repo → Settings → Secrets and variables → Actions → "New repository secret")

| Secret | Value |
|---|---|
| `SUPABASE_URL` | your Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service-role key (server-only) |
| `OPENAI_API_KEY` | your OpenAI key |
| `OPENAI_MODEL` | `gpt-4o-mini` (optional; this is the default) |
| `NIM_API_KEY` | your NVIDIA NIM key (fallback) |
| `NIM_BASE_URL` | `https://integrate.api.nvidia.com/v1` |
| `NIM_MODEL` | `meta/llama-3.3-70b-instruct` |
| `SLACK_WEBHOOK_URL` | your `#reddit-signals` incoming webhook |

The RSS tuning + thresholds are baked into the workflow as literals (no secrets needed for those).
`REDDIT_*` are **not** needed in rss mode.

---

## Step 3 — First run (manual) + verify

1. Repo → **Actions** tab → if prompted, **enable workflows**.
2. Click **reddit-listener** → **Run workflow** (the `workflow_dispatch` button).
3. Open the run logs. Healthy output ends with a `[pipeline] done: {...}` line.
4. Check your Supabase `reddit_threads` table and your Slack channel.

Once that's green, the **cron takes over automatically every 30 minutes** — nothing else to do.

---

## What "reliable on a datacenter IP" actually means here

- **Few requests:** 8 subs fetched as **2 combined feeds**, not 8 calls → small rate-limit surface.
- **Patient, jittered backoff:** on 429/403 it waits 20→40→80→160→320s (+ random jitter), up to 5
  retries, honoring `Retry-After`. A 12-minute job timeout leaves ample room.
- **Graceful degradation:** if a run still can't reach Reddit, it **posts a Slack warning**, logs the
  failure to `reddit_runs`, and **exits cleanly** so the next 30-min run just retries. RSS feeds
  persist, so a skipped run loses nothing — the next one catches up.
- **No silent breakage:** you'll see a ⚠️ in Slack if Reddit ever blocks the runner IP for a cycle.

### If Reddit blocks the Azure IP *persistently* (you'd see repeated ⚠️ Slack warnings)
GitHub's IP ranges are heavily used and Reddit *may* throttle them harder than a home IP. If the
warnings become constant, switch to the **local runner** (your residential IP, which already works):

- **Windows Task Scheduler:** Create Task → Trigger: daily, repeat every 30 min → Action:
  `Program: <path>\.venv\Scripts\python.exe`, `Arguments: run.py`,
  `Start in: C:\Users\Utkarsh\Documents\Projects\Utniv\reddit-listener`.
  Same code, same `.env`, far fewer 429s. Only runs while your PC is on.

You can run **both** (GitHub for always-on + local as backup) — dedup means duplicates are ignored.

---

## Operating notes
- **Scheduled workflows pause after 60 days of no repo commits** (GitHub policy). A trivial commit
  resets it; or the local runner sidesteps it entirely.
- **Tune anytime:** edit the `ALERT_THRESHOLD` / `REPLY_ICP_FLOOR` literals in the workflow, commit.
  To re-push past posts that newly qualify: run `python run.py --realert` locally.
- **Cost:** GitHub Actions free minutes cover this easily; OpenAI ~$2–5/mo; Supabase/Slack free.

---

## Standalone workflow (Option A) — the two edits vs the Utniv version
In `.github/workflows/reddit-listener.yml` for the standalone repo:
1. Remove these lines (code is at repo root, not a subfolder):
   ```yaml
   defaults:
     run:
       working-directory: reddit-listener
   ```
2. Change `cache-dependency-path: reddit-listener/requirements.txt` → `requirements.txt`.
Everything else (env, secrets, cron) stays identical.
