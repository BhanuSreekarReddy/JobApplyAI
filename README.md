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
| `SMTP_*` | Auto-send applications when a job lists an email |
| `USAJOBS_API_KEY` / `USAJOBS_USER_AGENT` | US federal jobs ([free key](https://developer.usajobs.gov/apirequest/); the user agent is your registered email) |
| `FORMFILL_HEADLESS` | Run the pre-fill browser without a window (testing only; you can't review a headless form) |
| `JOBAPPLY_DATA_DIR` | Store the database, uploads, and browser profile outside `./data` |

Without keys, the agent still searches every company board on the Companies tab plus the public remote job sites.

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
