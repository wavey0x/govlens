"""The complete multi-source product flow, kept deliberately linear."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from .gist import Gist, GistError, GistUnknown
from .model import Proposal
from .presentation import Presentation, presentation_for
from .report import build_report, report_title
from .store import Store
from .telegram import Telegram, build_message

Audit = Callable[[Proposal], dict[str, Any]]
LOG = logging.getLogger(__name__)


class ProposalSource(Protocol):
    @property
    def source(self) -> str: ...

    @property
    def protocol(self) -> Presentation: ...

    def finalized_block(self) -> int: ...

    def count(self, block: int | None = None) -> int: ...

    def proposal(self, proposal_id: int, block: int | None = None) -> Proposal: ...


def run_once(
    store: Store,
    sources: Sequence[ProposalSource],
    audit: Audit,
    gist: Gist,
    telegrams: Mapping[str, Telegram],
) -> dict[str, Any]:
    run_started = time.monotonic()
    LOG.info("event=run_started sources=%d", len(sources))
    blocks: dict[str, int] = {}
    initialized: list[str] = []
    discovered = 0
    failures = 0

    for source in sources:
        label = f"{source.protocol.slug}:{source.source}"
        source_started = time.monotonic()
        source_discovered = 0
        LOG.info(
            "event=source_poll_started protocol=%s source=%s",
            source.protocol.slug,
            source.source,
        )
        try:
            block = source.finalized_block()
            blocks[label] = block
            count = source.count(block)
            if type(count) is not int or count < 0:
                raise RuntimeError(f"{label} returned an invalid proposal count")
            cursor = store.cursor(source.protocol.slug, source.source)
            if cursor is None:
                store.initialize(source.protocol.slug, source.source, count)
                initialized.append(label)
                LOG.info(
                    "event=source_initialized protocol=%s source=%s block=%d cursor=%d "
                    "duration_ms=%d",
                    source.protocol.slug,
                    source.source,
                    block,
                    count,
                    round((time.monotonic() - source_started) * 1_000),
                )
                continue
            if count < cursor:
                raise RuntimeError(f"{label} proposal count moved backwards")
            for proposal_id in range(cursor, count):
                proposal = source.proposal(proposal_id, block)
                if (
                    proposal.protocol != source.protocol.slug
                    or proposal.source != source.source
                    or proposal.id != proposal_id
                    or proposal.block != block
                ):
                    raise RuntimeError(f"{label} returned an inconsistent proposal")
                store.discover(proposal)
                discovered += 1
                source_discovered += 1
                LOG.info(
                    "event=proposal_discovered protocol=%s source=%s proposal=%d block=%d",
                    source.protocol.slug,
                    source.source,
                    proposal_id,
                    block,
                )
            LOG.info(
                "event=source_poll_completed protocol=%s source=%s block=%d cursor=%d count=%d "
                "discovered=%d duration_ms=%d",
                source.protocol.slug,
                source.source,
                block,
                cursor,
                count,
                source_discovered,
                round((time.monotonic() - source_started) * 1_000),
            )
        except Exception as exc:
            failures += 1
            LOG.error(
                "event=source_poll_failed protocol=%s source=%s error=%s duration_ms=%d",
                source.protocol.slug,
                source.source,
                type(exc).__name__,
                round((time.monotonic() - source_started) * 1_000),
            )

    published = 0
    sent = 0
    verified_destinations: set[str] = set()
    pending = store.pending()
    LOG.info("event=backlog_loaded proposals=%d", len(pending))
    for row in pending:
        proposal = row.proposal
        key = proposal.key
        protocol = presentation_for(proposal.protocol)
        analysis = row.analysis
        if analysis is None:
            analysis_started = time.monotonic()
            LOG.info(
                "event=analysis_started protocol=%s source=%s proposal=%d actions=%d",
                key.protocol,
                key.source,
                key.upstream_id,
                len(proposal.actions),
            )
            try:
                analysis = audit(proposal)
                store.save_analysis(key, analysis)
            except Exception as exc:
                failures += 1
                LOG.error(
                    "event=analysis_failed protocol=%s source=%s proposal=%d error=%s "
                    "duration_ms=%d",
                    key.protocol,
                    key.source,
                    key.upstream_id,
                    type(exc).__name__,
                    round((time.monotonic() - analysis_started) * 1_000),
                )
                continue
            LOG.info(
                "event=analysis_completed protocol=%s source=%s proposal=%d severity=%s "
                "duration_ms=%d",
                key.protocol,
                key.source,
                key.upstream_id,
                analysis["severity"],
                round((time.monotonic() - analysis_started) * 1_000),
            )
        telegram = telegrams.get(key.protocol)
        if telegram is None:
            failures += 1
            LOG.error(
                "event=telegram_destination_missing protocol=%s source=%s proposal=%d",
                key.protocol,
                key.source,
                key.upstream_id,
            )
            continue
        if key.protocol not in verified_destinations:
            try:
                if not telegram.verify_destination():
                    raise RuntimeError("configured Telegram chat ID did not resolve to a group")
                verified_destinations.add(key.protocol)
                LOG.info("event=telegram_destination_verified protocol=%s", key.protocol)
            except Exception as exc:
                failures += 1
                LOG.error(
                    "event=telegram_destination_failed protocol=%s error=%s",
                    key.protocol,
                    type(exc).__name__,
                )
                continue

        gist_url = row.gist_url
        if gist_url is None:
            try:
                markdown = build_report(protocol, proposal, analysis)
                title = report_title(protocol, proposal, analysis)
                if not gist.configured:
                    raise GistError("Wavey Gist is not configured")
            except Exception as exc:
                failures += 1
                LOG.error(
                    "event=report_failed protocol=%s source=%s proposal=%d error=%s",
                    key.protocol,
                    key.source,
                    key.upstream_id,
                    type(exc).__name__,
                )
                continue
            store.publishing(key)
            publication_started = time.monotonic()
            LOG.info(
                "event=gist_started protocol=%s source=%s proposal=%d",
                key.protocol,
                key.source,
                key.upstream_id,
            )
            try:
                published_gist = gist.publish(title, markdown)
            except GistUnknown as exc:
                store.review(key, exc.url)
                failures += 1
                LOG.error(
                    "event=gist_ambiguous protocol=%s source=%s proposal=%d duration_ms=%d",
                    key.protocol,
                    key.source,
                    key.upstream_id,
                    round((time.monotonic() - publication_started) * 1_000),
                )
                continue
            except Exception as exc:
                store.publication_failed(key)
                failures += 1
                LOG.error(
                    "event=gist_failed protocol=%s source=%s proposal=%d error=%s duration_ms=%d",
                    key.protocol,
                    key.source,
                    key.upstream_id,
                    type(exc).__name__,
                    round((time.monotonic() - publication_started) * 1_000),
                )
                continue
            gist_url = published_gist.url
            store.published(key, gist_url)
            published += 1
            LOG.info(
                "event=gist_completed protocol=%s source=%s proposal=%d duration_ms=%d",
                key.protocol,
                key.source,
                key.upstream_id,
                round((time.monotonic() - publication_started) * 1_000),
            )

        try:
            message = build_message(protocol, proposal, analysis, gist_url)
        except Exception as exc:
            failures += 1
            LOG.error(
                "event=telegram_render_failed protocol=%s source=%s proposal=%d error=%s",
                key.protocol,
                key.source,
                key.upstream_id,
                type(exc).__name__,
            )
            continue
        store.sending(key)
        send_started = time.monotonic()
        LOG.info(
            "event=telegram_started protocol=%s source=%s proposal=%d",
            key.protocol,
            key.source,
            key.upstream_id,
        )
        try:
            message_id = telegram.send(message)
            store.sent(key, message_id)
            sent += 1
            LOG.info(
                "event=telegram_completed protocol=%s source=%s proposal=%d message_id=%d "
                "duration_ms=%d",
                key.protocol,
                key.source,
                key.upstream_id,
                message_id,
                round((time.monotonic() - send_started) * 1_000),
            )
        except Exception as exc:
            store.review(key, gist_url)
            failures += 1
            LOG.error(
                "event=telegram_ambiguous protocol=%s source=%s proposal=%d error=%s "
                "duration_ms=%d",
                key.protocol,
                key.source,
                key.upstream_id,
                type(exc).__name__,
                round((time.monotonic() - send_started) * 1_000),
            )

    all_initialized = bool(sources) and len(initialized) == len(sources)
    status = "initialized" if all_initialized and failures == 0 else "ok"
    if failures:
        status = "attention"
    states = store.status_counts()
    summary = {
        "status": status,
        "blocks": blocks,
        "initialized_sources": initialized,
        "discovered": discovered,
        "published": published,
        "sent": sent,
        "failures": failures,
        "states": states,
    }
    LOG.info(
        "event=run_completed status=%s discovered=%d published=%d sent=%d failures=%d "
        "states=%s duration_ms=%d",
        status,
        discovered,
        published,
        sent,
        failures,
        json.dumps(states, sort_keys=True, separators=(",", ":")),
        round((time.monotonic() - run_started) * 1_000),
    )
    return summary
