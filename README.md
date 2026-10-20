# JobApply Agent

A local AI agent that reads a resume, searches public job boards for roles that match the candidate’s experience, ranks them for the locations you care about, then prepares (and optionally submits) applications.

It does **not** log into LinkedIn, Indeed, or other accounts. Applications go out through:

- **Email**, when a posting lists a contact and SMTP is configured
- **Prepared packets** otherwise: tailored cover letter + official apply URL, ready to submit on the employer’s ATS

## Run it

You need Python 3.11+ and Node 18+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt

cd frontend
npm install
cd ..

./scripts/dev.sh
```

Open [http://localhost:5173](http://localhost:5173). Upload a resume (or `examples/sample-resume.txt`), add locations such as `San Francisco, Remote`, and start the agent.

## Optional keys

Copy `.env.example` to `.env`.

| Variable | What it unlocks |
| --- | --- |
| `OPENAI_API_KEY` | Stronger profile extraction and cover letters |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | Location-specific search via [Adzuna](https://developer.adzuna.com) |
| `SMTP_*` | Auto-send applications when a job lists an email |
| `USAJOBS_API_KEY` | US federal jobs |

Without keys, the agent still searches Remotive, Jobicy, Himalayas, and Arbeitnow.

## How the agent works

1. Parse PDF / DOCX / TXT resume
2. Extract titles, skills, years, contact info
3. Query public job APIs
4. Score listings against the resume and locations
5. Draft a cover letter per match
6. Apply by email when possible, otherwise store a ready packet
