# JobApply Agent

A local AI agent that reads a resume, searches hundreds of companies’ own job boards plus public job sites for roles that match the candidate’s experience, ranks them for the locations you care about, then prepares (and optionally submits) applications.

It does **not** log into LinkedIn, Indeed, or other accounts. Applications go out through:

- **Email**, when a posting lists a contact and SMTP is configured
- **Browser pre-fill** (optional): a Playwright-driven Chromium window opens the employer’s apply page, fills the form, and leaves it for you to review and submit
- **Prepared packets** otherwise: tailored cover letter + official apply URL, ready to submit on the employer’s ATS

## Run it

You need Python 3.11+ and Node 18+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
playwright install chromium

cd frontend
npm install
cd ..

./scripts/dev.sh
```

Open [http://localhost:5173](http://localhost:5173). Upload a resume (or `examples/sample-resume.txt`), add locations such as `San Francisco, CA, Remote`, and start the agent.

Locations are comma-separated; a state or country after a city belongs to that city (`Newark, CA, Remote` is two places: Newark, California and remote). “Remote” means remote roles open to your country, so a US candidate doesn’t get “Remote – EMEA” jobs.

## Where jobs come from

**Company job boards (no keys needed).** Most US tech companies post jobs on Greenhouse, Lever, or Ashby, which publish every customer’s openings through a free public API. The app ships with a verified starter list of ~340 companies (`backend/app/data/companies.json`, about 24,000 US openings) and searches all of them on every run in roughly 15 seconds (2 seconds when cached).

On the **Companies** tab you can:

- **Add a company** by name (`Lucid Motors`), its careers page (`https://lucidmotors.com/careers`), a board link (`https://jobs.lever.co/spotify`), or a **Workday** site (`https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite`). The app finds the board behind the page, including careers sites that embed Greenhouse.
- **Turn companies off** or remove ones you added.
- **See board health.** If a company leaves its ATS, the next run looks for its new board (e.g. Lever → Ashby) and updates it, or flags it as *Board gone*.

Rebuild the starter list with `cd backend && ../.venv/bin/python scripts/build_companies.py`; it keeps only boards whose company name matches and that list US jobs.

**Public job sites.** Remotive, Jobicy, Himalayas, and Arbeitnow (remote boards; filtered to your locations), plus Adzuna and USAJobs when you add keys.

LinkedIn and Indeed aren’t used: neither offers a public job-search API, and automating them breaks their terms.

## Apply to a specific job

Paste any job link into **Apply to a specific job** on the Agent tab, e.g. `https://lucidmotors.com/careers/search/5197938007` or a `jobs.lever.co/...` posting. The app identifies the job on the company’s ATS, writes the cover letter, and pre-fills the application with the profile from your most recent run. Careers-page links (not a single job) are rejected with a hint to add the company instead.

## Optional keys

Copy `.env.example` to `.env`.

