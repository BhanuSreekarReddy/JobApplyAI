import { FormEvent, useEffect, useState } from "react";

export type AuthUser = {
  id: number;
  email: string;
  role: "user" | "admin";
  is_active: boolean;
  email_verified: boolean;
  seeded_admin: boolean;
};

export type PublicSummary = {
  app_name: string;
  tagline: string;
  companies: number;
  open_jobs: number;
};

export type AuthPage =
  | "landing"
  | "login"
  | "register"
  | "verify"
  | "forgot"
  | "reset";

async function api(path: string, init?: RequestInit): Promise<Response> {
  return fetch(path, init);
}

async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await api(path, init);
  if (!res.ok) throw new Error(await errorText(res));
  return (await res.json()) as T;
}

export function ResendVerification({ email }: { email: string }) {
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  async function resend() {
    if (!email) {
      setNotice("Enter your email address first, then resend.");
      return;
    }
    setBusy(true);
    try {
      await apiJson("/api/auth/resend-verification", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      // The endpoint always answers the same way, account or not.
      setNotice(
        "If that email has an unverified account, a new verification link is on its way.",
      );
    } catch {
      setNotice("Could not request a new link right now. Try again in a minute.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button type="button" className="ghost" onClick={() => void resend()} disabled={busy}>
        {busy ? "Sending…" : "Resend verification email"}
      </button>
      {notice ? <p className="notice ok">{notice}</p> : null}
    </>
  );
}

async function errorText(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") return body.detail;
  } catch {
    /* fall through */
  }
  return `Request failed (${res.status})`;
}

function Brand() {
  return (
    <div className="brand">
      <div className="mark">JA</div>
      <div>
        <h1>JobApply Agent</h1>
        <p>Match a resume to live roles, then apply without babysitting forms.</p>
      </div>
    </div>
  );
}

export function Landing({
  summary,
  go,
}: {
  summary: PublicSummary | null;
  go: (page: AuthPage) => void;
}) {
  return (
    <div className="app">
      <header className="top">
        <Brand />
        <div className="tabs">
          <button onClick={() => go("login")}>Sign in</button>
          <button className="active" onClick={() => go("register")}>
            Register
          </button>
        </div>
      </header>
      <section className="card">
        <h2>Your job search, run like an agent — not a tab farm</h2>
        <p className="lede">
          Upload a resume once. The agent searches hundreds of company job boards plus public job
          sites, ranks the roles that actually fit, writes a tailored cover letter for each one,
          and pre-fills the employer's application form. You stay in control: nothing is ever
          submitted without your review.
        </p>
        <h3>How it works</h3>
        <ol className="steps">
          <li>Brief the agent with your resume and target locations.</li>
          <li>It searches, scores, and prepares application packets.</li>
          <li>
            You review each packet, approve it, and — for employer forms — watch the form get
            filled in a live streamed browser session, answer anything highlighted, and click
            Submit yourself.
          </li>
        </ol>
        {summary ? (
          <p className="lede">
            {summary.open_jobs > 0
              ? `Tracking ${summary.companies} companies and ${summary.open_jobs} open roles right now.`
              : `Tracking ${summary.companies} companies right now.`}
          </p>
        ) : null}
        <p className="meta">
          Registration is invite-only while the service is in early access. Have an invite code?
        </p>
        <div className="row">
          <button className="primary" onClick={() => go("register")}>
            Create your account
          </button>
          <button className="ghost" onClick={() => go("login")}>
            Sign in
          </button>
        </div>
      </section>
    </div>
  );
}

function AuthShell({
  title,
  children,
  go,
}: {
  title: string;
  children: React.ReactNode;
  go: (page: AuthPage) => void;
}) {
  return (
    <div className="app">
      <header className="top">
        <Brand />
        <div className="tabs">
          <button onClick={() => go("landing")}>Home</button>
        </div>
      </header>
      <section className="card" style={{ maxWidth: 520, margin: "0 auto" }}>
        <h2>{title}</h2>
        {children}
      </section>
    </div>
  );
}

export function LoginPage({
  go,
  onSignedIn,
}: {
  go: (page: AuthPage) => void;
  onSignedIn: (user: AuthUser) => void;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const user = await apiJson<AuthUser>("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      onSignedIn(user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell title="Sign in" go={go}>
      <form onSubmit={submit}>
        <label>Email</label>
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        <label>Password</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        {error ? <p className="notice error">{error}</p> : null}
        <button className="primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <p className="meta auth-links">
        <button className="ghost" onClick={() => go("forgot")}>
          Forgot password
        </button>
        <button className="ghost" onClick={() => go("register")}>
          Register with an invite
        </button>
      </p>
      <p className="meta">
        Didn't get the verification email?
        <ResendVerification email={email} />
      </p>
    </AuthShell>
  );
}

export function RegisterPage({ go }: { go: (page: AuthPage) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [invite, setInvite] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await apiJson("/api/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, invite_code: invite }),
      });
      // Registration no longer signs in: the address must be verified first.
      setDone(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Registration failed");
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <AuthShell title="Check your inbox" go={go}>
        <p className="notice ok">
          Account created. We've emailed a verification link to {email} — open it to verify
          your address, then sign in.
        </p>
        <button className="primary" onClick={() => go("login")}>
          Continue to sign in
        </button>
        <p className="meta">
          Didn't get it?
          <ResendVerification email={email} />
        </p>
      </AuthShell>
    );
  }

  return (
    <AuthShell title="Register" go={go}>
      <p className="lede">
        Registration is invite-only. Enter the invite code you received, choose a password of at
        least 10 characters, and we'll email you a verification link.
      </p>
      <form onSubmit={submit}>
        <label>Email</label>
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        <label>Password</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          minLength={10}
        />
        <label>Invite code</label>
        <input type="text" value={invite} onChange={(e) => setInvite(e.target.value)} required />
        {error ? <p className="notice error">{error}</p> : null}
        <button className="primary" disabled={busy}>
          {busy ? "Creating account…" : "Create account"}
        </button>
      </form>
      <p className="meta auth-links">
        Already have an account?
        <button className="ghost" onClick={() => go("login")}>
          Sign in
        </button>
      </p>
    </AuthShell>
  );
}

export function VerifyPage({ go }: { go: (page: AuthPage) => void }) {
  const [state, setState] = useState<"working" | "done" | "error">("working");
  const [message, setMessage] = useState("");

  useEffect(() => {
    const token = new URLSearchParams(window.location.search).get("token") || "";
    if (!token) {
      setState("error");
      setMessage("This verification link is missing its token.");
      return;
    }
    void (async () => {
      try {
        await apiJson("/api/auth/verify-email", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token }),
        });
        setState("done");
      } catch (err) {
        setState("error");
        setMessage(err instanceof Error ? err.message : "Verification failed");
      }
    })();
  }, []);

  return (
    <AuthShell title="Verify your email" go={go}>
      {state === "working" ? <p className="lede">Verifying…</p> : null}
      {state === "done" ? (
        <>
          <p className="notice ok">Your email is verified. You can start agent runs now.</p>
          <button className="primary" onClick={() => go("login")}>
            Continue to sign in
          </button>
        </>
      ) : null}
      {state === "error" ? (
        <>
          <p className="notice error">{message}</p>
          <p className="meta">
            If the link expired, you can request a new one from the sign-in page.
          </p>
          <button className="ghost" onClick={() => go("login")}>
            Back to sign in
          </button>
        </>
      ) : null}
    </AuthShell>
  );
}

