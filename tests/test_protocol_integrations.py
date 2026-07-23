from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from eth_abi import encode
from web3 import Web3

from govlens.audit import (
    AUDIT_PROMPT,
    AUDIT_TIMEOUT_SECONDS,
    MAX_RESULT_BYTES,
    _validate,
    investigate,
)
from govlens.checks import CheckResult, run_checks
from govlens.checks.curve import (
    ADD_GAUGE_3,
    GAUGE_CONTROLLER,
    GAUGE_VALIDATOR,
    VALIDATE_GAUGE,
)
from govlens.checks.curve import (
    run as run_curve_checks,
)
from govlens.config import Settings
from govlens.curve import CALLSCRIPT_ID, Curve
from govlens.model import Action, Fact, Proposal
from govlens.resupply import Resupply
from govlens.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
GAUGE = Web3.to_checksum_address("0xA49EB49F7e4C86D93f6EA8b81e4863aC4c3B4891")
BLOCK = 25_582_242


def _proposal(
    actions: list[Action],
    *,
    protocol: str = "curve",
    source: str = "ownership",
    unknowns: tuple[str, ...] = (),
) -> Proposal:
    return Proposal(
        protocol=protocol,
        source=source,
        id=1458,
        title="Fixture proposal",
        description="Fixture proposal",
        created_at=1_784_000_000,
        creation_block=BLOCK,
        voter="0x1111111111111111111111111111111111111111",
        executor="0x2222222222222222222222222222222222222222",
        block=BLOCK + 20,
        raw_payload=None,
        facts={"fixture": Fact("yes")},
        links={},
        actions=actions,
        unknowns=unknowns,
    )


def _analysis(action_count: int, severity: str = "MEDIUM") -> dict[str, Any]:
    del action_count
    return {
        "severity": severity,
        "summary": "The fixture has bounded effects.",
        "findings": [],
        "unknowns": [],
    }


class FakeEth:
    chain_id = 1

    def __init__(self) -> None:
        self.calls: dict[tuple[str, bytes], bytes] = {}
        self.codes: dict[str, bytes] = {}
        self.blocks: list[int] = []

    def call(self, transaction: dict[str, Any], *, block_identifier: int) -> bytes:
        self.blocks.append(block_identifier)
        key = (str(transaction["to"]).casefold(), bytes(transaction["data"]))
        return self.calls[key]

    def get_code(self, address: str, *, block_identifier: int) -> bytes:
        self.blocks.append(block_identifier)
        return self.codes.get(address.casefold(), b"")


class FakeWeb3:
    def __init__(self) -> None:
        self.eth = FakeEth()


def test_curve_callscript_fixture_preserves_every_byte_and_effective_action() -> None:
    fixture = json.loads((FIXTURES / "curve_aragon_vote_223.json").read_text())
    script = bytes.fromhex(fixture["vote"]["script"][2:])
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    source = Curve("http://127.0.0.1:1", "ownership", metadata_client=client)

    actions = source._decode_script(script)

    assert len(actions) == 3
    assert all(action.unresolved is None for action in actions)
    assert CALLSCRIPT_ID + b"".join(bytes.fromhex(action.raw[2:]) for action in actions) == script  # type: ignore[index]
    assert [action.calldata[:10] for action in actions] == ["0x4344ce71"] * 3
    client.close()


def test_curve_parser_retains_malformed_tail_as_unknown() -> None:
    source = Curve("http://127.0.0.1:1", "ownership")
    malformed = CALLSCRIPT_ID + bytes.fromhex("11" * 17)

    actions = source._decode_script(malformed)

    assert len(actions) == 1
    assert actions[0].raw == "0x" + malformed[4:].hex()
    assert actions[0].unresolved == "Curve CallScript segment header is truncated"
    source.close()


