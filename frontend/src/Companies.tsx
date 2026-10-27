import { FormEvent, useEffect, useMemo, useState } from "react";

export type Company = {
  id: number;
  name: string;
  ats: string;
  ats_label: string;
  slug: string;
  board_url: string;
  source: "starter" | "user";
  enabled: boolean;
  status: "ok" | "not_found" | "error" | "unchecked";
  job_count: number;
  note: string;
  last_checked: string | null;
};

type Summary = { total: number; enabled: number; user_added: number; open_jobs: number; problems: number };

const STATUS_LABEL: Record<Company["status"], string> = {
  ok: "Working",
  not_found: "Board gone",
  error: "Not responding",
  unchecked: "Not searched yet",
};

async function errorText(res: Response): Promise<string> {
  try {
    const body = await res.json();
    return typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    return `Request failed (${res.status})`;
  }
}

export default function Companies() {
  const [rows, setRows] = useState<Company[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [filter, setFilter] = useState("");
  const [view, setView] = useState<"all" | "user" | "problems" | "off">("all");
  const [busyId, setBusyId] = useState<number | null>(null);

  async function load() {
    const data = await fetch("/api/companies").then((r) => r.json());
    setRows(data.companies);
    setSummary(data.summary);
  }

  useEffect(() => {
    void load();
  }, []);

  async function add(e: FormEvent) {
    e.preventDefault();
    if (!query.trim()) return;
    setAdding(true);
    setMessage(null);
    try {
      const res = await fetch("/api/companies", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      if (!res.ok) throw new Error(await errorText(res));
      const data: { company: Company; created: boolean } = await res.json();
      const c = data.company;
      setMessage({
        kind: "ok",
        text: data.created
          ? `Added ${c.name} (${c.ats_label}, ${c.job_count} open jobs). It's searched on every run.`
          : `${c.name} is already tracked (${c.ats_label}, ${c.job_count} open jobs)${c.enabled ? "" : " and has been turned back on"}.`,
      });
      setQuery("");
      setFilter(c.name);
      setView("all");
      await load();
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : "Couldn't add that company." });
    } finally {
      setAdding(false);
    }
  }

  async function toggle(c: Company) {
    setBusyId(c.id);
    await fetch(`/api/companies/${c.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !c.enabled }),
    });
    await load();
    setBusyId(null);
  }

  async function remove(c: Company) {
    setBusyId(c.id);
    await fetch(`/api/companies/${c.id}`, { method: "DELETE" });
    await load();
    setBusyId(null);
  }

  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return rows.filter((c) => {
      if (view === "user" && c.source !== "user") return false;
      if (view === "problems" && !(c.enabled && (c.status === "not_found" || c.status === "error"))) return false;
      if (view === "off" && c.enabled) return false;
      return !needle || c.name.toLowerCase().includes(needle) || c.ats_label.toLowerCase().includes(needle);
    });
  }, [rows, filter, view]);

  return (
    <section className="card">
      <h2>Companies</h2>
      <p className="lede">
        Every run searches these companies' own job boards (Greenhouse, Lever, Ashby, and Workday) for roles in your
        locations. Add any company by name, or paste its careers page or a job link.
      </p>
      {summary ? (
        <p className="meta">
          {summary.enabled} of {summary.total} companies searched
          {summary.open_jobs ? ` · ${summary.open_jobs.toLocaleString()} open jobs at last search` : " · not searched yet"}
          {summary.user_added ? ` · ${summary.user_added} added by you` : ""}
          {summary.problems ? ` · ${summary.problems} need attention` : ""}
        </p>
      ) : null}

      <form className="add-company" onSubmit={add}>
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Company name or link, e.g. Lucid Motors or https://lucidmotors.com/careers"
          aria-label="Company name or careers link"
        />
        <button className="primary" disabled={adding || !query.trim()}>
          {adding ? "Finding board…" : "Add company"}
        </button>
      </form>
      {adding ? <p className="meta">Checking Greenhouse, Lever, and Ashby; careers pages can take up to 20 seconds.</p> : null}
      {message ? <p className={`notice ${message.kind}`}>{message.text}</p> : null}

      <div className="company-tools">
        <input
          type="search"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter companies"
          aria-label="Filter companies"
        />
        <div className="tabs small">
          {(
            [
              ["all", "All"],
              ["user", "Added by you"],
              ["problems", "Needs attention"],
              ["off", "Turned off"],
            ] as const
          ).map(([key, label]) => (
            <button key={key} className={view === key ? "active" : ""} onClick={() => setView(key)} type="button">
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="company-list">
        {shown.length === 0 ? <p className="empty">No companies match.</p> : null}
        {shown.map((c) => (
          <article className={`company ${c.enabled ? "" : "off"}`} key={c.id}>
            <div className="company-main">
              <h3>
                <a href={c.board_url} target="_blank" rel="noreferrer">
                  {c.name}
                </a>
              </h3>
              <p className="meta">
                {c.ats_label}
                {c.status === "ok" ? ` · ${c.job_count.toLocaleString()} open jobs` : ""}
                {c.source === "user" ? " · added by you" : ""}
              </p>
              {c.note ? <p className="meta note">{c.note}</p> : null}
            </div>
            <span className={`status ${c.enabled ? c.status : "off"}`}>{c.enabled ? STATUS_LABEL[c.status] : "Off"}</span>
            <div className="company-actions">
              <button className="ghost" disabled={busyId === c.id} onClick={() => void toggle(c)}>
                {c.enabled ? "Turn off" : "Turn on"}
              </button>
              {c.source === "user" ? (
                <button className="ghost" disabled={busyId === c.id} onClick={() => void remove(c)}>
                  Remove
                </button>
              ) : null}
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