export function ForgotPage({ go }: { go: (page: AuthPage) => void }) {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await apiJson("/api/auth/request-password-reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      setSent(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    }
  }

  return (
    <AuthShell title="Reset your password" go={go}>
      {sent ? (
        <p className="notice ok">
          If that email has an account, a password reset link is on its way. Check your inbox.
        </p>
      ) : (
        <form onSubmit={submit}>
          <label>Email</label>
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          {error ? <p className="notice error">{error}</p> : null}
          <button className="primary">Email me a reset link</button>
        </form>
      )}
      <p className="meta">
        <button className="ghost" onClick={() => go("login")}>
          Back to sign in
        </button>
      </p>
    </AuthShell>
  );
}

export function ResetPage({ go }: { go: (page: AuthPage) => void }) {
  const [password, setPassword] = useState("");
  const [done, setDone] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    const token = new URLSearchParams(window.location.search).get("token") || "";
    try {
      await apiJson("/api/auth/reset-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, password }),
      });
      setDone(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reset failed");
    }
  }

  return (
    <AuthShell title="Choose a new password" go={go}>
      {done ? (
        <>
          <p className="notice ok">Password updated. Sign in with your new password.</p>
          <button className="primary" onClick={() => go("login")}>
            Sign in
          </button>
        </>
      ) : (
        <form onSubmit={submit}>
          <label>New password (at least 10 characters)</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={10}
          />
          {error ? <p className="notice error">{error}</p> : null}
          <button className="primary">Set new password</button>
        </form>
      )}
    </AuthShell>
  );
}

