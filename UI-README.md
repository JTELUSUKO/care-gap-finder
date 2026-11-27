# Care-Gap Finder — Web UI

The same agent, now with a screen: pick a patient, run the agent, read
the gaps and the AI explanations, and approve or reject the draft alert
with a button. This is the UI the AI Testing track's Lesson 1 scenarios
run against.

## Run it (macOS Terminal, one command at a time)

**Step 1 — go to the project folder:**

```bash
cd ~/my-project/care-gap-finder
```

**Step 2 — activate the venv you already have:**

```bash
source venv/bin/activate
```

**Step 3 — install the three new packages (first time only):**

```bash
pip install -r requirements.txt
```

This adds FastAPI + Uvicorn to the venv, plus httpx, which the tests
use.

**Step 4 — start the server:**

```bash
python server.py
```

You should see: `Care-Gap Finder UI — open http://127.0.0.1:8000 in
your browser.` Leave that Terminal window open — the server runs until
you stop it.

**Step 5 — open the page:**

In your browser, go to: **http://127.0.0.1:8000**

## What to click

1. **Maria Santos (P001)** is selected by default. Click **Run agent**.
   You get her 1 gap (blood pressure, SOON), the plain-language
   explanation, the evidence, and a draft alert card.
2. Click **Approve** on the draft alert. It moves to `alerts/sent/` and
   the audit log is updated — the same as `app.py --approve`, but with
   a button.
3. Try **Aisha Patel (P005)** — the control patient. No gaps, no alert.
4. Switch the radio to **Claude explanations** and re-run Maria. Same
   gap, but the explanation is drafted live by your Claude Code CLI.
   (Needs Claude Code installed and signed in — if it isn't, you get a
   clear error and can switch back to Mock.)
5. The **Pending alerts** section at the bottom lists every alert
   waiting for a decision, including ones the command line created.
   Approve or reject from there too. Running a patient again never adds
   a second alert for the same gaps: if the result is unchanged, the
   waiting alert stays as it is; if it changed (say, Mock to Claude), the
   new alert replaces the old one under a new id, so approving the old id
   fails rather than sending text you did not see. A note appears if an
   alert for the same gap was already approved.

## Stop the server

Go back to the Terminal window and press **Ctrl+C**.

## Troubleshooting

- **"Address already in use"** — another server is still running on port
  8000 (maybe an earlier `python server.py` you forgot). Find its
  Terminal window and press Ctrl+C there, then start again.
- **Page won't load** — check the Terminal: the server prints the URL
  when it starts. If it printed an error instead, copy the last few
  lines and ask.
- **Claude mode errors** — the mock explanations always work (no
  network, no CLI). Claude mode needs the `claude` command on your Mac;
  the error message says so if it's missing.
- **Port 8000 blocked?** — run `python -m uvicorn server:app --port 8001`
  instead, then open http://127.0.0.1:8001.

## How it fits together

- `server.py` — FastAPI backend. Six endpoints around the same
  `run_agent` / `alerts` functions that `app.py` uses.
- `static/index.html` — the whole UI in one file: no build step, no
  external libraries, works offline.
- The UI never sees the full FHIR bundle — the server returns only the
  patient id and name, the gaps with their evidence, and a short alert
  summary. The MRN stays in the alert file and is never sent to the page.