def test_curve_metadata_accepts_only_bounded_ipfs_identifiers() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json={"text": "Gauge proposal"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = Curve("http://127.0.0.1:1", "ownership", metadata_client=client)

    text, unknown = source._metadata("ipfs:QmWHbBknnCMPTiWsHQio5cNKtm8o89WPrXPJTjuQGdfA2z")
    _, rejected = source._metadata("https://evil.example/metadata")

    assert text == "Gauge proposal"
    assert unknown is None
    assert rejected is not None
    assert requests == ["https://ipfs.io/ipfs/QmWHbBknnCMPTiWsHQio5cNKtm8o89WPrXPJTjuQGdfA2z"]
    client.close()


def test_curve_gauge_check_uses_validator_at_one_block() -> None:
    calldata = ADD_GAUGE_3 + encode(["address", "int128", "uint256"], [GAUGE, 0, 1])
    action = Action(
        0, "0x2222222222222222222222222222222222222222", GAUGE_CONTROLLER, 0, "0x" + calldata.hex()
    )
    proposal = _proposal([action])
    web3 = FakeWeb3()
    web3.eth.codes[GAUGE_VALIDATOR.casefold()] = b"\x60\x00"
    validate = VALIDATE_GAUGE + encode(["address"], [GAUGE])
    web3.eth.calls[(GAUGE_VALIDATOR.casefold(), validate)] = encode(["bool"], [True])

    result = run_curve_checks(proposal, web3)[0]  # type: ignore[arg-type]

    assert result.status == "PASS"
    assert len(result.evidence) == 2
    assert all(item.target == GAUGE_VALIDATOR for item in result.evidence)
    assert set(web3.eth.blocks) == {BLOCK}
    assert all(item.block == BLOCK and item.chain_id == 1 for item in result.evidence)


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (encode(["bool"], [False]), "FAIL"),
        (b"\x01", "UNKNOWN"),
    ],
)
def test_curve_gauge_check_is_conservative(response: bytes, status: str) -> None:
    calldata = ADD_GAUGE_3 + encode(["address", "int128", "uint256"], [GAUGE, 0, 1])
    proposal = _proposal(
        [Action(0, proposal_executor(), GAUGE_CONTROLLER, 0, "0x" + calldata.hex())]
    )
    web3 = FakeWeb3()
    web3.eth.codes[GAUGE_VALIDATOR.casefold()] = b"\x60"
    validate = VALIDATE_GAUGE + encode(["address"], [GAUGE])
    web3.eth.calls[(GAUGE_VALIDATOR.casefold(), validate)] = response

    assert run_curve_checks(proposal, web3)[0].status == status  # type: ignore[arg-type]


def test_curve_1452_routes_both_gauges_through_validator() -> None:
    gauges = [
        Web3.to_checksum_address("0x3A55AAb28B4516ceB565a6e0577285C84F53520a"),
        Web3.to_checksum_address("0x6b3A14e237a70c7703A4ac4590c2c254065FB8dd"),
    ]
    actions = [
        Action(
            index,
            proposal_executor(),
            GAUGE_CONTROLLER,
            0,
            "0x" + (ADD_GAUGE_3 + encode(["address", "int128", "uint256"], [gauge, 0, 0])).hex(),
        )
        for index, gauge in enumerate(gauges)
    ]
    proposal = replace(_proposal(actions), id=1452, creation_block=25_530_314)
    web3 = FakeWeb3()
    web3.eth.codes[GAUGE_VALIDATOR.casefold()] = b"\x60"
    for gauge in gauges:
        validate = VALIDATE_GAUGE + encode(["address"], [gauge])
        web3.eth.calls[(GAUGE_VALIDATOR.casefold(), validate)] = encode(["bool"], [False])

    results = run_curve_checks(proposal, web3)  # type: ignore[arg-type]

    assert [result.status for result in results] == ["FAIL", "FAIL"]
    assert all(result.id == "curve.gauge_validator" for result in results)


def test_resupply_leaves_sequence_sensitive_checks_to_the_audit() -> None:
    proposal = _proposal([], protocol="resupply", source="resupply")
    web3 = FakeWeb3()
    web3.eth.chain_id = 10

    assert run_checks(proposal, web3) == []  # type: ignore[arg-type]
    assert web3.eth.blocks == []


def proposal_executor() -> str:
    return "0x2222222222222222222222222222222222222222"


