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
  const [info, setInfo] = useState<SessionInfo | null>(null);
  const [streamState, setStreamState] = useState<"connecting" | "live" | "closed" | "error">(
    "connecting",
  );
  const [closing, setClosing] = useState(false);

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
        onWheel={(e) => send({ type: "wheel", ...point(e), deltaX: e.deltaX, deltaY: e.deltaY })}
        onKeyDown={(e) => {
          if (e.key.length === 1) {
            send({ type: "key", action: "char", text: e.key });
          } else {
            send({ type: "key", action: "down", key: e.key, code: e.code, text: "" });
          }
          e.preventDefault();
        }}
        onKeyUp={(e) => {
          if (e.key.length > 1) {
            send({ type: "key", action: "up", key: e.key, code: e.code, text: "" });
          }
        }}
      />
      <p className="hint">
        Click the form to focus it, then type and click as usual — including the site's own
        Submit button when you are ready.
      </p>
      <button className="primary" disabled={closing} onClick={() => void closeSession()}>
        {closing ? "Closing…" : "Close session"}
      </button>
    </section>
  );
}