type ProfileData = {
  name: string;
  contact_email: string;
  phone: string;
  linkedin: string;
  github: string;
  titles: string[];
  skills: string[];
  years_experience: number | null;
  summary: string;
  locations: string;
};

type SavedAnswer = {
  key: string;
  question: string;
  answer: string;
  sensitive: boolean;
};

// The repeat questions nearly every application asks. Answers stored here
// are the only values the agent may use for them — it never guesses.
const CANONICAL_ANSWER_FIELDS: { key: string; label: string; question: string }[] = [
  { key: "work_authorization", label: "Work authorization", question: "Are you authorized to work in the US?" },
  { key: "sponsorship", label: "Sponsorship", question: "Will you now or in the future require sponsorship?" },
  { key: "citizenship_status", label: "Citizenship status", question: "Citizenship status" },
  { key: "country", label: "Country", question: "Country" },
  { key: "work_onsite", label: "Willing to work onsite", question: "Are you willing to work onsite?" },
  { key: "salary_expectations", label: "Salary expectations", question: "What are your salary expectations?" },
  { key: "start_date", label: "Earliest start date", question: "What is your earliest start date?" },
  { key: "how_heard", label: "How you hear about roles", question: "How did you hear about this opportunity?" },
  // "Previously employed/interviewed at this employer" are deliberately NOT
  // standard fields: the answer differs per company, so a single saved value
  // would be wrong on most forms. Answers captured for them in a live
  // session still land in the bank (under Other saved answers).
  { key: "security_clearance", label: "Security clearance", question: "Do you hold an active security clearance?" },
];

