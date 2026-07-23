"""Minimal, checked-in presentation metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from .model import Proposal


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Presentation:
    slug: str
    name: str
    facts: tuple[Field, ...]
    links: tuple[Field, ...]

    def validate_url(self, field: Field, url: str) -> str:
        parsed = urlparse(url)
        try:
            valid = (
                parsed.scheme == "https"
                and parsed.hostname in field.hosts
                and parsed.port is None
                and not parsed.username
                and not parsed.password
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"{self.slug} {field.key} URL is not allowlisted")
        return url


_PRESENTATIONS = {
    "curve": Presentation(
        slug="curve",
        name="Curve",
        facts=(
            Field("proposer", "Proposer", ("etherscan.io",)),
            Field("vote_type", "Vote Type"),
            Field("quorum", "Minimum Quorum"),
            Field("ends_at", "Ends"),
        ),
        links=(
            Field("etherscan", "Etherscan", ("etherscan.io",)),
            Field("curve", "Curve", ("www.curve.finance",)),
        ),
    ),
    "resupply": Presentation(
        slug="resupply",
        name="Resupply",
        facts=(
            Field("proposer", "Proposer", ("etherscan.io",)),
            Field("epoch", "Epoch"),
            Field("quorum", "Quorum Required"),
            Field("ends_at", "Ends"),
        ),
        links=(
            Field("etherscan", "Etherscan", ("etherscan.io",)),
            Field("resupply", "Resupply", ("resupply.finance",)),
            Field("hippo", "Hippo Army", ("hippo.army",)),
        ),
    ),
}


def presentation_for(slug: str) -> Presentation:
    try:
        return _PRESENTATIONS[slug]
    except KeyError:
        raise ValueError(f"unknown protocol {slug}") from None


def validate_presentation(presentation: Presentation, proposal: Proposal) -> None:
    if proposal.protocol != presentation.slug:
        raise ValueError("proposal protocol does not match presentation")
    configured_facts = {field.key: field for field in presentation.facts}
    configured_links = {field.key: field for field in presentation.links}
    if set(proposal.facts) - set(configured_facts):
        raise ValueError("proposal contains an unconfigured presentation fact")
    if set(proposal.links) - set(configured_links):
        raise ValueError("proposal contains an unconfigured presentation link")
    for key, fact in proposal.facts.items():
        if fact.url:
            presentation.validate_url(configured_facts[key], fact.url)
    for key, url in proposal.links.items():
        presentation.validate_url(configured_links[key], url)
