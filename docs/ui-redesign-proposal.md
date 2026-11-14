# Proposal: Organize the UI Around the Job Pipeline, Not the System Pipeline

- Status: Proposed
- Date: 2026-11-14
- Deciders: Pratik Singh (Bhanu Sreekar Reddy, repo owner, to review)

## Context

The app's navigation mirrors how the system is built, not what the user is doing. The tabs are Run, Tracker, and Companies: run the agent, review what it produced, then track outcomes in a second place. But the user has one job throughout — moving a job from *found* to *applied* to *interview* — and the same job appears as different cards in different places as it moves: a search result, a review packet, a tracker row. Each place has its own card, its own buttons, and its own idea of the job's state.

This is not hypothetical. Two symptoms surfaced in real use on 2026-11-14:

- Approving a job in the review list teleported the user into the Applications tab and opened the form (the auto-navigation shipped in the improvement-notes round). The fix in PR #16 keeps the user in place — but it treats the symptom. The jump was only possible because "review" and "applications" are separate rooms for one continuous list.
- The review queue lists only pending-review applications, so an approved job vanished from the list the moment it was approved; PR #16 additionally had to pin approved cards into the queue so users could keep working with them. A list that drops the item you just acted on is a sign the list boundaries are drawn in the wrong place.

The proposals below are presentation and information-architecture changes. They are independent — accepting one does not require accepting the rest — and the phasing at the end orders them by cost.

## Proposal 1 — One pipeline, one list

Merge run results, the review queue, and the tracker into a single Jobs list. A job is one card whose stage changes: New match → Needs review → Ready → Applied → Recruiter reply → Screen → Interview → Offer → Closed. The tabs become stage filters with counts ("Needs review (6)", "Applied (12)") over that one list.

A job can never "go" anywhere else, because there is nowhere else. The stage model already exists — the tracker computes pipeline stages today (including the "Not applied" default and the rule that a stage advances only on a real submission) — this proposal points the whole UI at it instead of only the tracker tab.

## Proposal 2 — A "Needs you" inbox as the home view

The product's core loop is queuing human decisions. The home view should open on those queues, with counts:

- Jobs awaiting review.
- Filled forms awaiting the user's submit click (pre-fill never submits — the user does).
- Follow-ups due (the tracker already computes a 7-day follow-up nudge for applied jobs).

Running a search becomes a "Find new jobs" action that feeds the inbox, not the home page itself. Today the app opens on the run form — the machine's starting point, not the user's.

## Proposal 3 — Five-second triage cards

Real usage is batch review: scanning a list and deciding approve or skip per job in seconds. The card should lead with what decides that:

- Match score, and why it matched (top matching skills).
- What's missing (gaps against the posting).
- Salary and location where known, and the posting's age.

The cover letter collapses to a one-line preview (partly true today — approval already collapses it). Add keyboard triage (A approve / S skip / arrow keys to move) and bulk actions (approve everything above a chosen score, skip everything below one) so a batch of twenty jobs is a keyboard pass, not twenty mouse trips.

## Proposal 4 — One primary action per stage

A card's buttons today are a flat row of equals: Approve, Save letter, Skip — then, after approval, Fill form in browser, Mark submitted, Edit cover letter. Every button looks equally important, so the user re-derives "what do I do next with this one?" on every card.

Make the stage's next action the one primary button (Approve on a pending job, Fill form in browser on a ready one, Mark submitted after filling), and demote the rest to a quieter secondary row or an overflow menu. The set of available actions does not change — their hierarchy does.

## Proposal 5 — User language, not system language

Rename to the words users think in: Run → **Find jobs**, Tracker → **My applications**, and "packets" → jobs/applications throughout the UI copy. Companies becomes a watchlist whose matches feed Find jobs, not a peer tab of the pipeline. No behaviour changes — labels only.

## Proposal 6 — An answers-coverage meter

How much of an application gets filled automatically is decided by the saved answer bank, but its gaps only surface per application, as "Needs you" items — unanswered demographic questions are a recurring example. Show in Profile how much of a typical application the bank covers ("your answers cover ~85% of typical questions; 3 common gaps"), computed from the questions the bank is actually asked across applications, with the gaps listed so they can be answered once, as setup, instead of being rediscovered on every form.

## Proposal 7 — Run transparency

A search run takes minutes, and today it presents as an opaque queued state until results land. Show live progress while it executes: sources being searched, jobs found so far, scoring under way. The run view already reports results in stages internally; this surfaces that progress instead of a spinner, which matters more as runs get longer.

## Suggested phasing

1. **Phase 1 — copy and hierarchy (cheap):** Proposal 5 (renames) and Proposal 4 (one primary action per stage). Layout and wording only.
2. **Phase 2 — the unified list:** Proposal 1 (one Jobs list, stage filters with counts) and Proposal 3's card header (decision facts first, letter as preview). Proposal 2's inbox can ship as the default filter of this list ("Needs you" = stages that require a human action).
3. **Phase 3 — speed and polish:** Proposal 3's keyboard triage and bulk actions, Proposal 6 (coverage meter), Proposal 7 (run progress).

## Non-goals

- No backend pipeline changes are required for Phases 1–2: stages, the follow-up nudge, and the answer bank already exist; this is about what the UI points at and how it labels things.
- Hosted and local modes behave identically under every proposal — pre-fill delivery (streamed session vs local browser) stays a per-application detail, invisible to the navigation model.
- The never-submit rule is untouched: nothing here submits an application on the user's behalf, in any phase.

## Open items

- Review and accept/amend each proposal separately; implementation PRs should reference the proposal they implement.
- Proposal 2: whether the inbox is a separate home view or the unified list's default filter (Phase 2 suggests the latter — decide during that phase's design).
