"""Protocol-neutral normalized governance proposal model."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

MAX_TITLE_CHARS = 512


def proposal_title(description: str, fallback: str) -> str:
    title = next((line.strip() for line in description.splitlines() if line.strip()), fallback)
    if len(title) <= MAX_TITLE_CHARS:
        return title
    prefix = title[: MAX_TITLE_CHARS - 1].rstrip()
    if " " in prefix:
        prefix = prefix.rsplit(" ", 1)[0]
    return prefix + "…"


@dataclass(frozen=True, order=True)
class ProposalKey:
    protocol: str
    source: str
    upstream_id: int


@dataclass(frozen=True)
class Fact:
    value: str
    url: str | None = None


@dataclass(frozen=True)
class Action:
    index: int
    executor: str
    target: str
    value_wei: int
    calldata: str
    raw: str | None = None
    unresolved: str | None = None


@dataclass(frozen=True)
class Proposal:
    protocol: str
    source: str
    id: int
    title: str
    description: str
    created_at: int
    creation_block: int
    voter: str
    executor: str
    block: int
    raw_payload: str | None
    facts: dict[str, Fact]
    links: dict[str, str]
    actions: list[Action]
    unknowns: tuple[str, ...]

    @property
    def key(self) -> ProposalKey:
        return ProposalKey(self.protocol, self.source, self.id)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
