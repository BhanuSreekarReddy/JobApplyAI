# JobApplyAI — Hosted service, accounts & admin: design proposal

Status: PROPOSAL for review — nothing implemented yet.
Settled up front: (1) the app becomes a **hosted service**; (2) **admins are users seeded by email**.
Everything below answers the remaining product questions for that direction, grounded in the current codebase (`backend/app/models.py`, `agent/profile.py`, README).

## Current state (what the decisions build on)

- No user concept anywhere. Tables: `runs` (with `profile_json` extracted per run), `job_matches`, `applications`, `companies`, plus the seen store. SQLAlchemy throughout.
- "Profile" today is not an account — it is extracted from the uploaded resume on every run (name, email, phone, LinkedIn, GitHub, titles, skills, years of experience, summary) and snapshotted onto the run.
- Companies (~340 starter boards) are one global shared list; users can add/disable/remove.
- All API keys are global server config (`.env`): `OPENAI_API_KEY`, Adzuna, USAJobs, `SMTP_*`.
- Apply channels: email via SMTP, prepared packets, and **browser pre-fill** — a Playwright Chromium window on the *user's own machine*, using a persistent browser profile in `data/browser-profile/`; the app never submits, the user reviews and clicks Submit.

## Decisions

### 1. Public home page (no login)
Marketing/explainer only: what it does, how it works, live counts it can honestly show (companies searched, openings indexed), screenshots or a canned demo run using `examples/sample-resume.txt`. **No real search while logged out** — a run costs compute and (with a key) OpenAI spend, and anonymous runs are an abuse magnet. CTA is signup.

### 2. Where the gate sits
Account required to start a run (resume upload / search) and for everything downstream (review queue, tracker, seen, apply). Logged-out visitors get the home page and the demo only. This matches "profile required to use the application" and keeps every run attributable to an owner, which the data model needs anyway.

### 3. Signup & login (v1)
- Email + password. **Email verification required before the first run** (unverified accounts can log in and complete their profile, nothing more).
- Password reset via emailed link (single-use, expiring token).
- Google OAuth is a phase-2 fast follow, not v1 — email/password is also what makes admin seeding by email work cleanly.
- Passwords hashed with argon2id (bcrypt acceptable). Auth via secure, HttpOnly, SameSite=Lax session cookies; login endpoint rate-limited.

### 4. Profile = two layers
- **Account**: email, password hash, role (`user` | `admin`), created/last-login, active flag.
- **Job-seeker profile** (new, persistent, one per user in v1): exactly the fields the extractor already produces — name, contact email, phone, LinkedIn, GitHub, titles, skills, years of experience, summary, plus preferred locations. Created/updated from the resume upload flow; editable in a Profile page.
- Runs keep snapshotting `profile_json` as today, so history never changes when a profile is edited. Multiple resumes/profiles per user: phase 2.

### 5. Admin (seeded by email) — concrete powers for v1
- Seed list in server config: `ADMIN_EMAILS` (comma-separated). On signup, an account whose email matches gets role `admin`. Seeded admins cannot be demoted or deleted through the UI.
- Admins can: list/search users; see account metadata and usage counts (runs, applications — **not** resume contents or cover letters; that line is deliberate); deactivate/reactivate a user (blocks login, keeps data); delete a user and all their data; open/close registration; manage the global company list.
- An admin can promote another user to admin (audited); the seed list remains the bootstrap of trust.
- Every admin action writes an **audit log** entry (who, what, target user, when).

### 6. Data isolation & migration
- Add `user_id` to `runs`, `applications`, the seen store, and user-added `companies`. Every query is scoped to the current user; another user's object returns **404, not 403** (don't leak existence).
- **Companies**: the ~340 starter boards stay global (admin-managed). Companies a user adds are *theirs* (owner = user) in v1; an admin can promote a user company to global. (Today's single shared list would otherwise let any user delete everyone's companies.)
- **Migration**: hosted deployment starts from a fresh database. A one-time script assigns any pre-existing local rows to the first account registered with an email from `ADMIN_EMAILS` (the owner), so the existing local data is not orphaned.
- **Database**: move from SQLite to Postgres for the hosted service — the code is already SQLAlchemy, so this is a connection-string and migration change, not a rewrite. SQLite stays fine for local dev/tests.

### 7. Keys, costs, quotas
- All keys become **service-owned** (server env only): OpenAI, Adzuna, USAJobs, SMTP. Per-user keys are out of scope for v1.
- Because the service now pays for OpenAI calls, add per-user quotas: runs per day and the existing per-run `max_applications` cap (default 8) become the cost controls; admins can raise a user's quota.
- Application emails send from the service SMTP with **Reply-To set to the user's email**, so employer replies reach the applicant, not the service inbox.

### 8. Browser pre-fill in hosted mode — streamed, ephemeral sessions (revised after review discussion)
Pre-fill does NOT need the user's own machine — Playwright runs wherever Chromium runs. The hosted design is a **streamed browser session**: the form is filled in a server-side Chromium and streamed into the user's browser tab; the user watches it fill, takes control to answer the highlighted questions (EEO, work authorization, CAPTCHA) and clicks Submit themselves. The **never-submit rule is unchanged** — the agent never submits.
- **No-login forms** (Greenhouse, Lever, Ashby and most ATS forms are public) are fully supported this way.
- **Login-gated career sites**: allowed only in an **ephemeral session** — the user types their password into the streamed session themselves, the service never stores passwords or session cookies, and the whole browser profile is **wiped when the session closes**. There is no server-side equivalent of today's persistent `data/browser-profile/`.
- Accepted trade-offs, stated plainly: (a) fills come from a datacenter IP, so ATS bot detection will throw more CAPTCHAs than the local version does — the user clears them during review; (b) a streamed session costs server resources per concurrent user, so prefill sessions count against the per-user quota (decision 7); (c) local mode keeps the existing on-machine prefill exactly as today.
- Still phase 2: an optional installable local helper/extension that runs the same formfill code in the user's own browser (best fidelity, no datacenter-IP friction) for users who prefer it.

### 9. Security & privacy baseline (v1, non-negotiable)
HTTPS only; session cookies Secure/HttpOnly/SameSite=Lax; CSRF protection on state-changing requests; resume uploads size-capped and stored per-user (no shared uploads directory); account page offers **data export and account deletion** (deletion removes runs, applications, seen records, resume files); admin UI shows metadata only (decision 5); login/signup rate limits; secrets only in server env, never in the repo.

### 10. MVP phasing
- **Phase 1 (v1)**: public home page + demo; signup/login/verify/reset; persistent profile; `user_id` isolation + migration script; Postgres; seeded-email admins with user list/deactivate/delete + audit log; global-starter/per-user companies; service keys + per-user quotas; hosted browser pre-fill via streamed ephemeral sessions (decision 8; local mode keeps on-machine prefill).
- **Phase 2**: Google OAuth; multiple profiles/resumes; admin company-management UI and promote-to-global; optional local helper/extension for pre-fill; per-user API keys if demand appears.

## Open items to decide
- Hosting target and budget (drives the OpenAI quota numbers).
- Whether registration is open to anyone at launch or invite-only (the admin "open/close registration" toggle covers either; default recommendation: invite-only until quotas are proven).
