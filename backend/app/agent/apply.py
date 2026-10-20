from __future__ import annotations

import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from ..config import settings


def apply_to_job(
    *,
    profile: dict[str, Any],
    job: dict[str, Any],
    cover: str,
    resume_path: str,
    auto_apply: bool,
) -> dict[str, str]:
    if not auto_apply:
        return {
            "status": "prepared",
            "channel": "packet",
            "notes": "Application packet ready. Open the apply URL and paste the cover letter.",
        }

    email = (job.get("apply_email") or "").strip()
    if email and settings.smtp_host and profile.get("email"):
        try:
            _send_email(profile, job, cover, resume_path, email)
            return {
                "status": "applied",
                "channel": "email",
                "notes": f"Sent resume and cover letter to {email}.",
            }
        except Exception as exc:
            return {
                "status": "failed",
                "channel": "email",
                "notes": f"Email send failed: {exc}",
            }

    if email and not settings.smtp_host:
        return {
            "status": "prepared",
            "channel": "mailto",
            "notes": f"SMTP is not configured. Send the packet to {email}.",
        }

    return {
        "status": "prepared",
        "channel": "ats_link",
        "notes": "Employer ATS does not expose a public apply API. Packet is ready at the official apply URL.",
    }


def _send_email(profile: dict[str, Any], job: dict[str, Any], cover: str, resume_path: str, to_addr: str) -> None:
    msg = EmailMessage()
    sender = settings.smtp_from or settings.smtp_user or profile.get("email")
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Reply-To"] = profile.get("email") or sender
    msg["Subject"] = f"Application: {job.get('title')} — {profile.get('name')}"
    msg.set_content(cover)
    path = Path(resume_path)
    if path.exists():
        data = path.read_bytes()
        maintype, subtype = "application", "octet-stream"
        if path.suffix.lower() == ".pdf":
            subtype = "pdf"
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
