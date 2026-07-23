from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from govlens.audit import AuditError, _validate
from govlens.cli import _configure_logging, _run_lock
from govlens.config import Settings
from govlens.gist import Gist, GistUnknown, PublishedGist
from govlens.model import MAX_TITLE_CHARS, Action, Fact, Proposal, proposal_title
from govlens.pipeline import run_once
from govlens.presentation import Field, Presentation, presentation_for
from govlens.report import build_report
from govlens.store import Store
from govlens.telegram import build_message, matches_chat

FIXTURES = Path(__file__).parent / "fixtures"
GIST_URL = "https://gist.wavey.info/AbCdEfGhIjKlMnOp"


def proposal_23() -> Proposal:
    raw = json.loads((FIXTURES / "resupply_proposal_23.json").read_text())
    proposer = raw["proposer"]
    transaction = raw["creation_transaction"]
    return Proposal(
        protocol="resupply",
        source="resupply",
        id=23,
        title=raw["title"],
        description=raw["description"],
        created_at=raw["created_at"],
        creation_block=raw["discovery_block"],
        voter="0x11111111063874cE8dC6232cb5C1C849359476E6",
        executor=raw["actions"][0]["executor"],
        block=raw["discovery_block"],
        raw_payload=None,
        facts={
            "proposer": Fact(
                value=f"{proposer[:6]}…{proposer[-4:]}",
                url=f"https://etherscan.io/address/{proposer}",
            ),
            "epoch": Fact(value=str(raw["epoch"])),
            "quorum": Fact(value=f"{raw['quorum']:,}"),
            "ends_at": Fact(value=raw["ends_at"]),
        },
        links={
            "etherscan": f"https://etherscan.io/tx/{transaction}",
            "resupply": raw["canonical_url"],
            "hippo": f"https://hippo.army/dao/proposal/{raw['hippo_proposal_id']}",
        },
        actions=[
            Action(
                index=index,
                executor=action["executor"],
                target=action["target"],
                value_wei=action["value_wei"],
                calldata=action["calldata"],
            )
            for index, action in enumerate(raw["actions"])
        ],
        unknowns=(),
    )


def curve_proposal(proposal_id: int) -> Proposal:
    proposal = proposal_23()
    return replace(
        proposal,
        protocol="curve",
        source="ownership",
        id=proposal_id,
        facts={
            "proposer": proposal.facts["proposer"],
            "vote_type": Fact("Ownership"),
            "quorum": Fact("30.00%"),
            "ends_at": proposal.facts["ends_at"],
        },
        links={
            "etherscan": proposal.links["etherscan"],
            "curve": f"https://www.curve.finance/dao/vote/ownership/{proposal_id}",
        },
    )


def high_analysis() -> dict[str, Any]:
    expected = json.loads((FIXTURES / "resupply_proposal_23.json").read_text())["expected_failure"]
    return {
        "severity": "HIGH",
        "summary": (
            "The replacement PairAdder cannot operate through the real governance path. "
            "Its nested Core.execute call deterministically reverts under the reentrancy "
            "guard, and revoking the old permission removes the functioning fallback."
        ),
        "findings": [f"{expected['path']} reverts with {expected['result']}."],
        "unknowns": [],
    }


def low_analysis() -> dict[str, Any]:
    result = high_analysis()
    result["severity"] = "LOW"
    result["summary"] = "The proposal has bounded, understood effects."
    result["findings"] = []
    return result


class FakeSource:
    def __init__(self, proposals: list[Proposal]) -> None:
        self.proposals = proposals
        self.protocol = presentation_for("resupply")
        self.source = "resupply"

    def finalized_block(self) -> int:
        return 100

    def count(self, block: int | None = None) -> int:
        return len(self.proposals)

    def proposal(self, proposal_id: int, block: int | None = None) -> Proposal:
        return replace(
            self.proposals[proposal_id],
            block=self.finalized_block() if block is None else block,
        )


