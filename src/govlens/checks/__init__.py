"""Narrow, block-pinned checks backed by canonical protocol validators."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from web3 import Web3

from ..model import Proposal

CheckStatus = Literal["PASS", "FAIL", "UNKNOWN"]
MAINNET_CHAIN_ID = 1


@dataclass(frozen=True)
class RpcEvidence:
    chain_id: int
    block: int
    target: str
    request: str
    raw_result: str


@dataclass(frozen=True)
class CheckResult:
    id: str
    action_index: int
    status: CheckStatus
    summary: str
    evidence: tuple[RpcEvidence, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _chain_id(web3: Web3) -> int:
    chain_id = int(web3.eth.chain_id)
    if chain_id != MAINNET_CHAIN_ID:
        raise RuntimeError("canonical checks require Ethereum mainnet")
    return chain_id


def rpc_call(
    web3: Web3,
    target: str,
    calldata: bytes,
    block: int,
) -> tuple[bytes, RpcEvidence]:
    address = Web3.to_checksum_address(target)
    raw = bytes(
        web3.eth.call(
            {"to": address, "data": calldata},
            block_identifier=block,
        )
    )
    return raw, RpcEvidence(
        chain_id=_chain_id(web3),
        block=block,
        target=address,
        request="0x" + calldata.hex(),
        raw_result="0x" + raw.hex(),
    )


def rpc_code(web3: Web3, target: str, block: int) -> tuple[bytes, RpcEvidence]:
    address = Web3.to_checksum_address(target)
    raw = bytes(web3.eth.get_code(address, block_identifier=block))
    return raw, RpcEvidence(
        chain_id=_chain_id(web3),
        block=block,
        target=address,
        request="eth_getCode",
        raw_result=f"bytes={len(raw)};keccak256=0x{Web3.keccak(raw).hex()}",
    )


def run_checks(proposal: Proposal, web3: Web3) -> list[CheckResult]:
    if proposal.protocol == "curve":
        _chain_id(web3)
        from .curve import run

        return run(proposal, web3)
    return []
