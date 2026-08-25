# CareerCraft 35-Phase Status and Definition of Done

Last verified: 2026-08-26

A phase is **done** only when its user-visible or API behavior exists, its data and safety boundaries are implemented, deterministic tests cover the critical path, documentation names how to use it, and the relevant local validation command passes. A page that only describes a feature is not enough.

| Phase | Status | Definition of done | Current evidence / next gap |
| --- | --- | --- | --- |
| 0 Discovery and baseline | Complete | Architecture, routes, risks, deployment, journeys, and baseline smoke tests are documented. | `ARCHITECTURE.md`, `TESTING_STRATEGY.md`, and 19-test suite. |
| 1 Production career platform | Partial | Candidate, recruiter, hiring-manager, and admin journeys work through UI/API with jobs, applications, interviews, notifications, and audit history. | Auth, marketplace, recruiter CRUD APIs, interviews, and notifications exist. Admin controls and complete recruiter UI remain. |
| 2 Database/data model | Partial | Relationships, constraints, indexes, ownership, migrations, transactions, and a tested PostgreSQL-compatible deployment path exist. | SQLite ownership schema and transactions exist. PostgreSQL adapter, migrations, and durable hosted storage remain. |
| 3 Test data factory | Complete | Tagged deterministic normal data can be seeded at bounded sizes and removed without deleting unrelated data. | `test_data_factory.py`: `seed:test`, `seed:large`, `seed:performance`, `seed:cleanup`; tested. |
| 4 UI automation Lab | Partial | Beginner/intermediate/advanced controls are executable with stable meaningful locators and expected results. | Scenario pages exist; challenge components and Playwright fixtures remain. |
| 5 Functional testing Lab | Partial | Positive, negative, boundary, equivalence, decision-table, state, CRUD, and workflow cases are executable and expected outcomes are explicit. | Scenario catalog exists; executable case runner remains. |
| 6 Authentication Lab | Partial | Registration, login, logout, policy, lockout, sessions, reset, token, remember-me, and MFA cases are implemented and tested. | Registration, hashing, sessions, CSRF, and lockout exist; reset/tokens/MFA remain. |
| 7 Authorization/RBAC Lab | Partial | Role permissions and object-level checks are enforced consistently in UI/API and cross-account escalation tests pass. | Ownership and role checks exist; complete permission matrix and role fixtures remain. |
| 8 REST API Lab | Partial | Documented auth, user, job, application, candidate, and interview APIs have consistent schemas/statuses/pagination. | Lab catalog/orders and core APIs exist; full resource contract remains. |
| 9 API contract/integration | Partial | Schema, headers, auth, error, pagination, compatibility, and integration tests run independently of UI. | Integration tests exist; standalone broad contract suite remains. |
| 10 Fault injection | Missing | Admin/QA-only bounded failures, delays, and probabilities work only in local/test environments and reset safely. | Not implemented. |
| 11 Database testing Lab | Partial | Read-only inspection, constraints, joins, transactions, rollback, migration, cascade, and concurrency exercises are executable. | Lab order constraints and transactions exist; inspection/concurrency exercises remain. |
| 12 File testing Lab | Partial | File type/MIME/size/corruption/duplicate/name/access/delete/replace cases are executable and ownership-safe. | Upload parsing/limits/download exist; replacement/delete/MIME fixtures remain. |
| 13 Accessibility Lab | Partial | Main UI remains accessible and isolated challenge pages cover keyboard, ARIA, contrast, focus, and error cases. | Semantic labels/live regions exist; challenge defects and axe suite remain. |
| 14 Visual regression | Missing | Baselines, current screenshots, deterministic comparison, and responsive regression reports exist. | Not implemented. |
| 15 Responsive/compatibility | Partial | Required viewport matrix runs across Chromium, Firefox, and WebKit with stable assertions. | Responsive CSS and scenario metadata exist; browser matrix remains. |
| 16 Network/resilience | Partial | Offline, latency, timeout, retry, reconnect, partial failure, and stale-state cases are controllable and tested. | Provider fallback exists; controlled network lab remains. |
| 17 Performance Lab | Missing | Bounded fast/normal/slow/timeout/database/large-response/CPU endpoints exist with safe limits. | Not implemented. |
| 18 Load/stress/spike/soak | Missing | Local/staging-only k6 profiles produce latency/error/throughput reports without production targeting. | Not implemented. |
| 19 Concurrency/race | Partial | Deterministic competing updates and duplicate operations produce documented outcomes. | SQLite order transaction is protected; broader concurrent suites remain. |
| 20 Idempotency | Partial | Repeated application, interview, notification, and order requests are safely idempotent. | Lab orders are idempotent; other operations remain. |
| 21 Security Lab | Partial | Isolated security exercises cover auth, CSRF, headers, cookies, uploads, leakage, and authorization with automation. | Core protections and tests exist; isolated exercises and automation remain. |
| 22 Rate limiting/abuse | Missing | Bounded endpoint limits return `429` and `Retry-After`, recover after the window, and have burst tests. | Not implemented. |
| 23 Cache/state | Partial | Cache hit/miss/stale/invalidation and UI state transitions are observable and tested. | Job search cache exists; dedicated cache controls/tests remain. |
| 24 Real-time/WebSocket | Missing | Authenticated real-time updates handle reconnect, duplicates, ordering, and multiple tabs. | Not implemented. |
| 25 Observability | Partial | Request/correlation IDs, safe structured logs, timings, audit events, `/health`, and `/ready` exist. | Health/audit exist; middleware, readiness, timings, and structured logs remain. |
| 26 Feature flags | Partial | Flags support default, role, environment, on/off, and rollback behavior with tests. | Schema exists; evaluation and controls remain. |
| 27 Admin QA control center | Partial | `/qa-lab` contains guarded executable controls for all requested categories. | Protected Lab pages/runs exist; full control center and environment guard remain. |
| 28 Automation framework | Missing | `tests/` has UI/API/integration/DB/a11y/visual/security/performance suites, fixtures, page objects, and reusable assertions. | Not implemented. |
| 29 CI/CD | Missing | GitHub Actions run validation gates and publish safe reports/artifacts. | Not implemented. |
| 30 Quality dashboard | Partial | Test-run IDs, totals, status, duration, pass rate, failures, thresholds, and artifacts appear in a dashboard. | QA run persistence/summary exist; artifact ingestion remains. |
| 31 SEO/web quality | Partial | Titles, descriptions, canonicals, robots, sitemap, broken-link, redirect, semantic, and performance checks pass. | Titles/semantic pages exist; SEO endpoints and tests remain. |
| 32 Privacy/data handling | Partial | Secrets/passwords/tokens never leak, logs are masked, ownership is enforced, and test/prod data boundaries are documented and tested. | Hashing, ownership, and secret ignore rules exist; log masking/data boundary tests remain. |
| 33 Failure/recovery | Partial | Database/session/upload/notification/provider failures have predictable recovery UI and tests. | Provider/cache/upload/API errors exist; database/session/notification recovery cases remain. |
| 34 Documentation | Partial | All requested guides explain setup, practice, expected results, safety, and commands. | Architecture, deployment, Lab, ATS, and testing docs exist; dedicated category guides remain. |
| 35 Coverage matrix | Missing | Every feature maps to UI/API/DB/integration/a11y/security/performance/negative/boundary/regression evidence. | This ledger defines completion; feature matrix remains to be generated after the remaining surfaces stabilize. |

## Validation commands

```powershell
cd "D:\Resume builder\prototypes\python"
.\.venv\Scripts\python.exe -m py_compile app.py auth_service.py lab_service.py test_data_factory.py
node --check static\app.js
.\.venv\Scripts\python.exe -m unittest -v test_careercraft.py
```

Heavy load, stress, spike, and soak tests must target only local or dedicated staging endpoints. The public Render instance is not a load-test target, and its free SQLite storage is ephemeral.