export function ProfilePage() {
  const [form, setForm] = useState<ProfileData | null>(null);
  const [notice, setNotice] = useState("");
  const [savedAnswers, setSavedAnswers] = useState<SavedAnswer[]>([]);
  const [canonical, setCanonical] = useState<Record<string, string>>({});
  const [answerNotice, setAnswerNotice] = useState("");

  useEffect(() => {
    void (async () => {
      const data = await apiJson<ProfileData>("/api/profile");
      setForm(data);
    })();
    void (async () => {
      try {
        const data = await apiJson<{ answers: SavedAnswer[] }>("/api/profile/answers");
        setSavedAnswers(data.answers);
        const map: Record<string, string> = {};
        for (const a of data.answers) map[a.key] = a.answer;
        setCanonical(map);
      } catch {
        /* the section just stays empty */
      }
    })();
  }, []);

  if (!form) return (
    <section className="card">
      <h2>Profile</h2>
      <p className="lede">Loading…</p>
    </section>
  );

  const set = (patch: Partial<ProfileData>) => setForm((f) => (f ? { ...f, ...patch } : f));

  async function saveAnswers(e: FormEvent) {
    e.preventDefault();
    try {
      const data = await apiJson<{ answers: SavedAnswer[] }>("/api/profile/answers", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          items: CANONICAL_ANSWER_FIELDS.map((f) => ({
            key: f.key,
            question: f.question,
            answer: canonical[f.key] ?? "",
          })),
        }),
      });
      setSavedAnswers(data.answers);
      setAnswerNotice("Answers saved. Future application forms will reuse them.");
    } catch (err) {
      setAnswerNotice(err instanceof Error ? err.message : "Save failed");
    }
  }

  async function deleteAnswer(key: string) {
    try {
      const data = await apiJson<{ answers: SavedAnswer[] }>(
        `/api/profile/answers?key=${encodeURIComponent(key)}`,
        { method: "DELETE" },
      );
      setSavedAnswers(data.answers);
      setCanonical((c) => {
        const next = { ...c };
        delete next[key];
        return next;
      });
    } catch (err) {
      setAnswerNotice(err instanceof Error ? err.message : "Delete failed");
    }
  }

  const otherAnswers = savedAnswers.filter(
    (a) => !CANONICAL_ANSWER_FIELDS.some((f) => f.key === a.key),
  );

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!form) return;
    try {
      const saved = await apiJson<ProfileData>("/api/profile", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      setForm(saved);
      setNotice("Profile saved. Future runs start from these details.");
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Save failed");
    }
  }

  return (
    <section className="card">
      <h2>Profile</h2>
      <p className="lede">
        This is the persistent profile the agent works from. Uploading a resume on a run refreshes
        it from the resume's extracted details; edits here are kept until the next run.
      </p>
      <form onSubmit={save}>
        <label>Full name</label>
        <input type="text" value={form.name} onChange={(e) => set({ name: e.target.value })} />
        <label>Contact email</label>
        <input
          type="email"
          value={form.contact_email}
          onChange={(e) => set({ contact_email: e.target.value })}
        />
        <label>Phone</label>
        <input type="text" value={form.phone} onChange={(e) => set({ phone: e.target.value })} />
        <div className="row">
          <div>
            <label>LinkedIn</label>
            <input
              type="text"
              value={form.linkedin}
              onChange={(e) => set({ linkedin: e.target.value })}
            />
          </div>
          <div>
            <label>GitHub</label>
            <input
              type="text"
              value={form.github}
              onChange={(e) => set({ github: e.target.value })}
            />
          </div>
        </div>
        <label>Target titles (comma separated)</label>
        <input
          type="text"
          value={form.titles.join(", ")}
          onChange={(e) =>
            set({ titles: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })
          }
        />
        <label>Skills (comma separated)</label>
        <input
          type="text"
          value={form.skills.join(", ")}
          onChange={(e) =>
            set({ skills: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })
          }
        />
        <label>Years of experience</label>
        <input
          type="number"
          min={0}
          max={60}
          value={form.years_experience ?? ""}
          onChange={(e) =>
            set({ years_experience: e.target.value === "" ? null : Number(e.target.value) })
          }
        />
        <label>Summary</label>
        <textarea
          rows={5}
          style={{ width: "100%" }}
          value={form.summary}
          onChange={(e) => set({ summary: e.target.value })}
        />
        <label>Locations</label>
        <input
          type="text"
          value={form.locations}
          onChange={(e) => set({ locations: e.target.value })}
        />
        {notice ? <p className="notice ok">{notice}</p> : null}
        <button className="primary">Save profile</button>
      </form>
      <h3 style={{ marginTop: 28 }}>Application answers</h3>
      <p className="lede">
        The questions nearly every application asks. Answer them once here and the agent reuses
        your answers on future forms — it never guesses these, and anything you leave blank is
        still flagged for you on each form. Answers you save from a live pre-fill session land
        here too.
      </p>
      <form onSubmit={saveAnswers}>
        {CANONICAL_ANSWER_FIELDS.map((f) => (
          <div key={f.key}>
            <label>{f.label}</label>
            <input
              type="text"
              value={canonical[f.key] ?? ""}
              onChange={(e) => setCanonical((c) => ({ ...c, [f.key]: e.target.value }))}
            />
          </div>
        ))}
        {answerNotice ? <p className="notice ok">{answerNotice}</p> : null}
        <button className="primary">Save answers</button>
      </form>
      {otherAnswers.length ? (
        <>
          <h4 style={{ marginTop: 20 }}>Other saved answers</h4>
          <div className="jobs">
            {otherAnswers.map((a) => (
              <article className="job" key={a.key}>
                <p className="meta">
                  <strong>{a.question || a.key}</strong>
                  {a.sensitive ? " · sensitive" : ""} — {a.answer}
                </p>
                <button className="ghost" onClick={() => void deleteAnswer(a.key)}>
                  Delete
                </button>
              </article>
            ))}
          </div>
        </>
      ) : null}
    </section>
  );
}