| Variable | What it unlocks |
| --- | --- |
| `OPENAI_API_KEY` | Stronger profile extraction and cover letters |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | City-level search across many job sites via [Adzuna](https://developer.adzuna.com) (free key) |
| `SMTP_*` | Auto-send applications when a job lists an email; in hosted mode, also send account verification and password-reset email |
| `USAJOBS_API_KEY` / `USAJOBS_USER_AGENT` | US federal jobs ([free key](https://developer.usajobs.gov/apirequest/); the user agent is your registered email) |
| `FORMFILL_HEADLESS` | Run the pre-fill browser without a window (testing only; you can't review a headless form) |
| `JOBAPPLY_DATA_DIR` | Store the database, uploads, and browser profile outside `./data` |
| `JOBAPPLY_HOSTED` | `true` turns on hosted mode: user accounts, seeded admins, invite-only registration (see below) |
| `ADMIN_EMAILS` | Comma-separated emails that become admins when they register |
| `JOBAPPLY_BOOTSTRAP_INVITE` | One-time invite code created at startup so the first admin can register |
| `MAX_RUNS_PER_DAY` | Runs a non-admin user can start per day (default 20) |
| `SESSION_COOKIE_SECURE` | `true` when served over HTTPS, so the session cookie only travels over secure connections |
| `PUBLIC_BASE_URL` | Public origin (scheme + host) the app is opened at; verification and password-reset emails build their links from it (default `http://localhost:5173`; set it to the public address when hosted) |
| `DATABASE_URL` | Any SQLAlchemy URL; hosted deployments should use Postgres instead of the local SQLite file |

Without keys, the agent still searches every company board on the Companies tab plus the public remote job sites.

## Running it hosted (accounts + admins)

Hosted mode turns the app into a small multi-user service:

- The home page is public and shows aggregate counts only. Everything else — starting a run, applications, profiles — requires an account.
- Registration is **invite-only**: an admin creates invite codes in the admin UI, and a new user needs one to sign up.
- Email verification is required before a user can sign in (registration sends the link; sign-in stays blocked until the address is verified). Users can resend the verification link themselves from the sign-in page, rate-limited to one per minute and five per day. Password reset works by emailed link. Both use the same one-time token mechanism.
- Each user sees only their own runs, applications, and profile. Companies from the starter list are shared; companies a user adds are theirs alone.
- Admins can manage users and invites. Admins named in `ADMIN_EMAILS` can't be demoted, deactivated, or deleted.
- Hosted pre-fill runs in a streamed, server-side browser session: the user watches the form being filled, can take control, and always submits it themselves — the app never submits. The session uses a fresh, temporary browser profile that is deleted when the session closes.

### Setup

1. **Set the environment.** Locally, copy `.env.example` to `.env` and fill in the hosted block. On a cloud host (Render, Railway, Fly.io, a VPS, …) don't deploy the `.env` file at all — enter the same variables in the host's **Environment Variables / Secrets** settings. The host stores them encrypted and passes them to the app at startup; secrets never go into git.

   ```
   JOBAPPLY_HOSTED=true
   ADMIN_EMAILS=you@example.com
   JOBAPPLY_BOOTSTRAP_INVITE=<pick-a-one-time-code>
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=<the sending account>
   SMTP_PASSWORD=<its App Password — see step 2>
   SMTP_FROM=<same address as SMTP_USER>
   DATABASE_URL=postgresql+psycopg://user:password@host:5432/jobapply
   SESSION_COOKIE_SECURE=true
   ```

2. **Set up the sending email account.** Account email — verification links and password resets — goes out over SMTP using the `SMTP_*` settings above. Any SMTP provider works; a dedicated Gmail account is the simplest free option:

   - Create the account (this project uses `jobapplyai.app@gmail.com`).
   - Turn on **2-Step Verification** (Google Account → Security).
   - In the same Security section, open **App passwords**, create one named e.g. "JobApplyAI", and copy the 16-character code it shows — Google displays it only once.
   - Use that code as `SMTP_PASSWORD`. (The account's normal password won't work for SMTP.) Gmail's free sending limit is about **500 recipients per day**, which is ample for invite-only verification and reset mail. For higher volume, Resend or Amazon SES plug into the same `SMTP_*` variables with no code change.
   - If `SMTP_HOST` is left unset, nothing is sent: every outgoing email is only recorded in the `email_outbox` table in the database, which is handy for local testing.

3. **Start the app** the same way as local mode. Chromium must be installed on the server (`playwright install chromium`) for streamed pre-fill sessions; without it, pre-fill requests return an error while everything else works.

4. **Create the first admin.** Open the app, register with an email listed in `ADMIN_EMAILS`, and use the `JOBAPPLY_BOOTSTRAP_INVITE` code as your invite. Verify the account from the emailed link — that account is now an admin. From the admin UI you can mint further invite codes for everyone else, and the bootstrap code can be removed from the environment once the first admin exists.

## Browser pre-fill

Enable **Pre-fill employer application forms** when starting a run, or click **Fill form in browser** on any packet later. The agent opens the apply URL in a Chromium window, clicks through an “Apply” link if the form is on another page, and works on any ATS (Greenhouse, Lever, Ashby, custom career sites, embedded iframes) by matching field labels.

- **Fills:** name, email, phone, LinkedIn, GitHub/portfolio, location, years of experience, resume upload (renamed to `First_Last_Resume.ext`), and cover letter text or file.
- **Location** comes from the first non-remote entry in *Locations*. Include the state (`Newark, CA`, not `Newark`) so the agent can pick the right autocomplete suggestion; if several places match equally it leaves the field for you instead of guessing.
- Only the application form is touched; other forms on the page (job alerts, newsletter sign-ups) are ignored.
- **Never answers:** demographic/EEO questions, work authorization, visa sponsorship, salary, referral source, consents, or anything else it can’t take from the resume. Required ones are outlined in orange.
- **Never submits.** A banner on the page lists what was filled and what needs you. Review, answer the highlighted questions, complete any CAPTCHA, click the employer’s Submit button, then click **Mark submitted** in JobApply.

The browser uses a persistent profile in `data/browser-profile/` (git-ignored), so if a career site requires an account, sign in once in that window and click **Fill form in browser** again.

## How the agent works

1. Parse PDF / DOCX / TXT resume
2. Extract name, titles, skills, contact info, and years of experience (from stated years or employment date ranges)
3. Search every enabled company board and the public job APIs in parallel
4. Keep roles in your job family and locations, then read full descriptions for the top 200
5. Score them against the resume (skills, title, location, seniority; entry-level roles are down-ranked for experienced candidates)
6. Draft a cover letter per match
7. Apply by email when possible, otherwise store a ready packet
8. Optionally pre-fill each packet’s employer form in a browser window for you to review and submit

## Tests

```bash
pip install -r backend/requirements-dev.txt
cd backend && ../.venv/bin/python -m pytest
```

Tests use mocked HTTP and a temporary data directory, so they run offline and never touch `data/`.
