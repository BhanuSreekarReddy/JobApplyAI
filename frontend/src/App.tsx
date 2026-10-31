import { FormEvent, useState } from "react";
import Companies from "./Companies";
import JobLink from "./JobLink";

type Profile = {
  name?: string;
  email?: string;
  skills?: string[];
  titles?: string[];
  years_experience?: number;
  summary?: string;
};

type Job = {
  id: number;
  title: string;
  company: string;
  location: string;
  source: string;
  url: string;
  apply_url: string;
  score: number;
  reasons: string;
  remote: boolean;
  description: string;
  posted_date: string;
  is_new: boolean;
};

type Application = {
  id: number;
  title: string;
  company: string;
  status: string;
  channel: string;
  notes: string;
  apply_url: string;
  cover_letter: string;
  pipeline_stage: string;
  user_notes: string;
  follow_up_date: string;
  created_at: string | null;
};

const STAGES: { value: string; label: string }[] = [
  { value: "applied", label: "Applied" },
  { value: "reply", label: "Recruiter reply" },
  { value: "screen", label: "Screen" },
  { value: "interview", label: "Interview" },
  { value: "offer", label: "Offer" },
  { value: "rejected", label: "Rejected" },
  { value: "ghosted", label: "Ghosted" },
];

function needsFollowUp(app: Application): boolean {
  if (app.pipeline_stage !== "applied" || !app.created_at) return false;
  return Date.now() - new Date(app.created_at).getTime() > 7 * 24 * 60 * 60 * 1000;
}

type SeenJob = {
  id: number;
  title: string;
  company: string;
  url: string;
  status: string;
  created_at: string | null;
};

type Run = {
  id: string;
  status: string;
  summary: string;
  error: string;
  profile: Profile;
  jobs: Job[];
  applications: Application[];
};

type RunSummary = {
  id: string;
  status: string;
  locations: string;
  summary: string;
  error: string;
  created_at: string | null;
};

