"""Read complete Curve Aragon proposals from finalized Ethereum state."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

import httpx
from eth_abi import decode, encode
from eth_typing import ChecksumAddress
from web3 import Web3

from .model import Action, Fact, Proposal, proposal_title
from .presentation import Presentation, presentation_for, validate_presentation

CONFIRMATIONS = 12
VOTING_PERIOD_SECONDS = 7 * 24 * 60 * 60
CALLSCRIPT_ID = b"\x00\x00\x00\x01"
MAX_SCRIPT_ACTIONS = 256
ZERO_ADDRESS = Web3.to_checksum_address("0x0000000000000000000000000000000000000000")
START_VOTE_TOPIC = (
    "0x"
    + Web3.keccak(text="StartVote(uint256,address,string,uint256,uint256,uint256,uint256)").hex()
)
AGENT_EXECUTE_SELECTOR = Web3.keccak(text="execute(address,uint256,bytes)")[:4]
IPFS_ID = re.compile(r"^(?:Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{20,120})$")
MAX_METADATA_BYTES = 1_048_576

VOTING_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "votesLength",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getVote",
        "stateMutability": "view",
        "inputs": [{"name": "voteId", "type": "uint256"}],
        "outputs": [
            {"name": "open", "type": "bool"},
            {"name": "executed", "type": "bool"},
            {"name": "startDate", "type": "uint64"},
            {"name": "snapshotBlock", "type": "uint64"},
            {"name": "supportRequired", "type": "uint64"},
            {"name": "minAcceptQuorum", "type": "uint64"},
            {"name": "yea", "type": "uint256"},
            {"name": "nay", "type": "uint256"},
            {"name": "votingPower", "type": "uint256"},
            {"name": "script", "type": "bytes"},
        ],
    },
]


@dataclass(frozen=True)
class CurveSourceConfig:
    voting: ChecksumAddress
    agent: ChecksumAddress
    deployment_block: int


SOURCES: dict[str, CurveSourceConfig] = {
    "ownership": CurveSourceConfig(
        voting=Web3.to_checksum_address("0xE478de485ad2fe566d49342Cbd03E49ed7DB3356"),
        agent=Web3.to_checksum_address("0x40907540d8a6C65c637785e8f8B742ae6b0b9968"),
        deployment_block=10_648_599,
    ),
    "parameter": CurveSourceConfig(
        voting=Web3.to_checksum_address("0xBCfF8B0b9419b9A88c44546519b1e909cF330399"),
        agent=Web3.to_checksum_address("0x4EEb3bA4f221cA16ed4A0cC7254E2E32DF948c5f"),
        deployment_block=10_649_517,
    ),
}


def _short_address(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


def _utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M UTC")


def _percentage(value: int) -> str:
    return f"{value / 10**16:.2f}%"


class Curve:
    def __init__(
        self,
        rpc_url: str,
        source: Literal["ownership", "parameter"],
        *,
        metadata_client: httpx.Client | None = None,
    ) -> None:
        self.protocol: Presentation = presentation_for("curve")
        self.source = source
        self.config = SOURCES[source]
        self.web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 60}))
        self.contract = self.web3.eth.contract(
            address=self.config.voting, abi=cast(Any, VOTING_ABI)
        )
        self._owns_client = metadata_client is None
        self.metadata_client = metadata_client or httpx.Client(timeout=15, follow_redirects=False)

    def close(self) -> None:
        if self._owns_client:
            self.metadata_client.close()

    def finalized_block(self) -> int:
        if int(self.web3.eth.chain_id) != 1:
            raise RuntimeError("Curve proposal source is not Ethereum mainnet")
        return max(0, self.web3.eth.block_number - CONFIRMATIONS)

    def count(self, block: int | None = None) -> int:
        at = self.finalized_block() if block is None else block
        return int(self.contract.functions.votesLength().call(block_identifier=at))

    def _creation(
        self, vote_id: int, snapshot_block: int, finalized_block: int
    ) -> tuple[str, str, int, str]:
        vote_topic = "0x" + vote_id.to_bytes(32, "big").hex()
        logs = self.web3.eth.get_logs(
            {
                "address": self.config.voting,
                "fromBlock": max(self.config.deployment_block, snapshot_block - 2),
                "toBlock": min(finalized_block, snapshot_block + 2),
                "topics": [START_VOTE_TOPIC, vote_topic],
            }
        )
        if len(logs) != 1 or len(logs[0]["topics"]) < 3:
            raise RuntimeError("Curve StartVote event was missing or nonunique")
        log = logs[0]
        proposer = Web3.to_checksum_address("0x" + Web3.to_hex(log["topics"][2])[-40:])
        transaction = Web3.to_hex(log["transactionHash"])
        data = bytes(Web3.to_bytes(hexstr=Web3.to_hex(log["data"])))
        event_types = ["string", "uint256", "uint256", "uint256", "uint256"]
        event_values = decode(event_types, data)
        if encode(event_types, event_values) != data:
            raise RuntimeError("Curve StartVote event data was not canonical")
        metadata_uri = event_values[0]
        return proposer, transaction, int(log["blockNumber"]), str(metadata_uri)

    def _metadata(self, uri: str) -> tuple[str, str | None]:
        prefix = "ipfs://" if uri.startswith("ipfs://") else "ipfs:"
        if not uri.startswith(prefix):
            return "", "proposal metadata URI is not a supported IPFS identifier"
        identifier = uri[len(prefix) :]
        if not IPFS_ID.fullmatch(identifier):
            return "", "proposal metadata URI is not a valid bounded IPFS identifier"
        try:
            with self.metadata_client.stream(
                "GET", f"https://ipfs.io/ipfs/{identifier}"
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_METADATA_BYTES:
                        return "", "proposal metadata exceeds the size limit"
                    chunks.append(chunk)
            body = json.loads(b"".join(chunks))
        except (httpx.HTTPError, ValueError):
            return "", "proposal metadata could not be retrieved and decoded"
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            return "", "proposal metadata does not contain a text description"
        description = text.strip()
        if not description:
            return "", "proposal metadata contains an empty text description"
        return description, None

    def _unresolved_action(self, index: int, raw: bytes, reason: str) -> Action:
        target = ZERO_ADDRESS
        calldata = b""
        if len(raw) >= 20:
            target = Web3.to_checksum_address("0x" + raw[:20].hex())
        if len(raw) > 24:
            calldata = raw[24:]
        return Action(
            index=index,
            executor=self.config.voting,
            target=target,
            value_wei=0,
            calldata="0x" + calldata.hex(),
            raw="0x" + raw.hex(),
            unresolved=reason,
        )

    def _decode_script(self, script: bytes) -> list[Action]:
        if len(script) < 4 or script[:4] != CALLSCRIPT_ID:
            return [
                self._unresolved_action(
                    0, script, "Curve execution script is not a supported CallScript"
                )
            ]
        actions: list[Action] = []
        offset = 4
        while offset < len(script):
            if len(actions) >= MAX_SCRIPT_ACTIONS:
                actions.append(
                    self._unresolved_action(
                        len(actions),
                        script[offset:],
                        "Curve CallScript exceeds the action limit",
                    )
                )
                break
            remaining = script[offset:]
            if len(remaining) < 24:
                actions.append(
                    self._unresolved_action(
                        len(actions), remaining, "Curve CallScript segment header is truncated"
                    )
                )
                break
            top_target = Web3.to_checksum_address("0x" + remaining[:20].hex())
            length = int.from_bytes(remaining[20:24], "big")
            end = offset + 24 + length
            if end > len(script):
                actions.append(
                    self._unresolved_action(
                        len(actions),
                        script[offset:],
                        "Curve CallScript segment exceeds the remaining bytes",
                    )
                )
                break
            raw = script[offset:end]
            wrapper = script[offset + 24 : end]
            executor = self.config.voting
            target = top_target
            value_wei = 0
            calldata = wrapper
            unresolved: str | None = (
                "CallScript segment is not the configured Curve Agent execute wrapper"
            )
            if top_target == self.config.agent and wrapper[:4] == AGENT_EXECUTE_SELECTOR:
                try:
                    inner_target, inner_value, inner_data = decode(
                        ["address", "uint256", "bytes"], wrapper[4:]
                    )
                    canonical = AGENT_EXECUTE_SELECTOR + encode(
                        ["address", "uint256", "bytes"],
                        [inner_target, inner_value, inner_data],
                    )
                    if canonical != wrapper:
                        raise ValueError("noncanonical Agent wrapper")
                    executor = self.config.agent
                    target = Web3.to_checksum_address(inner_target)
                    value_wei = int(inner_value)
                    calldata = bytes(inner_data)
                    unresolved = None
                except (OverflowError, TypeError, ValueError):
                    unresolved = "Curve Agent execute wrapper is not canonical"
            actions.append(
                Action(
                    index=len(actions),
                    executor=executor,
                    target=target,
                    value_wei=value_wei,
                    calldata="0x" + calldata.hex(),
                    raw="0x" + raw.hex(),
                    unresolved=unresolved,
                )
            )
            offset = end
        return actions

    def proposal(self, proposal_id: int, block: int | None = None) -> Proposal:
        at = self.finalized_block() if block is None else block
        values = self.contract.functions.getVote(proposal_id).call(block_identifier=at)
        (
            _open,
            _executed,
            start_date,
            snapshot_block,
            _support_required,
            min_accept_quorum,
            _yea,
            _nay,
            _voting_power,
            script,
        ) = values
        proposer, transaction, creation_block, metadata_uri = self._creation(
            proposal_id, int(snapshot_block), at
        )
        description, metadata_unknown = self._metadata(metadata_uri)
        actions = self._decode_script(bytes(script))
        unknowns = [action.unresolved for action in actions if action.unresolved]
        if metadata_unknown:
            unknowns.append(metadata_unknown)
        title = proposal_title(description, f"Curve vote {proposal_id}")
        proposal = Proposal(
            protocol=self.protocol.slug,
            source=self.source,
            id=proposal_id,
            title=title,
            description=description,
            created_at=int(start_date),
            creation_block=creation_block,
            voter=self.config.voting,
            executor=self.config.agent,
            block=at,
            raw_payload="0x" + bytes(script).hex(),
            facts={
                "proposer": Fact(
                    value=_short_address(proposer),
                    url=f"https://etherscan.io/address/{proposer}",
                ),
                "vote_type": Fact(value=self.source.title()),
                "quorum": Fact(value=_percentage(int(min_accept_quorum))),
                "ends_at": Fact(value=_utc(int(start_date) + VOTING_PERIOD_SECONDS)),
            },
            links={
                "etherscan": f"https://etherscan.io/tx/{transaction}",
                "curve": f"https://www.curve.finance/dao/vote/{self.source}/{proposal_id}",
            },
            actions=actions,
            unknowns=tuple(dict.fromkeys(unknowns)),
        )
        validate_presentation(self.protocol, proposal)
        return proposal
