"""Render the complete, self-contained Markdown audit report."""

from __future__ import annotations

from typing import Any

from .model import Proposal
from .presentation import Presentation, validate_presentation


def _markdown(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for character in ("`", "*", "_", "{", "}", "[", "]", "<", ">", "#"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _link(label: str, url: str) -> str:
    return f"[{_markdown(label)}]({url})"


def report_title(protocol: Presentation, proposal: Proposal, analysis: dict[str, Any]) -> str:
    name = protocol.name
    if proposal.source != proposal.protocol:
        name = f"{name} {proposal.source.title()}"
    return f"{name} Proposal {proposal.id} Audit — {analysis['severity']}"


def build_report(
    protocol: Presentation,
    proposal: Proposal,
    analysis: dict[str, Any],
) -> str:
    validate_presentation(protocol, proposal)
    title = report_title(protocol, proposal, analysis)
    lines = [
        f"# {_markdown(title)}",
        "",
        f"**Severity:** {_markdown(analysis['severity'])}",
        "",
        "## Summary",
        "",
        " ".join(_markdown(item) for item in analysis["summary_sentences"]),
        "",
        "## Proposal",
        "",
        f"**Title:** {_markdown(proposal.title)}",
    ]
    for field in protocol.facts:
        fact = proposal.facts.get(field.key)
        if fact is None:
            continue
        value = _link(fact.value, fact.url) if fact.url else _markdown(fact.value)
        lines.append(f"**{_markdown(field.label)}:** {value}")
    lines.extend(
        (
            f"**Creation block:** {proposal.creation_block:,}",
            f"**Finalized block:** {proposal.block:,}",
            "",
            "### Links",
            "",
        )
    )
    lines.append(
        " · ".join(
            _link(field.label, proposal.links[field.key])
            for field in protocol.links
            if field.key in proposal.links
        )
    )
    lines.extend(("", "### Proposal text", "", _markdown(proposal.description), ""))

    if proposal.raw_payload is not None:
        lines.extend(
            (
                "### Complete governance payload",
                "",
                "```text",
                proposal.raw_payload,
                "```",
                "",
            )
        )

    lines.append("## Action analysis")
    for action, assessment in zip(proposal.actions, analysis["actions"], strict=True):
        lines.extend(
            (
                "",
                f"### Action {action.index + 1}",
                "",
                f"**Executor:** `{action.executor}`",
                f"**Target:** `{action.target}`",
                f"**Value:** {action.value_wei:,} wei",
                "",
                "**Complete calldata:**",
                "",
                "```text",
                action.calldata,
                "```",
            )
        )
        if action.raw is not None and action.raw != action.calldata:
            lines.extend(("", "**Exact raw action bytes:**", "", "```text", action.raw, "```"))
        if action.unresolved:
            lines.extend(("", f"**Unresolved:** {_markdown(action.unresolved)}"))
        lines.extend(
            (
                "",
                f"**Effect:** {_markdown(assessment['effect'])}",
                "",
                f"**Risk:** {_markdown(assessment['risk'])}",
            )
        )

    lines.extend(("", "## Protocol checks", ""))
    checks = analysis.get("checks", [])
    if not checks:
        lines.append("No deterministic check applied.")
    for check in checks:
        lines.extend(
            (
                f"### Action {int(check['action_index']) + 1}: "
                f"{_markdown(str(check['id']))} — {_markdown(str(check['status']))}",
                "",
                _markdown(str(check["summary"])),
            )
        )
        for item in check["evidence"]:
            lines.extend(
                (
                    "",
                    f"- Chain {int(item['chain_id'])}, block {int(item['block']):,}, "
                    f"target `{item['target']}`",
                    "",
                    "  ```text",
                    f"  request: {item['request']}",
                    f"  result: {item['raw_result']}",
                    "  ```",
                )
            )

    lines.extend(("", "## Findings", ""))
    if analysis["findings"]:
        lines.extend(f"- {_markdown(item)}" for item in analysis["findings"])
    else:
        lines.append("No material findings.")

    lines.extend(("", "## Unknowns", ""))
    unknowns = list(dict.fromkeys([*proposal.unknowns, *analysis["unknowns"]]))
    if unknowns:
        lines.extend(f"- {_markdown(item)}" for item in unknowns)
    else:
        lines.append("None.")

    return "\n".join(lines).rstrip() + "\n"
