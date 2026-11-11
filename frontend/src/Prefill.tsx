import { useEffect, useRef, useState } from "react";

type SessionInfo = {
  id: string;
  application_id: number;
  apply_url: string;
  status: string;
  filled: string[];
  needs_attention: string[];
  notes: string;
};

type AnswerCandidate = {
  key: string;
  question: string;
  answer: string;
  sensitive: boolean;
  selected: boolean;
};

/**
 * Live streamed pre-fill session. The server runs the employer's form in an
 * ephemeral browser profile and streams it here over a WebSocket (CDP
 * screencast frames drawn to a canvas); mouse/keyboard/wheel input is
 * forwarded back. The profile is wiped server-side when the session closes,
 * and nothing is submitted by the agent — the user clicks Submit in the
 * streamed page themselves.
 */
export default function PrefillView({
  sessionId,
  onClose,
}: {
  sessionId: string;
  onClose: () => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const wheelAcc = useRef<{ x: number; y: number; deltaX: number; deltaY: number } | null>(null);
  const wheelRaf = useRef<number | null>(null);
  const [info, setInfo] = useState<SessionInfo | null>(null);
  const [streamState, setStreamState] = useState<"connecting" | "live" | "closed" | "error">(
    "connecting",
  );
  const [closing, setClosing] = useState(false);
  const [candidates, setCandidates] = useState<AnswerCandidate[] | null>(null);
  const [answersBusy, setAnswersBusy] = useState(false);
  const [answersNote, setAnswersNote] = useState("");

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const res = await fetch(`/api/prefill-sessions/${sessionId}`);
      if (res.ok && !cancelled) setInfo((await res.json()) as SessionInfo);
    })();

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(
      `${proto}//${window.location.host}/api/prefill-sessions/${sessionId}/stream`,
    );
    ws.binaryType = "blob";
    wsRef.current = ws;
    ws.onopen = () => setStreamState("live");
    ws.onmessage = (msg) => {
      if (!(msg.data instanceof Blob)) return;
      void (async () => {
        const bmp = await createImageBitmap(msg.data as Blob);
        const canvas = canvasRef.current;
        if (!canvas) return;
        if (canvas.width !== bmp.width || canvas.height !== bmp.height) {
          canvas.width = bmp.width;
          canvas.height = bmp.height;
        }
        const ctx = canvas.getContext("2d");
        ctx?.drawImage(bmp, 0, 0);
        bmp.close();
      })();
    };
    ws.onclose = () => setStreamState((s) => (s === "live" ? "closed" : s));
    ws.onerror = () => setStreamState("error");
    return () => {
      cancelled = true;
      ws.close();
    };
  }, [sessionId]);

  // Wheel scrolling needs a NON-passive listener: React attaches onWheel as
  // passive, so preventDefault() there is ignored and the outer page scrolls
  // along with the streamed page. Deltas are normalized to pixels first —
  // deltaMode 1 is lines (Firefox), 2 is pages — and bursts within one
  // animation frame are coalesced into a single message.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const flush = () => {
      wheelRaf.current = null;
      const acc = wheelAcc.current;
      wheelAcc.current = null;
      const ws = wsRef.current;
      if (acc && ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "wheel", ...acc }));
      }
    };
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      let dx = e.deltaX;
      let dy = e.deltaY;
      if (e.deltaMode === 1) {
        dx *= 16;
        dy *= 16;
      } else if (e.deltaMode === 2) {
        dx *= canvas.width;
        dy *= canvas.height;
      }
      const acc = wheelAcc.current ?? { x: 0, y: 0, deltaX: 0, deltaY: 0 };
      acc.x = ((e.clientX - rect.left) / rect.width) * canvas.width;
      acc.y = ((e.clientY - rect.top) / rect.height) * canvas.height;
      acc.deltaX += dx;
      acc.deltaY += dy;
      wheelAcc.current = acc;
      if (wheelRaf.current === null) wheelRaf.current = requestAnimationFrame(flush);
    };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      canvas.removeEventListener("wheel", onWheel);
      if (wheelRaf.current !== null) cancelAnimationFrame(wheelRaf.current);
      wheelRaf.current = null;
      wheelAcc.current = null;
    };
  }, []);

  function send(message: Record<string, unknown>) {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
  }

  function point(e: React.MouseEvent): { x: number; y: number } {
    const canvas = canvasRef.current!;
    const rect = canvas.getBoundingClientRect();
    return {
      x: ((e.clientX - rect.left) / rect.width) * canvas.width,
      y: ((e.clientY - rect.top) / rect.height) * canvas.height,
    };
  }

  async function closeSession() {
    setClosing(true);
    try {
      await fetch(`/api/prefill-sessions/${sessionId}/close`, { method: "POST" });
    } finally {
      wsRef.current?.close();
      onClose();
    }
  }

  async function loadCandidates() {
    setAnswersBusy(true);
    setAnswersNote("");
    try {
      const res = await fetch(`/api/prefill-sessions/${sessionId}/answer-candidates`, {
        method: "POST",
      });
      if (!res.ok) throw new Error(`Request failed (${res.status})`);
      const data = (await res.json()) as {
        candidates: Omit<AnswerCandidate, "selected">[];
      };
      // Sensitive (EEO/demographic) answers are opt-in: unchecked by default.
      setCandidates(data.candidates.map((c) => ({ ...c, selected: !c.sensitive })));
    } catch {
      setAnswersNote("Could not read the form's answers right now.");
    } finally {
      setAnswersBusy(false);
    }
  }

  async function saveSelectedAnswers() {
    if (!candidates) return;
    const chosen = candidates.filter((c) => c.selected);
    setAnswersBusy(true);
    try {
      const res = await fetch(`/api/prefill-sessions/${sessionId}/save-answers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          answers: chosen.map(({ key, question, answer }) => ({ key, question, answer })),
        }),
      });
      if (!res.ok) throw new Error(`Request failed (${res.status})`);
      const data = (await res.json()) as { saved: unknown[] };
      setCandidates(null);
      setAnswersNote(
        data.saved.length
          ? `Saved ${data.saved.length} answer(s) to your profile — future applications will reuse them.`
          : "Nothing was saved.",
      );
    } catch {
      setAnswersNote("Saving failed — your answers are still on the form, nothing was lost.");
    } finally {
      setAnswersBusy(false);
    }
  }

  return (
    <section className="card">
      <h2>Live application form</h2>
      <p className="notice">
        This is the employer's real form, streamed from a temporary browser. The agent filled
        what it could — <strong>you review everything and click Submit yourself</strong> inside
        the streamed page. Nothing is sent by the agent. When you close this session, the
        temporary browser profile (including anything you typed, like a site login) is deleted.
      </p>
      {info ? (
        <div className="row" style={{ alignItems: "flex-start" }}>
          <div>
            <h3>Filled for you</h3>
            {info.filled.length ? (
              <ul>
                {info.filled.map((f) => (
                  <li key={f}>{f}</li>
                ))}
              </ul>
            ) : (
              <p className="meta">Nothing could be filled automatically.</p>
            )}
          </div>
          <div>
            <h3>Needs your attention</h3>
            {info.needs_attention.length ? (
              <ul>
                {info.needs_attention.map((f) => (
                  <li key={f}>{f}</li>
                ))}
              </ul>
            ) : (
              <p className="meta">Nothing flagged — still review before submitting.</p>
            )}
          </div>
        </div>
      ) : null}
      {info?.notes ? <p className="meta">{info.notes}</p> : null}
      <p className="meta">
        Stream: {streamState}
        {info?.apply_url ? (
          <>
            {" · "}
            <a href={info.apply_url} target="_blank" rel="noreferrer">
              Open the apply page directly instead
            </a>
          </>
        ) : null}
      </p>
      <canvas
        ref={canvasRef}
        tabIndex={0}
        style={{
          width: "100%",
          maxWidth: 1280,
          border: "1px solid #ccc",
          borderRadius: 8,
          outline: "none",
          cursor: "pointer",
          background: "#fff",
        }}
        onMouseDown={(e) => {
          canvasRef.current?.focus();
          send({ type: "mouse", action: "press", ...point(e), button: "left" });
        }}
        onMouseUp={(e) => send({ type: "mouse", action: "release", ...point(e), button: "left" })}
        onMouseMove={(e) => {
          if (e.buttons) send({ type: "mouse", action: "move", ...point(e), button: "left" });
        }}
        onKeyDown={(e) => {
          // Full keystrokes, not bare characters: the streamed page's own
          // keydown handlers (segmented one-time-code boxes especially) need
          // a real key event with a virtual key code, or entered digits
          // can't be edited. The server turns "down" with text into a
          // keyDown + char pair.
          send({
            type: "key",
            action: "down",
            key: e.key,
            code: e.code,
            text: e.key.length === 1 ? e.key : "",
            vk: e.keyCode,
            modifiers:
              (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0),
          });
          e.preventDefault();
        }}
        onKeyUp={(e) => {
          send({
            type: "key",
            action: "up",
            key: e.key,
            code: e.code,
            vk: e.keyCode,
            modifiers:
              (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0),
          });
        }}
      />
      <p className="hint">
        Click the form to focus it, then type and click as usual — including the site's own
        Submit button when you are ready.
      </p>
      {candidates ? (
        <div style={{ marginBottom: 12 }}>
          <h3>Save answers from this form</h3>
          {candidates.length ? (
            <>
              <p className="meta">
                These are answers you typed yourself. Checked ones are saved to your profile and
                reused on future applications. Sensitive questions (EEO/demographic) are left
                unchecked — tick one only if you want it stored. One-time codes and passwords
                are never saved.
              </p>
              {candidates.map((c) => (
                <label
                  key={c.key}
                  style={{ display: "flex", gap: 8, alignItems: "flex-start", marginBottom: 6 }}
                >
                  <input
                    type="checkbox"
                    checked={c.selected}
                    onChange={(e) =>
                      setCandidates((list) =>
                        list
                          ? list.map((x) =>
                              x.key === c.key ? { ...x, selected: e.target.checked } : x,
                            )
                          : list,
                      )
                    }
                  />
                  <span>
                    <strong>{c.question}</strong>
                    {c.sensitive ? " (sensitive)" : ""} — {c.answer}
                  </span>
                </label>
              ))}
              <div className="row">
                <button
                  className="primary"
                  disabled={answersBusy}
                  onClick={() => void saveSelectedAnswers()}
                >
                  Save selected answers
                </button>
                <button
                  className="ghost"
                  disabled={answersBusy}
                  onClick={() => setCandidates(null)}
                >
                  Cancel
                </button>
              </div>
            </>
          ) : (
            <>
              <p className="meta">
                Nothing new to save — everything with a value on this form was filled by the
                agent, and the rest is still empty.
              </p>
              <button className="ghost" onClick={() => setCandidates(null)}>
                Dismiss
              </button>
            </>
          )}
        </div>
      ) : null}
      {answersNote ? <p className="notice ok">{answersNote}</p> : null}
      <div className="row">
        <button
          className="ghost"
          disabled={streamState !== "live" || answersBusy}
          onClick={() => void loadCandidates()}
        >
          {answersBusy ? "Working…" : "Save answers from this form"}
        </button>
        <button className="primary" disabled={closing} onClick={() => void closeSession()}>
          {closing ? "Closing…" : "Close session"}
        </button>
      </div>
    </section>
  );
}
