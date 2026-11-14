# ADR: JobApplyAI Follow-Up Improvements — Radio-Group Filling, Production CORS, Pre-Fill Checkbox, Seen-Row Consistency

- Status: Proposed
- Date: 2026-11-13
- Deciders: Pratik Singh (Bhanu Sreekar Reddy, repo owner, to review)

## Context

Four improvement proposals that came out of building and testing the hosted service (PR #8), the hosting ADR review (PR #9), and hands-on testing on 2026-11-13. None of them is implemented yet; this document records the context and the proposed decision for each so they can be reviewed and picked up one at a time. They are independent of each other — accepting one does not require accepting the rest — except Decision 2, which blocks the public launch described in the hosting ADR.

## Decision 1 — Fill radio-group questions from the saved answer bank

Context:

- PR #8 added a per-user saved answer bank: answers a user gives during a pre-fill session can be captured (with the user's per-item confirmation) and are reused to answer the same questions on later applications.
- Radio-group questions are only half covered by that work. The form scanner detects radio groups and highlights them as questions for the user to answer, and an answer the user gives can be saved into the bank — but the fill path never selects a radio option itself. In the filler, the field classifier (`backend/app/agent/formfill.py`, `_classify`) explicitly returns no mapping for `radio`/`checkbox` fields, so a radio question is left for the user every time, even when the bank already holds their answer to the identical question.
- The visible result: applications stall on radio questions (work authorization, sponsorship, and similar yes/no or pick-one questions are very commonly radio groups on ATS forms), and the user answers the same questions again on every application — exactly the repetition the answer bank exists to remove.

Proposed decision: extend the form filler to select radio options from the stored answer bank.

- Match a radio group to a stored answer the same way other field types are matched today (question text and option text, normalized the same way the existing matching normalizes them), and click the option whose text matches the stored answer.
- If no stored answer matches, or the stored answer matches no option on the form, leave the group untouched and highlighted for the user, as today. Never guess an option.
- EEO/demographic answers remain opt-in per item, exactly as the answer bank already treats them: they are only stored with the user's per-item confirmation, and this change does not alter what gets stored — only that a stored answer can be applied.
- Covered by tests: a stored answer selects the matching radio option; an unmatched group is left alone; a stored EEO answer is applied only when it was stored under the existing opt-in rule.

Alternatives considered: leaving radios manual forever (rejected — it caps the value of the answer bank and of pre-fill itself, since radio questions appear on most applications); answering radio questions from the resume profile instead of the bank (rejected — these are the user's stated answers to specific questions, and the bank is already the reviewed store for them).

Consequences: pre-fill sessions need fewer manual answers per application; the never-submit rule is unchanged (selecting an option is filling, not submitting — the user still reviews every tab and clicks Submit themselves).

## Decision 2 — Make CORS origins environment-configured

Context:

- Raised by Bhanu while reviewing PR #9 (2026-11-13): the CORS configuration in `backend/app/main.py` allows localhost origins only (`http://localhost:5173` and `http://127.0.0.1:5173`, the Vite dev server).
- That is correct for local development and wrong for the hosted service: once the app is deployed, the browser will block the frontend on the real domain from calling the API at all. This is launch-blocking for the deployment planned in the hosting ADR, and it is easy to miss until the first real deploy because everything works in local testing.

Proposed decision: CORS origins become configuration, not code.

- A `CORS_ORIGINS` setting (comma-separated) in the existing settings/env configuration, defaulting to the two localhost origins so local development is unchanged.
- At deploy time the production origin (the jobapplylive.com frontend origin) is set through the environment, alongside the other deploy-time secrets/settings the hosting ADR already lists — no code change per environment, and no wildcard origins.

Alternatives considered: hardcoding the production domain next to the localhost entries (rejected — every future domain or staging environment becomes a code change, and an origin we later stop using stays allowed forever); `allow_origins=["*"]` (rejected — the API uses credentialed requests, and a wildcard would let any site call the API from a logged-in user's browser).

Consequences: one small change, made before the first deploy rather than debugged during it. Nothing about local development changes.

## Decision 3 — Stop showing a pre-fill checkbox that does nothing in hosted mode

Context:

- Found in testing on 2026-11-13. The brief form (the run setup form) has a checkbox: "Pre-fill employer application forms in a browser window. Nothing is submitted: you review each tab, answer the highlighted questions, and click Submit yourself." It is wired to the run's `browser_fill` flag.
- The automatic fill that flag triggers runs only in local mode: in `backend/app/main.py`, the approve flow calls the filler only when the app is **not** running hosted (`run.browser_fill and not settings.jobapply_hosted`). In hosted mode, pre-filling works differently — each prepared application has a "Fill form in browser" button that starts a streamed session on demand.
- So in the hosted product the checkbox silently does nothing: a user can tick or untick it and their run behaves identically. A control that does nothing in the product's primary form is worse than no control — users set it deliberately and then can't tell whether pre-fill is on.

Proposed decision: in hosted mode, hide the checkbox and show a short caption in its place explaining that pre-fill is per-application (each prepared application gets a "Fill form in browser" button). In local mode, keep the checkbox exactly as it is — there it works as labelled.

- Rationale for hiding rather than disabling: a disabled-but-visible checkbox still invites the question "why can't I change this?" and still reads as a setting the user is missing out on; a caption where the control would have been explains the actual flow at the point where the user is looking for it. If reviewers prefer a disabled control with an explanatory tooltip, that is an acceptable variation — the requirement is that hosted mode must not present a live-looking control that has no effect.

Alternatives considered: wiring the checkbox to something real in hosted mode (e.g. auto-starting a streamed session for every prepared application) — rejected for now: streamed sessions cost server resources per the hosted design, starting them unprompted changes the product's cost and interaction model, and it is a much bigger decision than fixing a misleading label; it can be proposed separately if wanted.

Consequences: the brief form tells the truth in both modes. No behaviour change in either mode — this is a presentation fix only.

## Decision 4 — Record the seen row on the add-from-link path too

Context:

- PR #8 round 3 fixed a 'Previously Applied' sync bug: several paths moved an application to an outcome status without recording the corresponding `SeenJob` row, so the tracker and the "previously applied" markers disagreed with what had actually happened. The fix centralized the recording in `record_application_seen` (`backend/app/agent/seen.py`), called from the paths that produce application outcomes (mark-submitted, approve/prepare, skip, and the pre-fill session outcome).
- One creation path was deliberately left out of scope at the time: `POST /api/jobs/from-link` creates an application directly in an outcome status (prepared, or the fill outcome when the user asks for the form to be filled) and does not record a seen row. This is a consistency gap, not a reported bug: a job the user adds from a link for a role they already applied to can later resurface in matching results as if it were new, because nothing recorded that it was seen.
- The stage work in PR #8 made the general principle concrete: every path that puts an application into an outcome state should leave the same records, or the tracker's state quietly diverges from reality and users notice the tracker lying before anyone notices the missing row.

Proposed decision: call the same `record_application_seen` helper from `apply_from_link`, for the same recordable statuses used by the other paths, with a regression test mirroring the round-3 tests (an application created from a link records its seen row; statuses outside the recordable set still record nothing).

Alternatives considered: leaving it as-is (acceptable only if we accept the tracker having one known inconsistent path; the cost of the fix is a few lines plus a test, and the cost of the gap is user trust in the matching/"new" markers); deduplicating at match time instead (rejected — it treats the symptom in one place and leaves the seen store incomplete for every other consumer).

Consequences: one consistent rule — an application outcome always records its seen row, whichever path produced it. No user-visible change for jobs the user genuinely has not seen before.

## Suggested sequencing

1. Decision 2 (CORS) — small and launch-blocking; do it before the first Fly.io deploy.
2. Decision 1 (radio filling) — the largest user-visible improvement; independent, can be built and tested on its own.
3. Decisions 3 and 4 — small correctness/honesty fixes; either order, both independent of the above.

## Open items

- Review and accept/amend each decision separately; implementation PRs should reference the decision they implement.
- Decision 3's hide-vs-disable presentation detail, if reviewers disagree with the recommendation above.
