# CareerCraft architecture

Last verified: 2026-08-26

## Runtime

CareerCraft is a Python 3 Flask application with server-rendered Jinja pages
and vanilla JavaScript. The primary entry point is
`prototypes/python/app.py`; Waitress serves the app locally and in Docker.

| Layer | Current implementation | Responsibility |
| --- | --- | --- |
| Web/UI | Jinja templates + `static/app.js` + `static/styles.css` | Responsive career workspace, account, jobs, resume, and QA Lab screens |
| HTTP/API | Flask routes in `app.py` | Authentication, resume workflow, jobs, applications, QA Lab APIs |
| Domain helpers | `ats_engine.py`, `docx_builder.py`, `job_discovery.py`, `local_ai.py`, `lab_service.py`, `auth_service.py` | ATS analysis, DOCX, public sources, local Ollama, QA data, account validation |
| Storage | SQLite (`RESUME_DB_PATH`) | Local development/test persistence with foreign keys enabled |
| Deployment | Docker + Waitress; Render blueprint in `render.yaml` | Hosted web service |
| Tests | Python `unittest`; JavaScript syntax check | Workflow, security boundary, job refresh, document, and QA Lab coverage |

## Authentication and tenancy

- Flask-Login owns signed sessions.
- Passwords use Werkzeug password hashes; APIs never return passwords.
- Every mutation requires `X-CSRF-Token`; the server compares it to a
  session-scoped token.
- Existing unowned local workspace records are claimed once by the first
  registered account. New accounts are isolated by `user_id`.
- Failed sign-ins increment an account-level counter; after five failures a
  temporary lockout is stored. Account discovery is not exposed in responses.
- `admin` is the first local account and is the only role that can approve a
  local source-edit assistant proposal. This is not a complete RBAC model yet.

## Data boundaries

The career workspace currently uses the following durable records:

```text
users ──< user_profiles
  │
  ├──< jobs ──< applications
  │       └──< resume_versions
  │
  ├──< job_search_runs ──< job_search_results >── jobs
  │
  ├──< lab_catalog_items
  ├──< lab_orders ──< lab_order_items >── lab_catalog_items
  └──< qa_runs
```

Career profile content is currently stored as user-owned JSON to preserve the
flexible existing resume model. Jobs, applications, discovery results, QA Lab
orders, and test evidence use relational records with indexes/constraints.

## Primary user journeys

1. **Candidate resume flow**: sign up/sign in → profile → paste/select job →
   analyse requirements → select layout → download DOCX → resume library.
2. **Job discovery flow**: Jobs → public refresh/manual import → exact Latest
   search result cards → New/Approved/Closed inbox → application pipeline.
3. **Manual application flow**: Applications → add manual/walk-in/referral →
   optional recruiter/referral details → status/next step → close/reopen job.
4. **QA learning flow**: QA Lab → scenario → internal API/data/UI target →
   execute test → save test-run evidence.
5. **Local AI flow**: Resources/Local AI → local Ollama check → proofread or
   review. Resume text remains local to the app/Ollama endpoint.

## Deployment and configuration

| Variable | Purpose | Exposure |
| --- | --- | --- |
| `RESUME_SECRET_KEY` | Signs sessions/CSRF state | Server secret only |
| `RESUME_DB_PATH` | SQLite location | Server only |
| `RESUME_SESSION_SECURE` | Turns on HTTPS-only cookie flag | Server config |
| `LOCAL_CODE_ASSISTANT` | Enables local-admin code proposal application | Local development only |
| `GOOGLE_CUSTOM_SEARCH_API_KEY` / `GOOGLE_CUSTOM_SEARCH_CX` | Optional official Google Programmable Search | Server secret only |
| `PORT` | Local/hosted listener port | Host config |

The current Render free deployment uses `/tmp/careercraft.db`; it is ephemeral.
It is suitable for preview only. A durable public deployment requires managed
PostgreSQL, migrations, HTTPS, backups, monitoring, and rate limits.

## Known technical limitations and next boundaries

- `app.py` is still a large route/controller module. Future phases should move
  account/jobs/lab/admin code into Flask blueprints and services gradually.
- SQLite is intentionally retained for local learning. PostgreSQL support must
  be introduced with a migration path, not by replacing the current database.
- Public job discovery is source-attributed and best-effort. Google and
  LinkedIn are not scraped; results can be manual/import handoffs.
- QA Lab has real protected synthetic API/data exercises, but it does not yet
  provide recruiter/hiring-manager workflows, WebSockets, a mail service, or
  heavy-load execution controls.
- Feature flags, fault injection, performance endpoints, and admin inspection
  must stay environment-guarded and unavailable on public production.
