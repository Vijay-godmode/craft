# CareerCraft testing strategy

## Test pyramid and commands

| Layer | Purpose | Current command |
| --- | --- | --- |
| Unit/domain | ATS, role classification, profile/job validation | `python -m unittest -q test_careercraft.py` |
| Integration | Account, CSRF, isolation, jobs, applications, search runs, DOCX, QA Lab | `python -m unittest -v test_careercraft.py` |
| Client syntax | Prevent JavaScript parse failures | `node --check static/app.js` |
| Server smoke | Start Waitress then request `/api/health` and `/sign-up` | See below |
| Browser/a11y/visual | Planned Playwright + axe | Phase Group C |
| Performance | Planned local/staging k6 only | Phase Group C |

## Baseline smoke suite

The integration test suite already executes the following stable smoke flow
against a temporary SQLite database:

1. Fetch CSRF token and register account.
2. Save candidate profile.
3. Create a QA job and approve it.
4. Verify application tracking.
5. Analyse a job and export a table-free DOCX resume.
6. Verify unauthorized access returns `401`, a missing CSRF token returns
   `403`, and a second account cannot access the first account's job.
7. Fetch QA Lab catalog, create an idempotent synthetic order, and verify
   invalid quantity is rejected.

## Manual release smoke checklist

1. Start the application with a non-default `RESUME_SECRET_KEY`.
2. Visit `/sign-up` or `/sign-in`; verify no private page is accessible before
   authentication.
3. Sign in, open Dashboard, Jobs, Resume Library, and QA Lab.
4. Refresh jobs with a fixture/local-safe source or add a manual job. Confirm
   Latest search results is distinct from the filtered New queue.
5. Create a QA Lab order twice with the same idempotency key; verify one order.
6. Sign out and repeat a protected API request; expect `401`.

## Test data rules

- Use temporary SQLite databases in tests.
- Use the synthetic QA Lab catalog/order data for API/data exercises.
- Use fixtures/mocks for job-provider tests; never depend on public sources.
- Use placeholder resumes only. Never commit resume exports, `data.db`, `.env`,
  access tokens, or passwords.
- Prefix future generated test data with a run ID and provide targeted cleanup.

## Quality gates for future phases

- Unit/integration pass before browser automation runs.
- API contracts have stable success/error schema checks.
- UI automation uses role/label selectors first and `data-testid` only where
  behaviour needs a stable machine selector.
- Accessibility test pages must not degrade the normal career workflow.
- Performance/load tests target local or dedicated staging only, with finite
  duration, finite virtual users, and a documented stop threshold.
- Every defect/failure scenario has an expected recovery state and a test.
