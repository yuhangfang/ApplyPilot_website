# Demo user data

`user-data/` holds **fictional** `profile.json`, `resume.txt`, `resume.pdf`, and `searches.yaml` for local testing.

In the **project root**, copy the API template and set your key (see main **README**):

```bash
cp env.placeholder .env
# Edit .env: set GEMINI_API_KEY and APPLYPILOT_DIR=demo/user-data
```

Runtime outputs (SQLite DB, logs such as `env_reminder.log`, tailored files, Chrome worker dirs) are created under `demo/user-data/` when `APPLYPILOT_DIR` points there.
