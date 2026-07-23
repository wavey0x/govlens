"""Read complete Resupply proposals from finalized Ethereum state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
from web3 import Web3

from .model import Action, Fact, Proposal, proposal_title
from .presentation import Presentation, presentation_for, validate_presentation

VOTER = Web3.to_checksum_address("0x11111111063874cE8dC6232cb5C1C849359476E6")
DEPLOYMENT_BLOCK = 22_800_403
CONFIRMATIONS = 12
VOTING_PERIOD_SECONDS = 7 * 24 * 60 * 60
PROPOSAL_CREATED_TOPIC = (
    "0x"
    + Web3.keccak(text="ProposalCreated(address,uint256,(address,bytes)[],uint256,uint256)").hex()
)
RESUPPLY_URL = "https://resupply.finance/governance/proposals?id={proposal_id}"
HIPPO_API = "https://api.hippo.army/v1/dao/proposals"
MAX_HIPPO_BYTES = 1_048_576

ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "getProposalCount",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "core",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "getProposalData",
        "stateMutability": "view",
        "inputs": [{"name": "proposalId", "type": "uint256"}],
        "outputs": [
            {"name": "description", "type": "string"},
            {"name": "epoch", "type": "uint256"},
            {"name": "createdAt", "type": "uint256"},
            {"name": "quorumWeight", "type": "uint256"},
            {"name": "weightYes", "type": "uint256"},
            {"name": "weightNo", "type": "uint256"},
            {"name": "processed", "type": "bool"},
            {"name": "executable", "type": "bool"},
            {
                "name": "payload",
                "type": "tuple[]",
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "data", "type": "bytes"},
                ],
            },
        ],
    },
]


def _short_address(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


def _utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M UTC")


class Resupply:
    source = "resupply"

    def __init__(self, rpc_url: str) -> None:
        self.protocol: Presentation = presentation_for("resupply")
        self.web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 60}))
        self.contract = self.web3.eth.contract(address=VOTER, abi=ABI)

    def finalized_block(self) -> int:
        if int(self.web3.eth.chain_id) != 1:
            raise RuntimeError("Resupply proposal source is not Ethereum mainnet")
        return max(0, self.web3.eth.block_number - CONFIRMATIONS)

    def count(self, block: int | None = None) -> int:
        at = self.finalized_block() if block is None else block
        return int(self.contract.functions.getProposalCount().call(block_identifier=at))

    def _creation(self, proposal_id: int, created_at: int, block: int) -> tuple[str, str, int]:
        low = DEPLOYMENT_BLOCK
        high = block
        candidate = low
        while low <= high:
            middle = (low + high) // 2
            timestamp = int(self.web3.eth.get_block(middle)["timestamp"])
            if timestamp <= created_at:
                candidate = middle
                low = middle + 1
            else:
                high = middle - 1
        proposal_topic = "0x" + proposal_id.to_bytes(32, "big").hex()
        logs = self.web3.eth.get_logs(
            {
                "address": VOTER,
                "fromBlock": max(DEPLOYMENT_BLOCK, candidate - 5),
                "toBlock": min(block, candidate + 5),
                "topics": [PROPOSAL_CREATED_TOPIC, None, proposal_topic],
            }
        )
        if len(logs) != 1 or len(logs[0]["topics"]) < 3:
            raise RuntimeError("Resupply ProposalCreated event was missing or nonunique")
        proposer = Web3.to_checksum_address("0x" + Web3.to_hex(logs[0]["topics"][1])[-40:])
        transaction = Web3.to_hex(logs[0]["transactionHash"])
        return proposer, transaction, int(logs[0]["blockNumber"])

    @staticmethod
    def _hippo_url(proposal_id: int, transaction: str) -> str | None:
        try:
            with httpx.stream(
                "GET",
                HIPPO_API,
                params={"page": 1, "per_page": 100, "order_by": "created_at"},
                timeout=10,
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_HIPPO_BYTES:
                        return None
                    chunks.append(chunk)
            body = json.loads(b"".join(chunks))
            proposals = body.get("proposals") if isinstance(body, dict) else None
            if not isinstance(proposals, list):
                return None
            matches = [
                item
                for item in proposals
                if isinstance(item, dict)
                and item.get("on_chain_id") == proposal_id
                and str(item.get("creation_tx_hash", "")).casefold() == transaction.casefold()
                and isinstance(item.get("proposal_id"), int)
            ]
            if len(matches) != 1:
                return None
            external_id = int(matches[0]["proposal_id"])
            return f"https://hippo.army/dao/proposal/{external_id}"
        except (httpx.HTTPError, ValueError):
            return None

    def proposal(self, proposal_id: int, block: int | None = None) -> Proposal:
        at = self.finalized_block() if block is None else block
        values = self.contract.functions.getProposalData(proposal_id).call(block_identifier=at)
        (
            description,
            epoch,
            created_at,
            quorum,
            _yes,
            _no,
            _processed,
            _executable,
            payload,
        ) = values
        executor = Web3.to_checksum_address(
            self.contract.functions.core().call(block_identifier=at)
        )
        proposer, transaction, creation_block = self._creation(proposal_id, int(created_at), at)
        actions = [
            Action(
                index=index,
                executor=executor,
                target=Web3.to_checksum_address(target),
                value_wei=0,
                calldata=Web3.to_hex(data),
            )
            for index, (target, data) in enumerate(payload)
        ]
        links = {
            "etherscan": f"https://etherscan.io/tx/{transaction}",
            "resupply": RESUPPLY_URL.format(proposal_id=proposal_id),
        }
        hippo_url = self._hippo_url(proposal_id, transaction)
        if hippo_url:
            links["hippo"] = hippo_url
        description_text = str(description).strip()
        proposal = Proposal(
            protocol=self.protocol.slug,
            source=self.source,
            id=proposal_id,
            title=proposal_title(description_text, f"Proposal {proposal_id}"),
            description=description_text,
            created_at=int(created_at),
            creation_block=creation_block,
            voter=VOTER,
            executor=executor,
            block=at,
            raw_payload=None,
            facts={
                "proposer": Fact(
                    value=_short_address(proposer),
                    url=f"https://etherscan.io/address/{proposer}",
                ),
                "epoch": Fact(value=str(int(epoch))),
                "quorum": Fact(value=f"{int(quorum):,}"),
                "ends_at": Fact(value=_utc(int(created_at) + VOTING_PERIOD_SECONDS)),
            },
            links=links,
            actions=actions,
            unknowns=(),
        )
        validate_presentation(self.protocol, proposal)
        return proposal