type AdminUser = AuthUser & {
  created_at: string | null;
  last_login_at: string | null;
  run_count: number;
  application_count: number;
};

type AdminInvite = {
  code: string;
  created_at: string | null;
  expires_at: string | null;
  used_by: number | null;
  used: boolean;
};

export function AdminPage() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [invites, setInvites] = useState<AdminInvite[]>([]);
  const [newCode, setNewCode] = useState("");
  const [error, setError] = useState("");

  async function refresh() {
    try {
      setUsers(await apiJson<AdminUser[]>("/api/admin/users"));
      setInvites(await apiJson<AdminInvite[]>("/api/admin/invites"));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load admin data");
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function act(id: number, action: string) {
    setError("");
    try {
      await apiJson(`/api/admin/users/${id}/${action}`, {
        method: action === "delete" ? "DELETE" : "POST",
      });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed");
    }
  }

  async function createInvite() {
    setError("");
    try {
      const res = await apiJson<{ code: string }>("/api/admin/invites", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expires_days: 7 }),
      });
      setNewCode(res.code);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create invite");
    }
  }

  return (
    <section className="card">
      <h2>Admin</h2>
      <p className="lede">
        Accounts on this hosted instance. Admins see counts and metadata only — never resume or
        application contents. Seeded admins (from ADMIN_EMAILS) cannot be demoted, deactivated,
        or deleted here.
      </p>
      {error ? <p className="notice error">{error}</p> : null}
      <div className="jobs">
        {users.map((u) => (
          <article className="job" key={u.id}>
            <header>
              <h3>{u.email}</h3>
              <span className={`status ${u.is_active ? "prepared" : "failed"}`}>
                {u.role}
                {u.seeded_admin ? " · seeded" : ""}
                {u.is_active ? "" : " · deactivated"}
                {u.email_verified ? "" : " · unverified"}
              </span>
            </header>
            <p className="meta">
              {u.run_count} runs · {u.application_count} applications
              {u.created_at ? ` · joined ${u.created_at.slice(0, 10)}` : ""}
              {u.last_login_at ? ` · last sign-in ${u.last_login_at.slice(0, 10)}` : ""}
            </p>
            <div className="row">
              {u.is_active ? (
                <button className="ghost" onClick={() => void act(u.id, "deactivate")}>
                  Deactivate
                </button>
              ) : (
                <button className="ghost" onClick={() => void act(u.id, "activate")}>
                  Activate
                </button>
              )}
              {u.role === "admin" ? (
                <button className="ghost" onClick={() => void act(u.id, "demote")}>
                  Demote
                </button>
              ) : (
                <button className="ghost" onClick={() => void act(u.id, "promote")}>
                  Promote
                </button>
              )}
              <button className="ghost" onClick={() => void act(u.id, "delete")}>
                Delete user &amp; data
              </button>
            </div>
          </article>
        ))}
      </div>
      <h3 style={{ marginTop: 24 }}>Invites</h3>
      <p className="meta">
        Registration is invite-only. Create a code and share it with the person who should join;
        each code works once and expires in 7 days.
      </p>
      <button className="primary" onClick={() => void createInvite()}>
        Create invite code
      </button>
      {newCode ? (
        <p className="notice ok">
          New invite code: <strong>{newCode}</strong> — copy it now and send it to the invitee.
        </p>
      ) : null}
      <div className="jobs" style={{ marginTop: 12 }}>
        {invites.map((inv) => (
          <article className="job" key={inv.code}>
            <p className="meta">
              <strong>{inv.code}</strong> · {inv.used ? "used" : "unused"}
              {inv.expires_at ? ` · expires ${inv.expires_at.slice(0, 10)}` : ""}
            </p>
          </article>
        ))}
      </div>
    </section>
  );
}
