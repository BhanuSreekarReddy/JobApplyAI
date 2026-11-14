# ADR: JobApplyAI Public Hosting — Domain, Fly.io, and GitHub-Driven Deployment

- Status: Accepted (hosting and deployment decisions); domain registration step still pending completion
- Date: 2026-11-10
- Deciders: Pratik Singh (Bhanu Sreekar Reddy, repo owner, to be informed)

## Context

JobApplyAI is moving from a local tool to a hosted, invite-only web service (accounts/admin implemented in PR #8; design in PR #7, merged). The hosted service needs: (1) a public domain name, (2) a hosting platform, and (3) a deployment path controlled from GitHub. Launch scale is small (invite-only beta), so running cost is a primary constraint — target under $20/month. The app is heavier than a typical small web app because hosted pre-fill runs real, streamed Chromium browser sessions, which drives a 2 GB RAM machine size. The application was designed to be multi-instance safe (sessions validated against one shared Postgres via DATABASE_URL), so no platform choice locks us in.

## Decision 1 — Domain: jobapplylive.com at Porkbun

- Domain chosen: jobapplylive.com. Rationale: jobapplyai.com is taken by a different product, as are jobapply.io, jobsapply.app, and jobapplypilot.com (a live competitor). The "live" name fits the product — users watch the pre-fill session live. Alternatives verified available the same day: getjobapplyai.com and wolfapply.com, each $11.81/yr at Porkbun.
- Registrar: Porkbun. Price verified 2026-11-10: $11.81 for the first year, $11.81/yr renewal, not a premium/aftermarket name, free WHOIS privacy included, no extras or bundles.
- Account: Porkbun account created 2026-11-10 (username psingh2233, email pratik.iem01@gmail.com) and email-verified. Registrant: Pratik Singh, 3315 Montgomery Dr Apt 470, Santa Clara, CA 95054, USA.
- Status: NOT YET REGISTERED. Three checkout attempts on 2026-11-10 produced orders #12502120, #12502168, and #12502208, all marked "NOT PROCESSED / REFUNDED" with $0.00 charged (verified directly in the account: domain list empty, cart intact at $11.81). Causes: the Stripe Link approval never completed a charge inside the checkout session, and fresh Stripe checkout sessions timed out during browser-takeover handoffs. Deferred by Pratik on 2026-11-10 ("let's come back to it later").
- Resume plan: start one completely fresh checkout and complete payment in a single session while it is live; do not reuse the Visa ending in 9224 (a virtual card that cannot receive refunds, and the card involved in the failed attempts). After registration, point DNS at Fly.io (Fly custom-domain flow: add the domain in Fly, set the DNS records Fly specifies at Porkbun, Fly issues the HTTPS certificate automatically).

## Decision 2 — Hosting: Fly.io (not Render, not AWS, not Kubernetes at launch)

Options priced from published rates, checked 2026-11-10, for this app (web app + Postgres):

| Option | Realistic monthly cost | Notes |
|---|---|---|
| Fly.io (lean setup, Decision 3) | ~$17–18 | Usage-billed per second; cheapest viable |
| Fly.io with Managed Postgres | ~$45+ | Managed Postgres Basic alone is $38/mo |
| Render | ~$31 | $25 Standard web service (2 GB) + $6 Postgres; flat and predictable, backups included |
| AWS ECS Fargate | ~$65–90 | Task ~$36 + load balancer ~$16–18 + RDS ~$12–15 (+ possible NAT gateway ~$33) |
| AWS EKS (Kubernetes) | ~$200–350 | $73/mo control plane before any workload, plus nodes, ALB, NAT, RDS |

Decision: Fly.io for launch. Kubernetes is not required: the app's multi-instance safety is a design property (shared Postgres), not a platform requirement, and moving later (Render, Fargate, EKS) is a redeploy, not a rewrite, because the deployment is a standard container plus environment variables.

- Fly.io account: created 2026-11-10 with pratik.iem01@gmail.com (Personal organization, $1 free trial credit noted). Password set by Pratik via secure entry.
- Outstanding on the account: (a) email confirmation — Pratik must click the confirm link in the Fly email sent to pratik.iem01@gmail.com; without it the platform is not fully usable; (b) a payment method must be added before deploying beyond the trial credit. Both are Pratik's actions.

## Decision 3 — Fly.io configuration: lean setup under $20/month

- One app Machine: shared-cpu-2x, 2 GB RAM, always on — $13.39/mo at published rates. 2 GB is driven by the Chromium pre-fill sessions. Auto-stop when idle is enabled where compatible, which can only lower this (Fly bills per second while running).
- Database: Postgres run on a small Fly Machine (shared-cpu-1x, 256 MB, ~$2.19/mo) plus a 10 GB volume at $0.15/GB/mo (~$1.50) — ~$3.70/mo total. Fly Managed Postgres ($38/mo) is explicitly rejected at this scale.
- Traffic: egress $0.02/GB in North America/Europe; invite-only volume is pennies per month.
- Expected total: ~$17–18/month. Caveat: Fly bills actual usage, so this is an estimate from published rates, not a guaranteed cap. The configuration choices that would break the $20 line: Managed Postgres, a second always-on app Machine, or a larger Machine size.
- Accepted trade-off: with a self-run database, backups and database maintenance are our responsibility, not Fly's. Acceptable at invite-only beta scale; revisit if the user base grows.

## Decision 4 — Deployment: GitHub Actions controls Fly.io deploys

Flow: merge to main → GitHub Actions builds and deploys to Fly → live. Rollback is a revert on GitHub.

Files to add to the repository:

1. Dockerfile — packages the app including Chromium (required for streamed pre-fill sessions).
2. fly.toml — app name, primary region (San Jose, sjc — closest to the operators/users), internal port, Machine size from Decision 3, auto-stop settings.
3. .github/workflows/fly-deploy.yml — on push to main: checkout, install flyctl via the official superfly/flyctl-actions setup action, then flyctl deploy --remote-only (builds on Fly's remote builders) with the deploy token injected from GitHub secrets.

Authentication between GitHub and Fly:

- Create an app-scoped deploy token on Fly: fly tokens create deploy (scoped to this one app only — it cannot touch other apps or the account). Never use a personal/full-access token in CI.
- Store it in GitHub as the Actions repository secret FLY_API_TOKEN. The token never appears in code.

Secrets and configuration:

- App secrets live on Fly, set once with fly secrets set — they do not travel in the repo or the .env file: SMTP_HOST (smtp.gmail.com), SMTP_PORT (587), SMTP_USER / SMTP_FROM (jobapplyai.app@gmail.com), SMTP_PASSWORD (the Gmail App Password for the dedicated jobapplyai.app@gmail.com account), JOBAPPLY_HOSTED (true), ADMIN_EMAILS, JOBAPPLY_BOOTSTRAP_INVITE, DATABASE_URL (pointing at the Decision 3 Postgres).
- Email context: verification and password-reset mail is sent from the dedicated Gmail account jobapplyai.app@gmail.com over Gmail SMTP with an App Password (free Gmail cap ~500 recipients/day — sufficient at invite-only scale).

Ownership constraint (important): the repository is BhanuSreekarReddy/JobApplyAI, owned by Bhanu Sreekar Reddy. Pratik has push access but not admin. Only a repo admin can add GitHub Actions secrets, so either (a) Bhanu adds the FLY_API_TOKEN secret, or (b) the deploy workflow runs from Pratik's fork (gitPratikSingh/JobApplyAI). To be decided with Bhanu. PR #8 (hosted accounts/admin) must merge before the first real deploy.

## Implementation sequence

1. Pratik completes the Porkbun payment for jobapplylive.com (fresh single-session checkout, different card) — domain registered for 1 year.
2. Pratik confirms the Fly.io email and adds a payment method to the Fly account.
3. Add Dockerfile, fly.toml, and .github/workflows/fly-deploy.yml to the repo (after PR #8 merges; Bhanu reviews).
4. Create the Fly app and the Postgres Machine + volume; set all secrets with fly secrets set.
5. Create the app-scoped deploy token; Bhanu (or the fork) stores it as the FLY_API_TOKEN GitHub secret.
6. First deploy via a push to main; verify the app responds on its fly.dev address.
7. Attach jobapplylive.com in Fly, set the DNS records at Porkbun, verify HTTPS.
8. Bootstrap the first admin with JOBAPPLY_BOOTSTRAP_INVITE, register, then create a regular invite from Admin → Invites and test the full user flow.

## Consequences

Positive: total running cost ~$17–18/mo plus $11.81/yr for the domain; deployment is fully controlled from GitHub (merge = deploy, revert = rollback); no dependency on any individual's laptop being on; the container-based setup stays portable to Render/Fargate/EKS later.

Negative/accepted: self-run Postgres means we own backups; a single app Machine is a single point of failure during beta; GitHub-to-Fly secret setup needs the repo owner's participation.

Risks: Fly usage billing can drift above the estimate if traffic or Machine sizes grow; the $20 target must be re-checked after the first full month's invoice.

## Open items

- Domain payment completion (deferred 2026-11-10, cart intact at Porkbun).
- Fly email confirmation and payment method (Pratik).
- Bhanu decision: who adds the FLY_API_TOKEN secret — upstream repo vs fork workflow.
- PR #8 merge (Bhanu's review in progress).
- Bhanu's premium beta v2 idea (OpenAI key integration for a small group) — separate decision, not covered by this ADR.
- The JobApplyAI verification-email delivery test is still blocked by Muse's email_smtp permission (Deny) for the local test only; production sending from Fly uses the app's own SMTP config and is unaffected.
