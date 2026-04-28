# ApplyPilot (this fork) — **web UI first**

> **Upstream:** Based on [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) by Pickle-Pixel. Not affiliated with applypilot.app or other commercial “ApplyPilot” products. This copy has no `LICENSE` / `CONTRIBUTING` in-repo; see upstream for those.

**You control the app in the browser.** A small local **Hub** (127.0.0.1) is the main interface: **Dashboard** (API keys alert + **Job board** with Diagnosis and embedded jobs HTML), **Explore** (score distribution + pipeline run), **Apply live** (URL + Run, live trace), and **Profile** (JSON, resume, searches, env check, doctor). The machine also runs **Claude Code + Playwright** in the background for form automation when you start a run from the UI.

**Python 3.11+** · work from the **repository root** so `.env` and `APPLYPILOT_DIR` resolve.

---

## 1. API keys (required for LLM features)

The repo includes **`env.placeholder`**: a **checked-in template** with fake placeholder text (e.g. `your-gemini-api-key-here`). It is **not** a real secret.

1. **Copy** it to a file named **`.env`** in the project root (this file is gitignored and is where you put real keys):

   ```bash
   cp env.placeholder .env
   ```

2. **Edit `.env`** and replace the placeholders with your real **Gemini** (or OpenAI / `LLM_URL`) credentials.  
   - If you leave placeholders or omit keys, the app will **remind you** in three ways: **warning in the terminal** when the hub starts, a **yellow banner on the Dashboard tab** in the browser, and **append-only lines** in **`logs/env_reminder.log`** under your data directory (e.g. `demo/user-data/logs/env_reminder.log` when using the demo profile path).

3. Optional: set **`APPLYPILOT_DIR=demo/user-data`** in `.env` to use the bundled **fake** profile and resume (safe for tests).

**Do not commit `.env`.** Commit only `env.placeholder` as the shared template.

---

## 2. Run the app (one step)

```bash
pip install -e .
# optional, if JobSpy install complains about numpy:
#   pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
```

(Complete **step 1** so `GEMINI_API_KEY` is set; then:)

```bash
applypilot
```

Your browser opens the Hub. **Apply live** → paste an application URL → **Run** (test mode: demo resume, dry-run, no DB). **Ctrl+C** in the terminal stops the server.

---

## What’s in the web UI

| Tab | Purpose |
|-----|---------|
| **Dashboard** | **API keys** banner (if needed) + **Job board** (embedded jobs HTML from your DB) with **Diagnosis** (database stats + doctor tier) shown as a compact footnote |
| **Explore** | Score distribution and **Run pipeline** (discovery→tailor stages from the browser) |
| **Apply live** | URL + Run, SSE trace (assistant, tools, tool results, usage) |
| **Profile** | Profile JSON, resume upload, searches.yaml, **Environment** (key presence, not values), doctor |

---

## Configuration (minimal)

| File / folder | Role |
|---------------|------|
| **`env.placeholder`** | Committed template — copy to **`.env`** and fill in (see above) |
| **`.env`** (gitignored) | Real API keys and optional `APPLYPILOT_DIR` |
| **`APPLYPILOT_DIR`** | Where profile, DB, `logs/env_reminder.log`, and other data live |

---

## Optional CLI (advanced)

`applypilot init`, `applypilot run`, `applypilot apply`, `applypilot doctor`, etc. — see `applypilot --help`. **Normal use: `applypilot` → browser only.**

---

## Requirements for Apply live

| Need | For |
|------|-----|
| Real LLM key in `.env` | Scoring / tailoring if you use those features |
| **Claude Code CLI**, **Chrome**, **Node (`npx`)** | Observed apply from **Apply live** |

---

## Upstream

[Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) — original project and license/contributing info.
