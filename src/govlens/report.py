"""Render the complete, self-contained Markdown audit report."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from .model import Proposal
from .presentation import Presentation, validate_presentation

_ADDRESS = re.compile(r"0x[0-9A-Fa-f]{40}")
_ADDRESS_TOKEN = re.compile(r"(?<![A-Za-z0-9_/])0x[0-9A-Fa-f]{40}(?![0-9A-Fa-f])")
_TRANSACTION_PATH = re.compile(r"/tx/(0x[0-9A-Fa-f]{64})")


def _markdown(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for character in ("`", "*", "_", "{", "}", "[", "]", "<", ">", "#"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _link(label: str, url: str) -> str:
    return f"[{_markdown(label)}]({url})"


def _inline_code(value: str) -> str:
    safe = value.replace("`", "\\x60").replace("\r", " ").replace("\n", " ")
    return f"`{safe}`"


def _short_hex(value: str) -> str:
    return f"{value[:6]}…{value[-4:]}"


def _address_link(address: str) -> str:
    if not _ADDRESS.fullmatch(address):
        return f"`{_markdown(address)}`"
    return f"[`{_short_hex(address)}`](https://etherscan.io/address/{address})"


def _prose(value: str) -> str:
    rendered: list[str] = []
    cursor = 0
    for match in _ADDRESS_TOKEN.finditer(value):
        rendered.append(_markdown(value[cursor : match.start()]))
        rendered.append(_address_link(match.group()))
        cursor = match.end()
    rendered.append(_markdown(value[cursor:]))
    return "".join(rendered)


def _transaction_link(url: str, fallback: str) -> str:
    parsed = urlparse(url)
    match = _TRANSACTION_PATH.fullmatch(parsed.path)
    if parsed.hostname == "etherscan.io" and match:
        transaction = match.group(1)
        return f"[`tx {_short_hex(transaction)}`]({url})"
    return _link(fallback, url)


def _description(proposal: Proposal) -> list[str]:
    if not proposal.description.strip() or proposal.description.strip() == proposal.title.strip():
        return []
    quoted = [
        f"> {_prose(line)}" if line else ">" for line in proposal.description.strip().splitlines()
    ]
    return ["", *quoted]


def _decoded_actions(analysis: dict[str, Any]) -> dict[int, dict[str, Any]]:
    decoded: dict[int, dict[str, Any]] = {}
    for call in analysis.get("decoded_actions", []):
        if not isinstance(call, dict) or type(call.get("action_index")) is not int:
            continue
        function = call.get("function")
        inputs = call.get("inputs")
        if not isinstance(function, str) or not isinstance(inputs, list):
            continue
        if not all(
            isinstance(item, dict)
            and all(isinstance(item.get(key), str) for key in ("name", "type", "value"))
            for item in inputs
        ):
            continue
        decoded[call["action_index"]] = call
    return decoded


def _decoded_value(value: str, solidity_type: str) -> str:
    return _prose(value) if "address" in solidity_type else _markdown(value)


def _raw_evidence(proposal: Proposal, analysis: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    if proposal.raw_payload is not None:
        evidence.append(f"governance_payload={proposal.raw_payload}")
    for action in proposal.actions:
        label = action.index + 1
        evidence.append(f"action[{label}].calldata={action.calldata}")
        if action.raw is not None and action.raw != action.calldata:
            evidence.append(f"action[{label}].raw={action.raw}")
    for check_index, check in enumerate(analysis.get("checks", []), 1):
        evidence.append(
            f"check[{check_index}].id={check['id']} status={check['status']} "
            f"action={int(check['action_index']) + 1}"
        )
        for item_index, item in enumerate(check["evidence"], 1):
            prefix = f"check[{check_index}].evidence[{item_index}]"
            evidence.extend(
                (
                    f"{prefix}.chain_id={int(item['chain_id'])} "
                    f"block={int(item['block'])} target={item['target']}",
                    f"{prefix}.request={item['request']}",
                    f"{prefix}.result={item['raw_result']}",
                )
            )
    return evidence


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
        f"**{_markdown(proposal.title)}**",
    ]
    facts: list[str] = []
    for field in protocol.facts:
        fact = proposal.facts.get(field.key)
        if fact is None:
            continue
        value = _link(fact.value, fact.url) if fact.url else _markdown(fact.value)
        facts.append(f"**{_markdown(field.label)}:** {value}")
    if facts:
        lines.extend(("", " · ".join(facts)))

    links: list[str] = []
    for field in protocol.links:
        url = proposal.links.get(field.key)
        if url is None:
            continue
        links.append(
            _transaction_link(url, field.label)
            if field.key == "etherscan"
            else _link(field.label, url)
        )
    links.extend(
        (
            f"[created block {proposal.creation_block:,}]"
            f"(https://etherscan.io/block/{proposal.creation_block})",
            f"[finalized block {proposal.block:,}](https://etherscan.io/block/{proposal.block})",
        )
    )
    lines.extend(("", " · ".join(links), *_description(proposal)))

    lines.extend(
        (
            "",
            "## Summary",
            "",
            f"**Risk: {_markdown(analysis['severity'])}** — {_prose(analysis['summary'])}",
        )
    )
    summary = str(analysis["summary"]).strip().casefold()
    for finding in analysis["findings"]:
        if finding.strip().casefold() != summary:
            lines.append(f"- {_prose(finding)}")

    unknowns = list(dict.fromkeys([*proposal.unknowns, *analysis["unknowns"]]))
    lines.extend(f"- **Unknown:** {_prose(item)}" for item in unknowns)

    for check in analysis.get("checks", []):
        lines.append(
            f"- **Check — {_markdown(str(check['id']))} "
            f"({_markdown(str(check['status']))}):** {_prose(str(check['summary']))}"
        )

    lines.extend(("", "## Actions"))
    decoded = _decoded_actions(analysis)
    executors = {action.executor.casefold(): action.executor for action in proposal.actions}
    values = {action.value_wei for action in proposal.actions}
    shared_executor = next(iter(executors.values())) if len(executors) == 1 else None
    shared_value = next(iter(values)) if len(values) == 1 else None
    action_facts: list[str] = []
    if shared_executor is not None:
        action_facts.append(f"**Executor:** {_address_link(shared_executor)}")
    if shared_value is not None:
        suffix = " for all calls" if len(proposal.actions) > 1 else ""
        action_facts.append(f"**Value:** {shared_value:,} wei{suffix}")
    if action_facts:
        lines.extend(("", " · ".join(action_facts)))
    if not proposal.actions:
        lines.extend(("", "No executable actions."))

    for action in proposal.actions:
        call = decoded.get(action.index)
        target = _address_link(action.target)
        if call is not None:
            inputs = call["inputs"]
            call_label = _inline_code(call["function"])
        else:
            inputs = []
            selector = action.calldata[:10] if len(action.calldata) >= 10 else action.calldata
            call_label = _inline_code(f"undecoded {selector}")
        qualifiers: list[str] = []
        if shared_executor is None:
            qualifiers.append(f"via {_address_link(action.executor)}")
        if shared_value is None:
            qualifiers.append(f"{action.value_wei:,} wei")
        if action.unresolved:
            qualifiers.append(f"**Unresolved:** {_prose(action.unresolved)}")
        suffix = f" · {' · '.join(qualifiers)}" if qualifiers else ""
        lines.extend(("", f"{action.index + 1}. {target} · {call_label}{suffix}"))
        if inputs:
            lines.append(
                "   "
                + " · ".join(
                    f"{_inline_code(item['name'])} = {_decoded_value(item['value'], item['type'])}"
                    for item in inputs
                )
            )

    evidence = _raw_evidence(proposal, analysis)
    if evidence:
        lines.extend(("", "### Raw evidence", "", "```text", *evidence, "```"))

    return "\n".join(lines).rstrip() + "\n"