class FakeGist:
    configured = True

    def __init__(self, events: list[str] | None = None, *, unknown: bool = False) -> None:
        self.markdown: list[str] = []
        self.events = events if events is not None else []
        self.unknown = unknown

    def publish(self, title: str, markdown: str) -> PublishedGist:
        self.events.append("gist")
        self.markdown.append(markdown)
        if self.unknown:
            raise GistUnknown("unclear", GIST_URL)
        return PublishedGist(url=GIST_URL, revision=1, sha256="a" * 64)


class FakeTelegram:
    def __init__(self, events: list[str] | None = None, *, valid: bool = True) -> None:
        self.messages: list[str] = []
        self.verify_calls = 0
        self.events = events if events is not None else []
        self.valid = valid

    def verify_destination(self) -> bool:
        self.verify_calls += 1
        self.events.append("verify")
        return self.valid

    def send(self, message: str) -> int:
        self.events.append("telegram")
        self.messages.append(message)
        return 99


def test_proposal_preserves_every_action_byte() -> None:
    proposal = proposal_23()
    original = json.loads((FIXTURES / "resupply_proposal_23.json").read_text())

    assert len(proposal.actions) == 3
    assert [action.calldata for action in proposal.actions] == [
        action["calldata"] for action in original["actions"]
    ]


def test_investigation_uses_a_small_contract_without_prose_rules() -> None:
    proposal = proposal_23()
    assert _validate(high_analysis(), proposal)["severity"] == "HIGH"

    numeric = high_analysis()
    numeric["score"] = 3
    del numeric["severity"]
    with pytest.raises(AuditError, match="invalid_result_shape"):
        _validate(numeric, proposal)

    extra_field = high_analysis()
    extra_field["confidence"] = "high"
    with pytest.raises(AuditError, match="invalid_result_shape"):
        _validate(extra_field, proposal)

    too_long = high_analysis()
    too_long["summary"] = "x" * 1_001
    with pytest.raises(AuditError, match="invalid_summary"):
        _validate(too_long, proposal)

    natural = high_analysis()
    natural["summary"] = (
        "Action 0 replaces the PairAdder.\n"
        "The new path remains bounded by Core permissions; see https://example.invalid."
    )
    assert _validate(natural, proposal)["summary"] == natural["summary"]

    unknown_low = low_analysis()
    unknown_low["unknowns"] = ["Execution could not be simulated."]
    assert _validate(unknown_low, proposal)["severity"] == "MEDIUM"

    linked = high_analysis()
    linked["findings"] = ["See https://evil.example for details."]
    assert _validate(linked, proposal)["findings"] == linked["findings"]


def test_telegram_is_compact_and_orders_metadata_audit_then_links() -> None:
    protocol = presentation_for("resupply")
    message = build_message(protocol, proposal_23(), high_analysis(), GIST_URL, test=True)

    assert "New Resupply Proposal Created" in message
    assert "Proposal 23:" in message
    assert "0xB813…61b6" in message
    assert "Quorum Required:</b> 7,647,591" in message
    assert "🔴 <b>Risk: HIGH</b>" in message
    assert "confidence" not in message.casefold()
    assert message.count("Core.execute") == 1
    assert "<b>Action " not in message
    assert "ReentrancyGuardReentrantCall" not in message
    assert "Full Audit" in message
    assert "Etherscan" in message
    assert "Resupply" in message
    assert "Hippo Army" in message
    assert message.index("Quorum Required") < message.index("Risk: HIGH")
    assert message.index("Risk: HIGH") < message.index("Full Audit")


def test_proposal_title_preserves_curve_1451_and_marks_real_truncation() -> None:
    title = (
        "Activate sDOLA-crvUSD and sfrxUSD-crvUSD Llamalend V2 markets on ETH mainnet by "
        "setting borrow cap (12.4M crvUSD cap for sDOLA collateral, 28.4M crvUSD cap for "
        "sfrxUSD collateral) and set admin fee percentage 10%."
    )

    assert proposal_title(title, "fallback") == title
    shortened = proposal_title("word " * MAX_TITLE_CHARS, "fallback")
    assert len(shortened) <= MAX_TITLE_CHARS
    assert shortened.endswith("…")


