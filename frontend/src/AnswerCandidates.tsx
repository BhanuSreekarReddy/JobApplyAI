import { useState } from "react";

export type AnswerCandidate = {
  key: string;
  question: string;
  answer: string;
  sensitive: boolean;
};

/**
 * Per-item confirmation for answer-bank candidates. Nothing is saved until
 * the user ticks items and confirms here: sensitive (EEO/demographic)
 * candidates start unchecked — an explicit opt-in per question — and
 * one-time codes/passwords never reach this list at all (the backend
 * filters them out before candidates are built).
 */
export default function AnswerCandidatesPanel({
  candidates,
  busy,
  saveLabel = "Save selected answers",
  dismissLabel = "Cancel",
  onSave,
  onDismiss,
}: {
  candidates: AnswerCandidate[];
  busy?: boolean;
  saveLabel?: string;
  dismissLabel?: string;
  onSave: (chosen: AnswerCandidate[]) => void;
  onDismiss: () => void;
}) {
  const [selected, setSelected] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(candidates.map((c) => [c.key, !c.sensitive])),
  );
  if (candidates.length === 0) {
    return (
      <div style={{ marginBottom: 12 }}>
        <p className="meta">
          Nothing new to save — everything with a value on this form was filled by the agent,
          and the rest is still empty.
        </p>
        <button className="ghost" onClick={onDismiss}>
          Dismiss
        </button>
      </div>
    );
  }
  return (
    <div style={{ marginBottom: 12 }}>
      <p className="meta">
        These are answers you typed yourself. Checked ones are saved to your profile and
        reused on future applications. Sensitive questions (EEO/demographic) are left
        unchecked — tick one only if you want it stored. One-time codes and passwords are
        never saved.
      </p>
      {candidates.map((c) => (
        <label
          key={c.key}
          style={{ display: "flex", gap: 8, alignItems: "flex-start", marginBottom: 6 }}
        >
          <input
            type="checkbox"
            checked={Boolean(selected[c.key])}
            onChange={(e) => setSelected((s) => ({ ...s, [c.key]: e.target.checked }))}
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
          disabled={busy}
          onClick={() => onSave(candidates.filter((c) => selected[c.key]))}
        >
          {saveLabel}
        </button>
        <button className="ghost" disabled={busy} onClick={onDismiss}>
          {dismissLabel}
        </button>
      </div>
    </div>
  );
}
