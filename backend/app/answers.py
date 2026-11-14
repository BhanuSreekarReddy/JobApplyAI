"""The profile answer bank: per-user saved answers to repeat application
questions, stored in profile_answers.

The form filler reads these (answers_map) alongside the resume profile; a
blocklisted/sensitive field may be filled ONLY from a saved answer here.
One-time codes, verification codes and passwords are refused at write time
— they must never be stored.
"""
from __future__ import annotations

from typing import Any

from .agent.formfill import CODE_LABEL_RE, answer_is_sensitive
from .models import ProfileAnswer


def _is_code_question(key: str, question: str) -> bool:
    hay = f"{key} {question}".lower()
    return bool(CODE_LABEL_RE.search(hay)) or "password" in hay


def answer_payload(row: ProfileAnswer) -> dict[str, Any]:
    return {
        "key": row.key,
        "question": row.question,
        "answer": row.answer,
        "sensitive": answer_is_sensitive(row.key, row.question),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_answers(db, user_id: int) -> list[dict[str, Any]]:
    rows = (
        db.query(ProfileAnswer)
        .filter(ProfileAnswer.user_id == user_id)
        .order_by(ProfileAnswer.key.asc())
        .all()
    )
    return [answer_payload(r) for r in rows]


def answers_map(db, user_id: int) -> dict[str, str]:
    """key -> answer for the filler. Only non-empty answers are usable."""
    rows = db.query(ProfileAnswer).filter(ProfileAnswer.user_id == user_id).all()
    return {r.key: r.answer for r in rows if r.key and (r.answer or "").strip()}


def upsert_answers(db, user_id: int, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Insert/update the given answers for this user. An item with an empty
    answer deletes the stored one. Code/password questions are dropped."""
    for item in items:
        key = (item.get("key") or "").strip()
        if not key:
            continue
        question = (item.get("question") or "").strip()
        answer = (item.get("answer") or "").strip()
        if _is_code_question(key, question):
            continue
        row = (
            db.query(ProfileAnswer)
            .filter(ProfileAnswer.user_id == user_id, ProfileAnswer.key == key)
            .first()
        )
        if not answer:
            if row is not None:
                db.delete(row)
            continue
        if row is None:
            row = ProfileAnswer(user_id=user_id, key=key)
            db.add(row)
        row.question = question or row.question or key
        row.answer = answer
    db.commit()
    return list_answers(db, user_id)


def delete_answer(db, user_id: int, key: str) -> bool:
    row = (
        db.query(ProfileAnswer)
        .filter(ProfileAnswer.user_id == user_id, ProfileAnswer.key == key)
        .first()
    )
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True
