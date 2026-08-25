# QA Lab implementation plan

This plan converts the master plan into safe, incremental deliverables. A
phase is complete only when its behaviour, tests, docs, and deployment guard
are present. The master plan prohibits a blind all-at-once rewrite.

## Verified baseline (Phases 0–5, partial 6–9)

| Area | Delivered | Verification |
| --- | --- | --- |
| Discovery | Repository, runtime, routes, env, deployment, journeys documented | Local inspection + deployed sign-in request |
| Accounts | Registration/sign-in/sign-out, password hashing, CSRF, lockout, private records | Integration tests |
| Resume platform | Profile, ATS analysis, original DOCX layouts, library, import/export | DOCX workflow tests |
| Job search | Persisted search runs and exact visible result cards | Discovery/search-run regression test |
| QA API/data | Synthetic catalog/order API, idempotency, stock checks, user ownership | QA Lab API tests |
| QA scenarios | Smoke, sanity, integration, API, data, UI/a11y, compatibility, performance, security, CI/CD learning flows | Protected QA Lab pages |
| Evidence | User-owned QA run records | QA Lab API/UI |

## Sequential delivery groups

### Group A — platform/RBAC/data (master phases 1, 2, 6, 7, 8)

1. Introduce `candidate`, `company`, recruiter/hiring-manager assignment,
   interviews, notifications, audit events, roles, and permissions.
2. Add explicit `candidate`, `recruiter`, `hiring_manager`, and `admin` role
   fixtures. Protect every route/API with object-level authorization.
3. Add pagination/filter/sort contracts for jobs, candidates, applications,
   interviews, and audit history.
4. Add a PostgreSQL-compatible data access/migration layer while preserving
   SQLite local mode.

Exit criteria: two-user and multi-role tests prove horizontal and vertical
authorization; an account cannot access another account's object IDs.

### Group B — deterministic labs (master phases 3–5, 10–17, 19–23, 26–27)

1. Add a CLI test-data factory with tagged datasets and safe cleanup.
2. Add isolated admin-only QA environment controls: feature flags, finite
   fault responses, bounded delay, bounded payloads, and controlled network
   scenarios. Production default is disabled.
3. Add challenge modules only in QA Lab: form controls, table/list behaviour,
   modal/file/multi-step components, accessibility defects, responsive
   fixtures, database inspection summaries, and concurrency exercises.
4. Add bounded performance endpoints and never expose arbitrary target URLs or
   unlimited CPU/memory work.

Exit criteria: every control is admin+environment gated, deterministic, has a
clear expected result, and can be reset.

### Group C — automation/quality operations (master phases 9, 14–18, 24–25, 28–35)

1. Create Playwright UI/API/a11y/visual suites plus page objects/fixtures.
2. Add k6 scripts that target only local/staging bounded endpoints.
3. Add request/correlation IDs, JSON logs, readiness, audit events, timing,
   feature-flag state, and a dashboard that ingests test artifacts.
4. Add GitHub Actions checks and artifacts; never schedule heavy tests against
   production.
5. Complete dedicated testing docs and a feature-to-test coverage matrix.

Exit criteria: CI creates reports/traces/screenshots; a QA run has an ID,
timestamp, status, and links to usable artifacts.

## Safety gates

- No Google/LinkedIn scraping or automated job application.
- No public arbitrary SQL, network target, shell, code execution, or
unbounded resource endpoint.
- No fault injection, vulnerable challenge, or high-load control in production.
- No production stress/spike/soak tests.
- No secret, password, token, or real resume content in generated logs.
- Each migration must have a tested backward-compatible path or backup plan.
