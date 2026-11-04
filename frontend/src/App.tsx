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
};

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

export default function App() {
  const [tab, setTab] = useState<"run" | "tracker" | "companies">("run");
  const [file, setFile] = useState<File | null>(null);
  const [locations, setLocations] = useState("San Francisco, CA, Remote");
  const [email, setEmail] = useState("");
  const [minScore, setMinScore] = useState(68);
  const [maxApps, setMaxApps] = useState(6);
  const [autoApply, setAutoApply] = useState(true);
  const [browserFill, setBrowserFill] = useState(true);
  const [busy, setBusy] = useState(false);
  const [logs, setLogs] = useState<string[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [openCover, setOpenCover] = useState<number | null>(null);
  const [history, setHistory] = useState<Application[]>([]);
  const [working, setWorking] = useState<number | null>(null);
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
    const source = new EventSource(`/api/runs/${created.id}/events`);
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
        void refresh(created.id);
      }
      if (event.type === "profile" || event.type === "job" || event.type === "application") {
        void refresh(created.id);
      }
    };
    source.onerror = () => {
      void refresh(created.id);
    };
  }

  async function refresh(id: string) {
    const data: Run = await fetch(`/api/runs/${id}`).then((r) => r.json());
    setRun(data);
    if (data.status === "completed" || data.status === "failed") setBusy(false);
  }

  async function loadTracker() {
    setTab("tracker");
    const rows = await fetch("/api/applications").then((r) => r.json());
    setHistory(rows);
    const seenRows = await fetch("/api/seen").then((r) => r.json());
    setSeen(seenRows);
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
          <button className={tab === "companies" ? "active" : ""} onClick={() => setTab("companies")}>
            Companies
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
              Apply automatically to ranked matches (email when possible; otherwise prepare an ATS packet)
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
            <div className="jobs" style={{ marginTop: 16 }}>
              {(run?.jobs || []).map((job) => (
                <article className="job" key={job.id}>
                  <header>
                    <h3>{job.title}</h3>
                    <span className="score">{job.score.toFixed(0)}</span>
                  </header>
                  <p className="meta">
                    {job.company} · {job.location || (job.remote ? "Remote" : "")} · {job.source}
                  </p>
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
                {run!.applications.map((app) => (
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
      ) : tab === "tracker" ? (
        <section className="card">
          <h2>Application tracker</h2>
          <p className="lede">Every packet the agent prepared or sent, newest first.</p>
          <div className="jobs">
            {history.length === 0 ? (
              <p className="empty">No applications yet.</p>
            ) : (
              history.map((app) => (
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
                  <div>{appButtons(app)}</div>
                </article>
              ))
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
