# Free internet deployment

CareerCraft can run on Render's free web-service tier using the included
`render.yaml` blueprint.

## Deploy

1. Put this workspace in a GitHub repository. Do not commit `.env`, `data.db`,
   resume exports, or personal profile data.
2. Create an account at [render.com](https://render.com) and choose **New >
   Blueprint**.
3. Select the GitHub repository. Render will detect `render.yaml` and build the
   Docker service from `prototypes/python/Dockerfile`.
4. Deploy, then open the generated `onrender.com` URL.

## Free-tier limitations

- The service sleeps when idle and can take a little time to wake up.
- The local SQLite database is stored under `/tmp` and can be cleared when the
  service restarts or redeploys. Export your profile and resume versions often.
- Accounts, password hashing, user data isolation, CSRF checks, and security
  headers are included. The free service still has no durable database: `/tmp`
  is cleared on restart/redeploy, so it is not suitable for real private data.
- Ollama is intentionally local-only and will not run inside this small free
  web service. The built-in review fallback remains available.

## Before publishing

Remove any personal `data.db` from the repository and verify that `.env` is
ignored. Set a strong `RESUME_SECRET_KEY` in the hosting dashboard if you
replace the generated value. For a real deployment, replace SQLite with a
managed PostgreSQL database, enforce HTTPS-only session cookies, add backups,
monitoring, rate limits, and an e-mail provider before offering password reset.