def test_sources_and_checks_reject_non_mainnet_rpc() -> None:
    curve = Curve("http://127.0.0.1:1", "ownership")
    curve.web3 = SimpleNamespace(eth=SimpleNamespace(chain_id=10, block_number=100))  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="Ethereum mainnet"):
        curve.finalized_block()
    curve.close()

    resupply = Resupply("http://127.0.0.1:1")
    resupply.web3 = SimpleNamespace(eth=SimpleNamespace(chain_id=10, block_number=100))  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="Ethereum mainnet"):
        resupply.finalized_block()

    web3 = FakeWeb3()
    web3.eth.chain_id = 10
    with pytest.raises(RuntimeError, match="require Ethereum mainnet"):
        run_checks(_proposal([]), web3)  # type: ignore[arg-type]


def test_nonpassing_parent_check_and_normalization_unknown_promote_low() -> None:
    proposal = _proposal([], unknowns=("wrapper unresolved",))
    assert _validate(_analysis(0, "LOW"), proposal)["severity"] == "MEDIUM"

    clean = replace(proposal, unknowns=())
    unknown_check = CheckResult(
        id="fixture",
        action_index=0,
        status="UNKNOWN",
        summary="Unknown.",
        evidence=(),
    )
    assert _validate(_analysis(0, "LOW"), clean, [unknown_check])["severity"] == "MEDIUM"


def test_store_keeps_equal_ids_from_curve_sources_distinct(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.initialize("curve", "ownership", 1)
    store.initialize("curve", "parameter", 1)
    base = _proposal([])
    store.discover(replace(base, id=1, source="ownership"))
    store.discover(replace(base, id=1, source="parameter"))

    rows = store.connection.execute(
        "SELECT source, upstream_id FROM proposals ORDER BY source"
    ).fetchall()

    assert [(row["source"], row["upstream_id"]) for row in rows] == [
        ("ownership", 1),
        ("parameter", 1),
    ]
    assert store.cursor("curve", "ownership") == 2
    assert store.cursor("curve", "parameter") == 2
    store.close()


def test_store_rejects_noncurrent_schema_without_mutating_it(tmp_path: Path) -> None:
    path = tmp_path / "other.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE state(id INTEGER PRIMARY KEY, next_proposal_id INTEGER)")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="empty or use the current schema"):
        Store(path)

    connection = sqlite3.connect(path)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    connection.close()
    assert tables == {"state"}


def test_audit_injects_small_protocol_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / "codex"
    codex.write_text("fixture")
    settings = Settings(
        rpc_url="http://rpc.invalid",
        archive_rpc_url="http://archive.invalid",
        etherscan_key="",
        gist_key="",
        telegram_token="",
        telegram_targets={},
        database=tmp_path / "state.db",
        codex=codex,
    )
    proposal = _proposal([])
    monkeypatch.setattr("govlens.audit.run_checks", lambda _proposal, _web3: [])

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        case = Path(command[command.index("--cd") + 1])
        output = Path(command[command.index("--output-last-message") + 1])
        assert kwargs["input"] == AUDIT_PROMPT
        assert "best-effort creation-block fork" in kwargs["input"]
        assert "execution of every ordered payload" in kwargs["input"]
        assert "routine ballot mechanics is unnecessary" in kwargs["input"]
        assert "keep that as an unknown" in kwargs["input"]
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert json.loads((case / "proposal.json").read_text())["id"] == 1458
        assert json.loads((case / "checks.json").read_text()) == []
        assert "Trusted Curve context" in (case / "PROTOCOL.md").read_text()
        assert "gov.curve.finance" in (case / "PROTOCOL.md").read_text()
        assert (case / "lib" / "analysis.py").is_file()
        assert not (case / "lib" / "workflow.py").exists()
        assert not (case / "lib" / "chifra.py").exists()
        assert 'model_reasoning_effort="high"' in command
        assert 'web_search="cached"' in command
        assert all("xhigh" not in item for item in command)
        output.write_text(json.dumps(_analysis(0, "LOW")))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("govlens.audit.subprocess.run", fake_run)

    result = investigate(settings, proposal)

    assert result["severity"] == "LOW"
    assert result["checks"] == []