export default function App() {
  const [tab, setTab] = useState<"run" | "tracker" | "companies" | "review" | "history">("run");
  const [file, setFile] = useState<File | null>(null);
  const [locations, setLocations] = useState("San Francisco, CA, Remote");
  const [email, setEmail] = useState("");
  const [minScore, setMinScore] = useState(68);
  const [maxApps, setMaxApps] = useState(6);
  const [autoApply, setAutoApply] = useState(false);
  const [browserFill, setBrowserFill] = useState(true);
  const [busy, setBusy] = useState(false);
  const [logs, setLogs] = useState<string[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [openCover, setOpenCover] = useState<number | null>(null);
  const [history, setHistory] = useState<Application[]>([]);
  const [review, setReview] = useState<Application[]>([]);
  const [draftLetters, setDraftLetters] = useState<Record<number, string>>({});
  const [working, setWorking] = useState<number | null>(null);
  const [trackerDrafts, setTrackerDrafts] = useState<
    Record<number, { stage: string; user_notes: string; follow_up_date: string }>
  >({});
  const [runHistory, setRunHistory] = useState<RunSummary[]>([]);
  const [newOnly, setNewOnly] = useState(false);
  const [seen, setSeen] = useState<SeenJob[]>([]);

  const profile = run?.profile;

  function replaceApp(updated: Application) {
    setRun((r) => (r ? { ...r, applications: r.applications.map((a) => (a.id === updated.id ? updated : a)) } : r));
    setHistory((h) => h.map((a) => (a.id === updated.id ? updated : a)));
  }

  function addApp(created: Application) {
    setRun((r) => (r ? { ...r, applications: [...r.applications, created] } : r));
    setHistory((h) => [created, ...h]);
  }

  async function appAction(app: Application, action: "fill" | "submitted") {
    setWorking(app.id);
    try {
      const res =
        action === "fill"
          ? await fetch(`/api/applications/${app.id}/fill`, { method: "POST" })
          : await fetch(`/api/applications/${app.id}`, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ status: "applied" }),
            });
      if (!res.ok) throw new Error(await res.text());
      replaceApp(await res.json());
    } catch (err) {
      replaceApp({ ...app, notes: err instanceof Error ? err.message : "Request failed" });
    } finally {
      setWorking(null);
    }
  }

  function appButtons(app: Application) {
    if (app.status === "applied") return null;
    if (app.status === "pending_review")
      return (
        <button className="ghost" onClick={() => void loadReview()}>
          Review
        </button>
      );
    const pending = working === app.id;
    return (
      <>
        {app.apply_url ? (
          <button className="ghost" disabled={working !== null} onClick={() => void appAction(app, "fill")}>
            {pending ? "Filling…" : app.status === "filled" ? "Fill again" : "Fill form in browser"}
          </button>
        ) : null}
        <button className="ghost" disabled={working !== null} onClick={() => void appAction(app, "submitted")}>
          Mark submitted
        </button>
      </>
    );
  }

  function watchRun(id: string) {
    const source = new EventSource(`/api/runs/${id}/events`);
    source.onmessage = (msg) => {
      const event = JSON.parse(msg.data);
      if (event.type === "status") setLogs((l) => [...l, event.message]);
      if (event.type === "error") {
        setLogs((l) => [...l, event.message]);
        setBusy(false);
        source.close();
      }
      if (event.type === "done") {
        setLogs((l) => [...l, event.summary]);
        setBusy(false);
        source.close();
        void refresh(id);
        void refreshReview();
      }
      if (event.type === "profile" || event.type === "job" || event.type === "application") {
        void refresh(id);
      }
    };
    source.onerror = () => {
      void (async () => {
        const r = await fetch(`/api/runs/${id}`).catch(() => null);
        if (r && r.status === 404) {
          // The run is gone (e.g. the backend restarted and lost it). Stop
          // reconnecting instead of polling a dead events stream.
          source.close();
          setBusy(false);
          setLogs((l) => [
            ...l,
            "Run stream closed: this run is no longer on the server. Start a new run to continue.",
          ]);
          return;
        }
        void refresh(id);
      })();
    };
  }

  async function start(e: FormEvent) {
    e.preventDefault();
    if (!file) return;
    setBusy(true);
    setLogs(["Queued agent run"]);
    setRun(null);
    const body = new FormData();
    body.append("resume", file);
    body.append("locations", locations);
    body.append("auto_apply", String(autoApply));
    body.append("browser_fill", String(browserFill));
    body.append("min_score", String(minScore));
    body.append("max_applications", String(maxApps));
    body.append("contact_email", email);
    let created: { id: string };
    try {
      const res = await fetch("/api/runs", { method: "POST", body });
      if (!res.ok) throw new Error(await res.text());
      created = await res.json();
    } catch (err) {
      setLogs((l) => [...l, err instanceof Error ? err.message : "Failed to start"]);
      setBusy(false);
      return;
    }
    watchRun(created.id);
  }

  async function refresh(id: string) {
    const res = await fetch(`/api/runs/${id}`);
    if (!res.ok) return;
    const data: Run = await res.json();
    setRun(data);
    if (data.status === "completed" || data.status === "failed") setBusy(false);
  }

  async function loadTracker() {
    setTab("tracker");
    const rows: Application[] = await fetch("/api/applications").then((r) => r.json());
    setHistory(rows);
    setTrackerDrafts(
      Object.fromEntries(
        rows.map((a) => [
          a.id,
          {
            stage: a.pipeline_stage || "applied",
            user_notes: a.user_notes || "",
            follow_up_date: a.follow_up_date || "",
          },
        ]),
      ),
    );
    const seenRows = await fetch("/api/seen").then((r) => r.json());
    setSeen(seenRows);
  }

  async function saveTracker(id: number) {
    const draft = trackerDrafts[id];
    if (!draft) return;
    await fetch(`/api/applications/${id}/tracker`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pipeline_stage: draft.stage,
        user_notes: draft.user_notes,
        follow_up_date: draft.follow_up_date,
      }),
    });
    void loadTracker();
  }

  async function refreshReview() {
    const rows: Application[] = await fetch("/api/review").then((r) => r.json());
    setReview(rows);
    setDraftLetters((d) => {
      const next = { ...d };
      for (const a of rows) if (!(a.id in next)) next[a.id] = a.cover_letter;
      return next;
    });
  }

  async function loadReview() {
    setTab("review");
    await refreshReview();
  }

  async function approveApp(id: number) {
    setWorking(id);
    try {
      const res = await fetch(`/api/applications/${id}/approve`, { method: "POST" });
      if (res.ok) replaceApp(await res.json());
    } finally {
      setWorking(null);
      void refreshReview();
    }
  }

  async function skipApp(id: number) {
    const res = await fetch(`/api/applications/${id}/skip`, { method: "POST" });
    if (res.ok) replaceApp(await res.json());
    void refreshReview();
  }

  async function saveLetter(id: number) {
    const res = await fetch(`/api/applications/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cover_letter: draftLetters[id] ?? "" }),
    });
    if (res.ok) replaceApp(await res.json());
    void refreshReview();
  }

  function reviewCard(app: Application) {
    return (
      <article className="job" key={app.id}>
        <header>
          <h3>
            {app.title} · {app.company}
          </h3>
          <span className={`status ${app.status}`}>{app.status}</span>
        </header>
        <p className="meta">
          {app.channel} — {app.notes}
        </p>
        {app.apply_url ? (
          <a href={app.apply_url} target="_blank" rel="noreferrer">
            Apply URL
          </a>
        ) : null}
        <div style={{ marginTop: 8 }}>
          <label>Cover letter</label>
          <textarea
            rows={8}
            style={{ width: "100%" }}
            value={draftLetters[app.id] ?? app.cover_letter}
            onChange={(e) => setDraftLetters((d) => ({ ...d, [app.id]: e.target.value }))}
          />
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          <button className="primary" disabled={working !== null} onClick={() => void approveApp(app.id)}>
            {working === app.id ? "Approving…" : "Approve"}
          </button>
          <button className="ghost" onClick={() => void saveLetter(app.id)}>
            Save letter
          </button>
          <button className="ghost" onClick={() => void skipApp(app.id)}>
            Skip
          </button>
        </div>
      </article>
    );
  }

  async function loadRunHistory() {
    setTab("history");
    const rows: RunSummary[] = await fetch("/api/runs").then((r) => r.json());
    setRunHistory(rows);
  }

  async function openHistoryRun(id: string) {
    setTab("run");
    setBusy(false);
    setLogs([`Opened past run ${id}`]);
    await refresh(id);
  }

  async function rerunRun(id: string) {
    const created: { id: string } = await fetch(`/api/runs/${id}/rerun`, {
      method: "POST",
    }).then((r) => r.json());
    setTab("run");
    setBusy(true);
    setLogs([`Re-running with the same settings (new run ${created.id})`]);
    setRun(null);
    watchRun(created.id);
  }

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <div className="mark">JA</div>
          <div>
            <h1>JobApply Agent</h1>
            <p>Match a resume to live roles, then apply without babysitting forms.</p>
          </div>
        </div>
        <div className="tabs">
          <button className={tab === "run" ? "active" : ""} onClick={() => setTab("run")}>
            Agent
          </button>
          <button className={tab === "tracker" ? "active" : ""} onClick={() => void loadTracker()}>
            Applications
          </button>
          <button className={tab === "review" ? "active" : ""} onClick={() => void loadReview()}>
            Review
          </button>
          <button className={tab === "companies" ? "active" : ""} onClick={() => setTab("companies")}>
            Companies
          </button>
          <button className={tab === "history" ? "active" : ""} onClick={() => void loadRunHistory()}>
            Past runs
          </button>
        </div>
      </header>

      {tab === "companies" ? <Companies /> : null}
      {tab === "run" ? (
        <div className="grid">
          <div className="stack">
          <form className="card" onSubmit={start}>
            <h2>Brief the agent</h2>
            <p className="lede">
              Upload a resume and where you want to work. The agent searches hundreds of companies' job
              boards plus public job sites, ranks fits, writes cover letters, and pre-fills employer
              application forms for you to review.
            </p>
            <label>Resume</label>
            <input
              type="file"
              accept=".pdf,.docx,.doc,.txt,.md"
              onChange={(e) => setFile(e.target.files?.[0] || null)}
              required
            />
            <label>Locations</label>
            <input
              type="text"
              value={locations}
              onChange={(e) => setLocations(e.target.value)}
              placeholder="Austin, TX, Remote"
              required
            />
            <p className="hint">
              Separate places with commas and include the state (e.g. Newark, CA). Add “Remote” for remote roles
              in your country.
            </p>
            <label>Contact email override</label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="Used if the resume has no email"
            />
            <div className="row">
              <div>
                <label>Min match score</label>
                <input
                  type="number"
                  min={40}
                  max={95}
                  value={minScore}
                  onChange={(e) => setMinScore(Number(e.target.value))}
                />
              </div>
              <div>
                <label>Max applications</label>
                <input
                  type="number"
                  min={1}
                  max={20}
                  value={maxApps}
                  onChange={(e) => setMaxApps(Number(e.target.value))}
                />
              </div>
            </div>
            <label className="check">
              <input
                type="checkbox"
                checked={autoApply}
                onChange={(e) => setAutoApply(e.target.checked)}
              />
              Allow approved applications to be emailed automatically (every application still
              needs your approval first; otherwise approvals stay as prepared packets)
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={browserFill}
                onChange={(e) => setBrowserFill(e.target.checked)}
              />
              Pre-fill employer application forms in a browser window. Nothing is submitted: you review
              each tab, answer the highlighted questions, and click Submit yourself.
            </label>
            <button className="primary" disabled={busy || !file}>
              {busy ? "Agent running…" : "Run agent"}
            </button>
          </form>
          <JobLink<Application> runId={run?.id} onAdded={addApp} />
          </div>

          <section className="card">
            <h2>Live run</h2>
            {profile?.name ? (
              <>
                <p className="lede">
                  {profile.name}
                  {profile.years_experience ? ` · ${profile.years_experience} years` : ""}
                  {profile.titles?.[0] ? ` · ${profile.titles[0]}` : ""}
                </p>
                <div className="profile">
                  {(profile.skills || []).slice(0, 10).map((s) => (
                    <span className="chip" key={s}>
                      {s}
                    </span>
                  ))}
                </div>
              </>
            ) : (
              <p className="lede">Waiting for a resume. The console below fills in as the agent works.</p>
            )}
            <div className="log">
              {logs.length === 0 ? "Idle." : logs.map((line, i) => <div key={i}>› {line}</div>)}
            </div>
            <label className="check">
              <input
                type="checkbox"
                checked={newOnly}
                onChange={(e) => setNewOnly(e.target.checked)}
              />
              Only matches no earlier run has shown
            </label>
            <div className="jobs" style={{ marginTop: 16 }}>
              {(run?.jobs || [])
                .filter((job) => !newOnly || job.is_new)
                .map((job) => (
                  <article className="job" key={job.id}>
                    <header>
                      <h3>{job.title}</h3>
                      <span className="score">{job.score.toFixed(0)}</span>
                    </header>
                    <p className="meta">
                      {job.company} · {job.location || (job.remote ? "Remote" : "")} · {job.source}
                      {job.posted_date ? ` · posted ${job.posted_date}` : ""}
                    </p>
                    {job.is_new ? <p className="meta">New since your last runs</p> : null}
                    <p className="meta">{job.reasons}</p>
                    <a href={job.apply_url || job.url} target="_blank" rel="noreferrer">
                      Open posting
                    </a>
                  </article>
                ))}
            </div>
            {(run?.applications || []).length > 0 && (
              <div className="jobs" style={{ marginTop: 18 }}>
                <h2>Packets</h2>
                {run!.applications.map((app) =>
                  app.status === "pending_review" ? (
                    reviewCard(app)
                  ) : (
                  <article className="job" key={app.id}>
                    <header>
                      <h3>
                        {app.title} · {app.company}
                      </h3>
                      <span className={`status ${app.status}`}>{app.status}</span>
                    </header>
                    <p className="meta">
                      {app.channel} — {app.notes}
                    </p>
                    {app.apply_url ? (
                      <a href={app.apply_url} target="_blank" rel="noreferrer">
                        Apply on employer site
                      </a>
                    ) : null}
                    <div>
                      <button className="ghost" onClick={() => setOpenCover(openCover === app.id ? null : app.id)}>
                        {openCover === app.id ? "Hide letter" : "Cover letter"}
                      </button>
                      {appButtons(app)}
                    </div>
                    {openCover === app.id ? <div className="cover">{app.cover_letter}</div> : null}
                  </article>
                ))}
              </div>
            )}
          </section>
        </div>
      ) : tab === "history" ? (
        <section className="card">
          <h2>Past runs</h2>
          <p className="lede">Reopen any past run, or re-run it with the same settings and resume.</p>
          <div className="jobs">
            {runHistory.length === 0 ? (
              <p className="empty">No runs yet.</p>
            ) : (
              runHistory.map((r) => (
                <article className="job" key={r.id}>
                  <header>
                    <h3>Run {r.id}</h3>
                    <span className={`status ${r.status}`}>{r.status}</span>
                  </header>
                  <p className="meta">
                    {r.locations}
                    {r.created_at ? ` · ${r.created_at.slice(0, 10)}` : ""}
                  </p>
                  <p className="meta">{r.summary || r.error}</p>
                  <div className="row">
                    <button className="ghost" onClick={() => void openHistoryRun(r.id)}>
                      Open
                    </button>
                    <button className="ghost" onClick={() => void rerunRun(r.id)}>
                      Re-run
                    </button>
                  </div>
                </article>
              ))
            )}
          </div>
        </section>
      ) : tab === "tracker" ? (
        <section className="card">
          <h2>Application tracker</h2>
          <p className="lede">Every packet the agent prepared or sent, newest first.</p>
          <div className="jobs">
            {history.length === 0 ? (
              <p className="empty">No applications yet.</p>
            ) : (
              history.map((app) => {
                const draft = trackerDrafts[app.id] ?? {
                  stage: app.pipeline_stage || "applied",
                  user_notes: app.user_notes || "",
                  follow_up_date: app.follow_up_date || "",
                };
                const setDraft = (patch: Partial<typeof draft>) =>
                  setTrackerDrafts((d) => ({ ...d, [app.id]: { ...draft, ...patch } }));
                return (
                  <article className="job" key={app.id}>
                    <header>
                      <h3>
                        {app.title} · {app.company}
                      </h3>
                      <span className={`status ${app.status}`}>{app.status}</span>
                    </header>
                    <p className="meta">
                      {app.channel} — {app.notes}
                    </p>
                    {needsFollowUp(app) ? (
                      <p className="meta">Needs follow-up: in Applied for over 7 days with no reply.</p>
                    ) : null}
                    {app.follow_up_date ? (
                      <p className="meta">Follow up on {app.follow_up_date}</p>
                    ) : null}
                    {app.apply_url ? (
                      <a href={app.apply_url} target="_blank" rel="noreferrer">
                        Apply URL
                      </a>
                    ) : null}
                    <div className="row" style={{ marginTop: 8 }}>
                      <div>
                        <label>Stage</label>
                        <select
                          value={draft.stage}
                          onChange={(e) => setDraft({ stage: e.target.value })}
                        >
                          {STAGES.map((s) => (
                            <option key={s.value} value={s.value}>
                              {s.label}
                            </option>
                          ))}
                        </select>
                      </div>
                      <div>
                        <label>Follow-up date</label>
                        <input
                          type="date"
                          value={draft.follow_up_date}
                          onChange={(e) => setDraft({ follow_up_date: e.target.value })}
                        />
                      </div>
                    </div>
                    <div style={{ marginTop: 8 }}>
                      <label>Notes</label>
                      <textarea
                        rows={3}
                        style={{ width: "100%" }}
                        value={draft.user_notes}
                        onChange={(e) => setDraft({ user_notes: e.target.value })}
                      />
                    </div>
                    <button className="ghost" onClick={() => void saveTracker(app.id)}>
                      Save
                    </button>
                    <div>{appButtons(app)}</div>
                  </article>
                );
              })
            )}
          </div>
        </section>
      ) : tab === "review" ? (
        <section className="card">
          <h2>Review queue</h2>
          <p className="lede">
            Nothing here has been sent. Edit a letter, approve it to send, or skip it.
          </p>
          <div className="jobs">
            {review.length === 0 ? (
              <p className="empty">No applications waiting for review.</p>
            ) : (
              review.map((app) => reviewCard(app))
            )}
          </div>
          <h2 style={{ marginTop: 24 }}>Previously applied</h2>
          <p className="lede">
            Jobs already applied to or prepared in earlier runs. These are skipped automatically
            by future runs.
          </p>
          <div className="jobs">
            {seen.length === 0 ? (
              <p className="empty">Nothing applied to yet.</p>
            ) : (
              seen.map((job) => (
                <article className="job" key={job.id}>
                  <header>
                    <h3>
                      {job.title} · {job.company}
                    </h3>
                    <span className={`status ${job.status}`}>{job.status}</span>
                  </header>
                  <p className="meta">
                    {job.created_at ? `First seen ${job.created_at.slice(0, 10)}` : ""}
                  </p>
                  {job.url ? (
                    <a href={job.url} target="_blank" rel="noreferrer">
                      Posting
                    </a>
                  ) : null}
                </article>
              ))
            )}
          </div>
        </section>
      ) : null}
    </div>
  );
}
