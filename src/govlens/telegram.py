"""Render and send the compact Telegram product alert."""

from __future__ import annotations

import html
from typing import Any

import httpx

from .gist import validate_gist_url
from .model import Proposal
from .presentation import Presentation, validate_presentation

ICONS = {
    "LOW": "🟢",
    "MEDIUM": "🟠",
    "HIGH": "🔴",
    "CRITICAL": "🚨",
}


def _link(label: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def build_message(
    protocol: Presentation,
    proposal: Proposal,
    analysis: dict[str, Any],
    gist_url: str | None,
    *,
    test: bool = False,
) -> str:
    validate_presentation(protocol, proposal)
    severity = str(analysis["severity"])
    lines: list[str] = []
    name = protocol.name
    if proposal.source != proposal.protocol:
        name = f"{name} {proposal.source.title()}"
    if test:
        lines.extend(("🧪 <b>TEST · CURRENT ALERT REPLAY</b>", ""))
    lines.extend(
        (
            f"📜 <b>New {html.escape(name)} Proposal Created</b>",
            "",
            f"<b>Proposal {proposal.id}:</b> {html.escape(proposal.title)}",
            "",
        )
    )
    for field in protocol.facts:
        fact = proposal.facts.get(field.key)
        if fact is None:
            continue
        value = _link(fact.value, fact.url) if fact.url else html.escape(fact.value)
        lines.append(f"<b>{html.escape(field.label)}:</b> {value}")
    lines.extend(
        (
            "",
            f"{ICONS[severity]} <b>Risk: {html.escape(severity)}</b>",
            " ".join(html.escape(item) for item in analysis["summary_sentences"]),
            "",
        )
    )
    footer: list[str] = []
    if gist_url:
        footer.append(_link("Full Audit", validate_gist_url(gist_url)))
    footer.extend(
        _link(field.label, proposal.links[field.key])
        for field in protocol.links
        if field.key in proposal.links
    )
    lines.append("🔗 " + " | ".join(footer))
    message = "\n".join(lines)
    if len(message) > 4096:
        raise ValueError("Telegram alert exceeds the platform limit")
    return message


def matches_chat(actual_id: object, actual_type: object, expected_id: str) -> bool:
    return str(actual_id) == expected_id and actual_type in {"group", "supergroup"}


class Telegram:
    def __init__(self, token: str, chat_id: str) -> None:
        self.chat_id = chat_id
        self.client = httpx.Client(
            base_url=f"https://api.telegram.org/bot{token}/",
            timeout=20,
        )

    def close(self) -> None:
        self.client.close()

    def verify_destination(self) -> bool:
        response = self.client.post("getChat", data={"chat_id": self.chat_id})
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError("Telegram could not resolve the configured chat")
        result = body.get("result")
        return isinstance(result, dict) and matches_chat(
            result.get("id"), result.get("type"), self.chat_id
        )

    def send(self, message: str) -> int:
        response = self.client.post(
            "sendMessage",
            data={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError("Telegram rejected the alert")
        return int(body["result"]["message_id"])
