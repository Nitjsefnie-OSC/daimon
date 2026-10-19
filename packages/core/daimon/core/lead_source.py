"""Decode immutable email evidence without trusting model-supplied message text."""

import base64
import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses


@dataclass(frozen=True)
class EmailEvidence:
    text: str
    recipients: frozenset[str]


def decode_email(raw: str) -> EmailEvidence:
    message = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    )
    headers = "\n".join(f"{key}: {value}" for key, value in message.items())
    parts: list[str] = []
    for part in message.walk():
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    recipients = frozenset(
        address.casefold()
        for _, address in getaddresses(
            [
                str(value)
                for header in ("To", "Cc", "Delivered-To", "X-Original-To")
                for value in message.get_all(header, [])
            ]
        )
    )
    return EmailEvidence(text=headers + "\n\n" + "\n\n".join(parts), recipients=recipients)


def has_registration_request(content: str) -> bool:
    """Require a direct lead-registration request at the start of a user's message."""
    return (
        re.match(
            r"\s*(?:<@!?\d+>\s*)?(?:(?:please|can you|could you|would you)\s+)?"
            r"register this lead\b",
            content,
            flags=re.IGNORECASE,
        )
        is not None
    )
