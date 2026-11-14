import { useEffect, useRef, useState } from "react";
import AnswerCandidatesPanel, { AnswerCandidate } from "./AnswerCandidates";

type SessionInfo = {
  id: string;
  application_id: number;
  apply_url: string;
  status: string;
  filled: string[];
  needs_attention: string[];
  notes: string;
};

/**
 * Live streamed pre-fill session. The server runs the employer's form in an
 * ephemeral browser profile and streams it here over a WebSocket (CDP
 * screencast frames drawn to a canvas); mouse/keyboard/wheel input is
 * forwarded back. The profile is wiped server-side when the session closes,
 * and nothing is submitted by the agent — the user clicks Submit in the
 * streamed page themselves.
 *
 * Copy/paste: keyboard-native, the way a local page behaves. Ctrl/Cmd+V
 * is captured from the browser's own paste event (the focused canvas is
 * the sink, so the clipboard text is available synchronously — no
 * permission prompt, no paste box) and forwarded to the server, which
 * puts it on the streamed browser's clipboard and dispatches a genuine
 * Ctrl+V there: the page sees a real paste event. Ctrl/Cmd+C asks the
 * server to dispatch a genuine Ctrl+C in the streamed page and read its
 * clipboard back; the returned text is written to this machine's
 * clipboard. Copy/paste inside the streamed page never leaves its
 * browser and is fully native. Only plain text crosses between the two
 * machines. If a browser won't play along, the server degrades paste to
 * direct text insertion and copy to reading the selection — automatically;
 * there are no clipboard controls in the UI by design.
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
  const [pendingClose, setPendingClose] = useState(false);
  const [answersBusy, setAnswersBusy] = useState(false);
  const [answersNote, setAnswersNote] = useState("");
  const [clipNote, setClipNote] = useState("");

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
      if (typeof msg.data === "string") {
        // Text messages are control replies (clipboard text read back from
        // the streamed page, paste outcomes); binary messages are
        // screencast frames.
        try {
          const data = JSON.parse(msg.data) as { type?: string; text?: string; via?: string };
          if (data.type === "clipboard") void receiveCopiedText(data.text || "");
          if (data.type === "paste-result") {
            setClipNote(
              data.via === "native"
                ? "Pasted into the form."
                : "Pasted into the form (basic mode — the native paste route wasn't available).",
            );
          }
        } catch {
          /* not a control message */
        }
        return;
      }
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

  // Copy-out: the server dispatches a real Ctrl+C in the streamed page
  // and sends its clipboard back; put the text on this machine's
  // clipboard. Try the async clipboard API first, then a temporary
  // textarea + execCommand('copy') (still within reach of the copy
  // keystroke's user gesture). If the browser blocks both, the status
  // note says so — there is deliberately no manual-copy UI.
  async function receiveCopiedText(text: string) {
    if (!text) {
      setClipNote("Nothing is selected in the form — select text there first, then copy.");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      setClipNote("Copied from the form to your clipboard.");
      return;
    } catch {
      /* fall through to the execCommand route */
    }
    try {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      if (ok) {
        setClipNote("Copied from the form to your clipboard.");
        return;
      }
    } catch {
      /* fall through to the status note */
    }
    setClipNote("Your browser blocked automatic copying, so the selected text couldn't be copied.");
  }

  function requestCopy() {
    setClipNote("");
    send({ type: "copy" });
  }

  async function fetchCandidates(): Promise<AnswerCandidate[] | null> {
    const res = await fetch(`/api/prefill-sessions/${sessionId}/answer-candidates`, {
      method: "POST",
    });
    if (!res.ok) throw new Error(`Request failed (${res.status})`);
    const data = (await res.json()) as { candidates: AnswerCandidate[] };
    return data.candidates;
  }

  async function loadCandidates() {
    setAnswersBusy(true);
    setAnswersNote("");
    try {
      setCandidates(await fetchCandidates());
    } catch {
      setAnswersNote("Could not read the form's answers right now.");
    } finally {
      setAnswersBusy(false);
    }
  }

  // Closing used to throw away everything the user typed unless they had
  // pressed "Save answers" first. Now closing captures first: if there
  // are unsaved answers, the save panel appears and the user chooses —
  // save and close, or close without saving.
  async function requestClose() {
    if (candidates) return; // the save panel is showing; its buttons decide
    if (streamState === "live") {
      setAnswersBusy(true);
      try {
        const found = await fetchCandidates();
        if (found && found.length) {
          setCandidates(found);
          setPendingClose(true);
          setAnswersBusy(false);
          return;
        }
      } catch {
        /* capture is best-effort at close; the session still closes */
      } finally {
        setAnswersBusy(false);
      }
    }
    await closeSession();
  }

  async function saveSelectedAnswers(chosen: AnswerCandidate[]) {
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
      if (pendingClose) {
        setPendingClose(false);
        await closeSession();
      }
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
          const mod = e.ctrlKey || e.metaKey;
          if (mod && e.key.toLowerCase() === "v") {
            // Paste: do NOT preventDefault and do NOT forward the
            // keystroke. The browser must be allowed to fire its native
            // paste event on this focused canvas — preventDefault here
            // would suppress it — and onPaste forwards the clipboard
            // text to the server, which performs a genuine paste inside
            // the streamed page. Forwarding the bare keystroke would
            // race that with the session browser's own clipboard.
            return;
          }
          if (mod && e.key.toLowerCase() === "c") {
            // Copy: swallow the keystroke locally and ask the server to
            // copy natively (it dispatches its own Ctrl+C in the
            // streamed page, so the page's clipboard updates there too
            // and copy/paste within the page keeps working).
            e.preventDefault();
            requestCopy();
            return;
          }
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
          const modUp = e.ctrlKey || e.metaKey;
          if (modUp && (e.key.toLowerCase() === "v" || e.key.toLowerCase() === "c")) {
            // The matching keydown was handled locally (paste event /
            // native copy request); the streamed page never saw it, so
            // don't send it a stray key release either.
            return;
          }
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
        onPaste={(e) => {
          // The browser's native paste event: this machine's clipboard
          // text, available synchronously because the canvas is focused.
          // The server turns it into a genuine paste in the streamed
          // page (its clipboard + a real Ctrl+V), falling back to plain
          // insertion only if that route is unavailable there.
          const text = e.clipboardData.getData("text");
          if (text) {
            e.preventDefault();
            setClipNote("");
            send({ type: "paste", text });
          }
        }}
      />
      <p className="hint">
        Click the form to focus it, then type and click as usual — including the site's own
        Submit button when you are ready. Copy and paste work with your usual shortcuts:
        Ctrl+C / Cmd+C copies the text you selected in the form to your own clipboard, and
        Ctrl+V / Cmd+V pastes your clipboard into the focused field — no buttons needed.
      </p>
      {clipNote ? <p className="meta">{clipNote}</p> : null}
      {candidates ? (
        <>
          <h3>{pendingClose ? "Save your answers before closing?" : "Save answers from this form"}</h3>
          <AnswerCandidatesPanel
            candidates={candidates}
            busy={answersBusy}
            saveLabel={pendingClose ? "Save selected & close" : "Save selected answers"}
            dismissLabel={pendingClose ? "Close without saving" : "Cancel"}
            onSave={(chosen) => void saveSelectedAnswers(chosen)}
            onDismiss={() => {
              setCandidates(null);
              if (pendingClose) {
                setPendingClose(false);
                void closeSession();
              }
            }}
          />
        </>
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
        <button className="primary" disabled={closing} onClick={() => void requestClose()}>
          {closing ? "Closing…" : "Close session"}
        </button>
      </div>
    </section>
  );
}