def test_full_report_contains_complete_analysis_and_every_calldata_byte() -> None:
    proposal = proposal_23()
    report = build_report(presentation_for("resupply"), proposal, high_analysis())

    assert report.startswith("# Resupply Proposal 23 Audit — HIGH")
    assert "ReentrancyGuardReentrantCall" in report
    assert "## Unknowns\n\nNone." in report
    for action in proposal.actions:
        assert action.calldata in report
        assert action.executor in report
        assert action.target in report


def test_report_renders_raw_payload_parent_checks_and_normalization_unknowns() -> None:
    proposal = proposal_23()
    first = replace(
        proposal.actions[0],
        raw="0xdeadbeef",
        unresolved="Fixture wrapper is unresolved",
    )
    proposal = replace(
        proposal,
        raw_payload="0xfeedface",
        actions=[first, *proposal.actions[1:]],
        unknowns=("Fixture proposal unknown",),
    )
    analysis = high_analysis()
    analysis["checks"] = [
        {
            "id": "fixture.provenance",
            "action_index": 0,
            "status": "PASS",
            "summary": "Fixture provenance is proven.",
            "evidence": [
                {
                    "chain_id": 1,
                    "block": proposal.creation_block,
                    "target": proposal.actions[0].target,
                    "request": "0x12345678",
                    "raw_result": "0x01",
                }
            ],
        }
    ]

    report = build_report(presentation_for("resupply"), proposal, analysis)

    assert "## Protocol checks" in report
    assert "fixture.provenance — PASS" in report
    assert "request: 0x12345678" in report
    assert "result: 0x01" in report
    assert "0xfeedface" in report
    assert "0xdeadbeef" in report
    assert "Fixture wrapper is unresolved" in report
    assert "Fixture proposal unknown" in report


def test_shared_renderer_handles_a_protocol_with_one_link() -> None:
    protocol = Presentation(
        slug="simple",
        name="Simple DAO",
        facts=(Field("ends_at", "Ends"),),
        links=(Field("official", "Official", ("dao.example",)),),
    )
    proposal = replace(
        proposal_23(),
        protocol="simple",
        source="simple",
        facts={"ends_at": Fact("2026-07-02 16:05 UTC")},
        links={"official": "https://dao.example/proposals/23"},
    )
    message = build_message(protocol, proposal, high_analysis(), GIST_URL)

    assert "New Simple DAO Proposal Created" in message
    assert "Official" in message
    assert "Etherscan" not in message
    assert "Hippo Army" not in message


def test_presentation_fields_are_optional() -> None:
    protocol = presentation_for("resupply")
    proposal = replace(
        proposal_23(),
        facts={"ends_at": Fact("2026-07-02 16:05 UTC")},
        links={"resupply": "https://resupply.finance/governance/proposals?id=23"},
    )

    message = build_message(protocol, proposal, high_analysis(), None)

    assert "Ends" in message
    assert "Resupply" in message
    assert "Proposer" not in message
    assert "Etherscan" not in message


def test_presentation_is_an_exact_url_allowlist() -> None:
    protocol = presentation_for("resupply")
    proposal = proposal_23()
    malicious = replace(
        proposal,
        links={**proposal.links, "etherscan": "https://evil.example/tx/0x123"},
    )
    with pytest.raises(ValueError, match="allowlisted"):
        build_message(protocol, malicious, high_analysis(), GIST_URL)


