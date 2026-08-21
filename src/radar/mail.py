"""Resend transactional email (SPEC §8.5). Shared by digest + flash alerts.

From: onboarding@resend.dev — delivers ONLY to the Resend account's own signup
email, which DIGEST_TO must equal. Idempotency-Key guards against Actions
retries double-sending.
"""

from __future__ import annotations

import httpx

RESEND_URL = "https://api.resend.com/emails"
FROM_ADDRESS = "internship-radar <onboarding@resend.dev>"


class SendError(RuntimeError):
    pass


def send_email(
    api_key: str,
    to: str,
    subject: str,
    html: str,
    idempotency_key: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """POST to Resend; returns the Resend email id. Raises SendError on non-2xx.
    `transport` lets tests inject httpx.MockTransport."""
    if not api_key or not to:
        raise SendError("RESEND_API_KEY / DIGEST_TO not configured")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Idempotency-Key": idempotency_key,
    }
    payload = {"from": FROM_ADDRESS, "to": [to], "subject": subject, "html": html}
    with httpx.Client(timeout=30, transport=transport) as client:
        resp = client.post(RESEND_URL, json=payload, headers=headers)
    if resp.status_code >= 300:
        raise SendError(f"resend {resp.status_code}: {resp.text[:300]}")
    try:
        return str(resp.json().get("id", ""))
    except ValueError:
        return ""
