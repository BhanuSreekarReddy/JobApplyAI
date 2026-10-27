import { FormEvent, useState } from "react";

type Props<A> = {
  runId?: string;
  onAdded: (application: A) => void;
};

type Result = { status: string; company: string; title: string; notes: string };

export default function JobLink<A extends Result>({ runId, onAdded }: Props<A>) {
  const [url, setUrl] = useState("");
  const [fill, setFill] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [last, setLast] = useState<A | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!url.trim()) return;
    setBusy(true);
    setError("");
    setLast(null);
    try {
      const res = await fetch("/api/jobs/from-link", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url.trim(), run_id: runId || "", fill }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Couldn't use that link.");
      setLast(body.application);
      onAdded(body.application);
      setUrl("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't use that link.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card" onSubmit={submit}>
      <h2>Apply to a specific job</h2>
      <p className="lede">
        Paste a job link from any company's careers site. The agent finds the application form, writes the cover
        letter, and pre-fills it with your last resume.
      </p>
      <label>Job link</label>
      <input
        type="url"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        placeholder="https://lucidmotors.com/careers/search/5197938007"
        required
      />
      <label className="check">
        <input type="checkbox" checked={fill} onChange={(e) => setFill(e.target.checked)} />
        Pre-fill the form in the browser window (nothing is submitted)
      </label>
      <button className="primary" disabled={busy || !url.trim()}>
        {busy ? (fill ? "Opening and filling the form…" : "Preparing packet…") : "Prepare application"}
      </button>
      {error ? <p className="notice error">{error}</p> : null}
      {last ? (
        <p className="notice ok">
          {last.title} · {last.company}: {last.status === "filled" ? "form pre-filled. " : "packet ready. "}
          {last.notes}
        </p>
      ) : null}
    </form>
  );
}