def test_settings_load_explicit_protocol_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {
        "ETHEREUM_RPC_URL": "https://rpc.example",
        "GOVLENS_DB": str(tmp_path / "state.db"),
        "WAVEY_GIST_API_KEY": "gist-key",
        "TELEGRAM_BOT_TOKEN": "bot-token",
        "CURVE_PROPOSALS_CHAT": "WAVEY_ALERTS_CHAT_ID",
        "RESUPPLY_PROPOSALS_CHAT": "RESUPPLY_MULTISIG_CHAT_ID",
        "WAVEY_ALERTS_CHAT_ID": "-1001",
        "RESUPPLY_MULTISIG_CHAT_ID": "-1002",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    settings = Settings.load()

    assert settings.telegram_targets == {
        "curve": "-1001",
        "resupply": "-1002",
    }
    monkeypatch.setenv("CURVE_PROPOSALS_CHAT", "curve-id")
    with pytest.raises(RuntimeError, match="CURVE_PROPOSALS_CHAT"):
        Settings.load()

    monkeypatch.setenv("CURVE_PROPOSALS_CHAT", "WAVEY_ALERTS_CHAT_ID")
    monkeypatch.setenv("WAVEY_ALERTS_CHAT_ID", "not-a-chat-id")
    with pytest.raises(RuntimeError, match="numeric Telegram chat ID"):
        Settings.load()


def test_first_run_ignores_history_then_gist_precedes_one_telegram(tmp_path: Path) -> None:
    proposal = proposal_23()
    source = FakeSource([proposal])
    events: list[str] = []
    gist = FakeGist(events)
    telegram = FakeTelegram(events)
    store = Store(tmp_path / "state.db")

    first = run_once(store, [source], lambda _: high_analysis(), gist, {"resupply": telegram})  # type: ignore[list-item, arg-type]
    assert first["status"] == "initialized"
    assert telegram.messages == []

    source.proposals.append(replace(proposal, id=1))
    second = run_once(store, [source], lambda _: high_analysis(), gist, {"resupply": telegram})  # type: ignore[list-item, arg-type]
    third = run_once(store, [source], lambda _: high_analysis(), gist, {"resupply": telegram})  # type: ignore[list-item, arg-type]

    assert second["published"] == 1
    assert second["sent"] == 1
    assert third["sent"] == 0
    assert events == ["verify", "gist", "telegram"]
    assert len(gist.markdown) == 1
    assert len(telegram.messages) == 1
    assert store.cursor("resupply", "resupply") == 2
    store.close()


def test_routes_delivery_by_protocol_and_verifies_each_once(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.discover(curve_proposal(0))
    store.discover(replace(proposal_23(), id=0))
    store.discover(replace(proposal_23(), id=1))
    gist = FakeGist()
    curve = FakeTelegram()
    resupply = FakeTelegram()

    result = run_once(
        store,
        [],
        lambda _: high_analysis(),
        gist,
        {"curve": curve, "resupply": resupply},  # type: ignore[dict-item]
    )

    store.close()
    assert result["sent"] == 3
    assert curve.verify_calls == 1
    assert resupply.verify_calls == 1
    assert len(curve.messages) == 1
    assert len(resupply.messages) == 2
    assert "New Curve Ownership Proposal Created" in curve.messages[0]
    assert all("New Resupply Proposal Created" in message for message in resupply.messages)


def test_invalid_destination_prevents_gist_and_telegram(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.discover(replace(proposal_23(), id=0))
    gist = FakeGist()
    telegram = FakeTelegram(valid=False)

    result = run_once(
        store,
        [],
        lambda _: high_analysis(),
        gist,
        {"resupply": telegram},  # type: ignore[dict-item]
    )

    states = store.status_counts()
    store.close()
    assert result["failures"] == 1
    assert states == {"analyzed": 1}
    assert gist.markdown == []
    assert telegram.messages == []
    assert telegram.events == ["verify"]


def test_two_proposals_are_failure_isolated(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = FakeSource([replace(proposal_23(), id=0), replace(proposal_23(), id=1)])
    gist = FakeGist()
    telegram = FakeTelegram()
    store = Store(tmp_path / "state.db")
    store.initialize("resupply", "resupply", 0)
    attempts: list[int] = []

    def audit(proposal: Proposal) -> dict[str, Any]:
        attempts.append(proposal.id)
        if proposal.id == 0:
            raise RuntimeError("untrusted failure detail")
        return high_analysis()

    with caplog.at_level(logging.INFO, logger="govlens.pipeline"):
        result = run_once(store, [source], audit, gist, {"resupply": telegram})  # type: ignore[list-item, arg-type]

    states = store.status_counts()
    store.close()
    assert attempts == [0, 1]
    assert result["discovered"] == 2
    assert result["failures"] == 1
    assert result["sent"] == 1
    assert states == {"pending": 1, "sent": 1}
    assert "event=analysis_failed" in caplog.text
    assert "error=RuntimeError" in caplog.text
    assert "untrusted failure detail" not in caplog.text
    assert "event=run_completed" in caplog.text


def test_inconsistent_source_proposal_does_not_advance_cursor(tmp_path: Path) -> None:
    source = FakeSource([replace(proposal_23(), id=1)])
    store = Store(tmp_path / "state.db")
    store.initialize("resupply", "resupply", 0)

    result = run_once(
        store,
        [source],
        lambda _: high_analysis(),
        FakeGist(),
        {"resupply": FakeTelegram()},  # type: ignore[list-item, arg-type]
    )

    assert result["failures"] == 1
    assert result["discovered"] == 0
    assert store.cursor("resupply", "resupply") == 0
    assert store.status_counts() == {}
    store.close()


def test_production_run_lock_rejects_overlap(tmp_path: Path) -> None:
    database = tmp_path / "state.db"

    with _run_lock(database) as first:
        assert first
        with _run_lock(database) as second:
            assert not second

    with _run_lock(database) as after_release:
        assert after_release


def test_logging_enables_only_govlens_info() -> None:
    root = logging.getLogger()
    package = logging.getLogger("govlens")
    dependencies = [logging.getLogger(name) for name in ("httpx", "httpcore", "urllib3", "web3")]
    root_level = root.level
    package_level = package.level
    dependency_levels = [logger.level for logger in dependencies]
    try:
        _configure_logging()
        assert root.getEffectiveLevel() == logging.WARNING
        assert all(logger.getEffectiveLevel() >= logging.WARNING for logger in dependencies)
        assert package.getEffectiveLevel() == logging.INFO
    finally:
        root.setLevel(root_level)
        package.setLevel(package_level)
        for logger, level in zip(dependencies, dependency_levels, strict=True):
            logger.setLevel(level)


def test_low_severity_publishes_and_sends(tmp_path: Path) -> None:
    proposal = replace(proposal_23(), id=0)
    source = FakeSource([proposal])
    gist = FakeGist()
    telegram = FakeTelegram()
    store = Store(tmp_path / "state.db")
    store.initialize("resupply", "resupply", 0)

    result = run_once(store, [source], lambda _: low_analysis(), gist, {"resupply": telegram})  # type: ignore[list-item, arg-type]

    assert result["published"] == 1
    assert result["sent"] == 1
    assert len(gist.markdown) == 1
    assert len(telegram.messages) == 1
    status = store.connection.execute(
        "SELECT status FROM proposals WHERE protocol='resupply' "
        "AND source='resupply' AND upstream_id=0"
    ).fetchone()[0]
    assert status == "sent"
    store.close()


def test_fallback_analysis_is_saved_delivered_and_not_retried(tmp_path: Path) -> None:
    proposal = replace(proposal_23(), id=0)
    source = FakeSource([proposal])
    gist = FakeGist()
    telegram = FakeTelegram()
    store = Store(tmp_path / "state.db")
    store.initialize("resupply", "resupply", 0)
    attempts = 0

    def audit(_proposal: Proposal) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        return {
            "severity": "MEDIUM",
            "summary": "Automated analysis was incomplete, so manual review is required.",
            "findings": [],
            "unknowns": ["The Codex investigation did not return a usable result."],
            "checks": [],
        }

    first = run_once(
        store,
        [source],
        audit,
        gist,
        {"resupply": telegram},  # type: ignore[dict-item]
    )
    second = run_once(
        store,
        [source],
        audit,
        gist,
        {"resupply": telegram},  # type: ignore[dict-item]
    )

    assert attempts == 1
    assert first["published"] == 1
    assert first["sent"] == 1
    assert second["published"] == 0
    assert second["sent"] == 0
    assert len(gist.markdown) == 1
    assert len(telegram.messages) == 1
    assert store.status_counts() == {"sent": 1}
    store.close()


def test_ambiguous_gist_is_never_retried_or_sent(tmp_path: Path) -> None:
    proposal = replace(proposal_23(), id=0)
    source = FakeSource([proposal])
    gist = FakeGist(unknown=True)
    telegram = FakeTelegram()
    database = tmp_path / "state.db"
    store = Store(database)
    store.initialize("resupply", "resupply", 0)

    first = run_once(store, [source], lambda _: high_analysis(), gist, {"resupply": telegram})  # type: ignore[list-item, arg-type]
    store.close()
    reopened = Store(database)
    second = run_once(reopened, [source], lambda _: high_analysis(), gist, {"resupply": telegram})  # type: ignore[list-item, arg-type]

    row = reopened.connection.execute(
        "SELECT status, gist_url FROM proposals WHERE protocol='resupply' "
        "AND source='resupply' AND upstream_id=0"
    ).fetchone()
    assert first["failures"] == 1
    assert second["failures"] == 0
    assert row["status"] == "review"
    assert row["gist_url"] == GIST_URL
    assert len(gist.markdown) == 1
    assert telegram.messages == []
    reopened.close()


def test_uncertain_telegram_delivery_is_not_retried(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    store = Store(database)
    store.initialize("resupply", "resupply", 0)
    store.discover(proposal_23())
    key = proposal_23().key
    store.save_analysis(key, high_analysis())
    store.published(key, GIST_URL)
    store.sending(key)
    store.close()

    assert Store.check(database)
    connection = sqlite3.connect(database)
    status = connection.execute(
        "SELECT status FROM proposals WHERE protocol='resupply' "
        "AND source='resupply' AND upstream_id=23"
    ).fetchone()[0]
    connection.close()
    assert status == "sending"

    reopened = Store(database)
    assert reopened.recover_interrupted() == 1
    status = reopened.connection.execute(
        "SELECT status FROM proposals WHERE protocol='resupply' "
        "AND source='resupply' AND upstream_id=23"
    ).fetchone()[0]
    assert status == "review"
    assert reopened.pending() == []
    reopened.close()


def test_database_check_does_not_create_a_missing_database(tmp_path: Path) -> None:
    database = tmp_path / "missing.db"

    assert not Store.check(database)
    assert not database.exists()


def test_gist_publication_verifies_exact_revision() -> None:
    markdown = "# Report\n\nExact bytes.\n"
    digest = hashlib.sha256(markdown.encode()).hexdigest()
    snapshot = hashlib.sha256(
        json.dumps(
            {
                "version": 1,
                "title": "Report",
                "files": [{"filename": "README.md", "content_sha256": digest}],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    calls: list[str] = []
    response = {
        "id": "AbCdEfGhIjKlMnOp",
        "url": GIST_URL,
        "title": "Report",
        "primary_file": "README.md",
        "revision_number": 1,
        "latest_revision_number": 1,
        "snapshot_sha256": snapshot,
        "files": {
            "README.md": {
                "filename": "README.md",
                "content": markdown,
                "content_sha256": digest,
                "byte_size": len(markdown.encode()),
            }
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url}")
        if request.method == "POST":
            assert json.loads(request.content) == {
                "title": "Report",
                "files": {"README.md": {"content": markdown}},
            }
            return httpx.Response(201, json=response)
        if request.url.path.endswith("/raw"):
            return httpx.Response(200, content=markdown.encode())
        if request.url.host == "api.wavey.info":
            return httpx.Response(200, json=response)
        return httpx.Response(200, text="<html>report</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    result = Gist("secret", client=client).publish("Report", markdown)

    assert result.url == GIST_URL
    assert result.sha256 == digest
    assert len(calls) == 4


def test_gist_lost_response_is_ambiguous_and_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("lost", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GistUnknown):
        Gist("secret", client=client).publish("Report", "# Report\n")
    assert calls == 1


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_telegram_destination_matching(chat_type: str) -> None:
    assert matches_chat(-123, chat_type, "-123")
    assert not matches_chat(-456, chat_type, "-123")
    assert not matches_chat(-123, "private", "-123")
