# CareerCraft

CareerCraft is a local-first, QA-focused resume workspace. It keeps one factual master profile, tailors an ATS-safe Word resume for a real job description, and keeps jobs in an approval-controlled inbox.

## Included workflow

- Editable QA / Test Engineer starter covering manual QA, automation, API testing, and SDET evidence. It uses obvious placeholders and prevents accidental personalised export until they are replaced.
- Profile backup and portability: import an existing `.docx`, text PDF, or `.txt` resume as a reviewable draft; import/export CareerCraft profile JSON; download an original starter `.docx`.
- Explainable Job Match: matched requirements, evidence, gaps, and ATS-readability guidance. It is a preparation signal, not a company ATS score or interview guarantee.
- Original single-column Word documents with standard sections, ordinary body text, and real Word bullets. No tables, columns, text boxes, images, or hidden keywords are used in ATS mode.
- India-first, source-attributed QA role discovery from free public feeds including Himalayas, Remotive, Jobicy, Remote OK, Arbeitnow, and The Muse, plus curated public Greenhouse product-company boards. Optional official Google Programmable Search results can add employer-board coverage, but are not required. Every source link is retained and saved locally.
- Filters for Test Engineer, Manual QA, QA Automation, SDET, API, performance, mobile, and accessibility roles, with product-company public-board and salary-disclosed signals where a source provides them.
- A reversible close action removes an accidentally approved role from the application queue; closed roles can be restored later.
- Optional local AI review via Ollama. If Ollama is unavailable, the app labels and uses a built-in local spelling, clarity, and structure review instead. Resume text is never sent to a cloud model by CareerCraft. The Local AI page can propose narrow, reviewable source edits; it cannot access secrets, run commands, or apply changes without confirmation.
- Manual links to Jobscan, Resume Worded, and Glassdoor research. Uploading to those external services is always your decision.

## LinkedIn boundary

CareerCraft does not scrape LinkedIn, automate LinkedIn activity, or auto-apply. Add a LinkedIn role by pasting its URL and description, or import a user-exported CSV/JSON file to show it in the in-app approval queue. A native LinkedIn search/alert link is available as a separate, optional handoff.

## Run locally

```powershell
cd "D:\Resume builder\prototypes\python"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
waitress-serve --host=127.0.0.1 --port=8000 app:app
```

Open `http://127.0.0.1:8000`.

## Enable the local LLM (optional)

Install Ollama for Windows, then use the compact default model:

```powershell
ollama pull qwen2.5:1.5b
```

Keep Ollama running and use **Proofread summary** or **Run local AI scan** in CareerCraft. The app falls back safely if the model is not installed.

For optional official Google employer-board search results, create a Google Programmable Search Engine and enable the Custom Search JSON API in Google Cloud. Then copy `.env.example` to `.env` and fill in both server-side values:

```powershell
Copy-Item .env.example .env
# Edit .env and set GOOGLE_CUSTOM_SEARCH_API_KEY and GOOGLE_CUSTOM_SEARCH_CX.
```

Restart CareerCraft after changing `.env`, then use **Refresh QA roles**. Results are employer-board links retained in the inbox for review; this integration does not create Google Jobs alerts or send push/email notifications.

CareerCraft does not scrape Google Jobs or LinkedIn. If the variables are not set, the Jobs inbox explains that the optional Google provider is unavailable while the other public sources and saved results remain usable.

## Test

```powershell
python -m unittest -v test_careercraft.py
node --check static\app.js
```

## Deployment note

This is a single-user local workspace. Before exposing it publicly, add authentication, per-user database isolation, HTTPS, CSRF protection, rate limits, backups, monitoring, and a secure secret manager. Do not publish the SQLite file or resume exports.

Crafted by Vijay Yadav.