@pytest.mark.parametrize(
    ("context", "forum"),
    (("curve.md", "gov.curve.finance"), ("resupply.md", "gov.resupply.finance")),
)
def test_protocol_context_names_official_forum(context: str, forum: str) -> None:
    path = Path(__file__).parents[1] / "src/govlens/investigator/contexts" / context

    assert forum in path.read_text()


def test_audit_uses_fallback_for_oversized_model_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / "codex"
    codex.write_text("fixture")
    settings = Settings(
        rpc_url="http://rpc.invalid",
        archive_rpc_url="http://archive.invalid",
        etherscan_key="",
        gist_key="",
        telegram_token="",
        telegram_targets={},
        database=tmp_path / "state.db",
        codex=codex,
    )
    monkeypatch.setattr("govlens.audit.run_checks", lambda _proposal, _web3: [])

    def fake_run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_bytes(b"x" * (MAX_RESULT_BYTES + 1))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("govlens.audit.subprocess.run", fake_run)

    result = investigate(settings, _proposal([]))

    assert result["severity"] == "MEDIUM"
    assert result["checks"] == []
    assert result["unknowns"] == [
        "The Codex investigation did not return a usable result; manual review is required."
    ]


def test_audit_timeout_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex = tmp_path / "codex"
    codex.write_text("fixture")
    settings = Settings(
        rpc_url="http://rpc.invalid",
        archive_rpc_url="http://archive.invalid",
        etherscan_key="",
        gist_key="",
        telegram_token="",
        telegram_targets={},
        database=tmp_path / "state.db",
        codex=codex,
    )
    monkeypatch.setattr("govlens.audit.run_checks", lambda _proposal, _web3: [])

    def timeout(command: list[str], **kwargs: Any) -> SimpleNamespace:
        assert AUDIT_TIMEOUT_SECONDS == 30 * 60
        assert f"{AUDIT_TIMEOUT_SECONDS // 60}-minute execution budget" in kwargs["input"]
        assert kwargs["timeout"] == AUDIT_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("govlens.audit.subprocess.run", timeout)

    result = investigate(settings, _proposal([]))

    assert result["severity"] == "MEDIUM"
    assert result["checks"] == []
    assert "manual review" in result["unknowns"][0]


def test_audit_uses_fallback_for_invalid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    codex = tmp_path / "codex"
    codex.write_text("fixture")
    settings = Settings(
        rpc_url="http://rpc.invalid",
        archive_rpc_url="http://archive.invalid",
        etherscan_key="",
        gist_key="",
        telegram_token="",
        telegram_targets={},
        database=tmp_path / "state.db",
        codex=codex,
    )
    monkeypatch.setattr("govlens.audit.run_checks", lambda _proposal, _web3: [])

    def fake_run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text("untrusted model content")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("govlens.audit.subprocess.run", fake_run)

    with caplog.at_level("WARNING", logger="govlens.audit"):
        result = investigate(settings, _proposal([]))

    assert result["severity"] == "MEDIUM"
    assert result["checks"] == []
    assert "manual review" in result["summary"]
    assert "reason=invalid_json" in caplog.text
    assert "untrusted model content" not in caplog.text


def test_audit_uses_fallback_when_checks_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        rpc_url="http://rpc.invalid",
        archive_rpc_url="http://archive.invalid",
        etherscan_key="",
        gist_key="",
        telegram_token="",
        telegram_targets={},
        database=tmp_path / "state.db",
        codex=tmp_path / "codex",
    )

    def fail_checks(_proposal: Proposal, _web3: Web3) -> list[CheckResult]:
        raise RuntimeError("untrusted RPC failure")

    monkeypatch.setattr("govlens.audit.run_checks", fail_checks)
    monkeypatch.setattr(
        "govlens.audit.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("Codex should not run without parent checks"),
    )

    result = investigate(settings, _proposal([]))

    assert result["severity"] == "MEDIUM"
    assert result["checks"] == []
    assert result["findings"] == []
    assert "Deterministic protocol checks could not be completed" in result["unknowns"][0]
