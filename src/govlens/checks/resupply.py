"""Resupply-specific deterministic PairDeployer provenance check."""

from __future__ import annotations

from eth_abi import decode, encode
from web3 import Web3

from ..model import Action, Proposal
from . import CheckResult, RpcEvidence, rpc_call, rpc_code

REGISTRY = Web3.to_checksum_address("0x10101010E0C3171D894B71B3400668aF311e7D94")
PAIR_ADDERS = {
    Web3.to_checksum_address("0x6Ba4D235B71Cb868bC4576E15dD75701DE6D6929"),
}
ADD_PAIR = bytes(Web3.keccak(text="addPair(address)")[:4])
SET_ADDRESS = bytes(Web3.keccak(text="setAddress(string,address)")[:4])
GET_ADDRESS = bytes(Web3.keccak(text="getAddress(string)")[:4])
DEPLOY_INFO = bytes(Web3.keccak(text="deployInfo(address)")[:4])
CORE = bytes(Web3.keccak(text="core()")[:4])
PAIR_ADDER_REGISTRY = bytes(Web3.keccak(text="registry()")[:4])
PAIR_DEPLOYER_KEY = "PAIR_DEPLOYER"


def _address(raw: bytes) -> str:
    if len(raw) != 32 or raw[:12] != b"\x00" * 12:
        raise ValueError("address response was not canonical")
    return Web3.to_checksum_address("0x" + raw[-20:].hex())


def _result(
    action: Action,
    status: str,
    summary: str,
    evidence: list[RpcEvidence],
) -> CheckResult:
    if status not in {"PASS", "FAIL", "UNKNOWN"}:
        raise ValueError("invalid check status")
    return CheckResult(
        id="resupply.pair_deployer_provenance",
        action_index=action.index,
        status=status,  # type: ignore[arg-type]
        summary=summary,
        evidence=tuple(evidence),
    )


def _changes_pair_deployer(proposal: Proposal, before: int) -> bool:
    for action in proposal.actions[:before]:
        if action.target != REGISTRY:
            continue
        try:
            data = bytes.fromhex(action.calldata[2:])
            if data[:4] != SET_ADDRESS:
                continue
            try:
                values = decode(["string", "address"], data[4:])
                if data != SET_ADDRESS + encode(["string", "address"], values):
                    return True
                if values[0] == PAIR_DEPLOYER_KEY:
                    return True
            except (OverflowError, TypeError, ValueError):
                return True
        except (OverflowError, TypeError, ValueError):
            continue
    return False


def _check(action: Action, proposal: Proposal, web3: Web3) -> CheckResult:
    evidence: list[RpcEvidence] = []
    try:
        data = bytes.fromhex(action.calldata[2:])
        values = decode(["address"], data[4:])
        if data != ADD_PAIR + encode(["address"], values):
            return _result(action, "UNKNOWN", "Pair add calldata is not canonical.", evidence)
        pair = Web3.to_checksum_address(values[0])
        if action.unresolved or action.value_wei != 0:
            return _result(
                action,
                "UNKNOWN",
                "Pair add action is unresolved or carries value.",
                evidence,
            )
        if action.target not in {REGISTRY, *PAIR_ADDERS}:
            return _result(
                action,
                "UNKNOWN",
                "addPair target is not a reviewed Registry or PairAdder.",
                evidence,
            )
        if _changes_pair_deployer(proposal, action.index):
            return _result(
                action,
                "UNKNOWN",
                "An earlier action changes PAIR_DEPLOYER, so proposal pre-state is insufficient.",
                evidence,
            )
        if action.target in PAIR_ADDERS:
            raw_registry, item = rpc_call(
                web3, action.target, PAIR_ADDER_REGISTRY, proposal.creation_block
            )
            evidence.append(item)
            raw_core, item = rpc_call(web3, action.target, CORE, proposal.creation_block)
            evidence.append(item)
            if _address(raw_registry) != REGISTRY or _address(raw_core) != action.executor:
                return _result(
                    action,
                    "FAIL",
                    "Reviewed PairAdder is not bound to the expected Registry and executor.",
                    evidence,
                )
        pointer_call = GET_ADDRESS + encode(["string"], [PAIR_DEPLOYER_KEY])
        raw_deployer, item = rpc_call(web3, REGISTRY, pointer_call, proposal.creation_block)
        evidence.append(item)
        deployer = _address(raw_deployer)
        code, item = rpc_code(web3, deployer, proposal.creation_block)
        evidence.append(item)
        if not code:
            return _result(action, "FAIL", "Registry PAIR_DEPLOYER pointer has no code.", evidence)
        deploy_info_call = DEPLOY_INFO + encode(["address"], [pair])
        raw_info, item = rpc_call(web3, deployer, deploy_info_call, proposal.creation_block)
        evidence.append(item)
        values = decode(["uint40", "uint40"], raw_info)
        if raw_info != encode(["uint40", "uint40"], values):
            raise ValueError("deployInfo response was not canonical")
        protocol_id, deploy_time = (int(values[0]), int(values[1]))
        if deploy_time == 0:
            return _result(
                action,
                "FAIL",
                "PairDeployer has no deployment record for the proposed pair.",
                evidence,
            )
        return _result(
            action,
            "PASS",
            f"PairDeployer proves protocol {protocol_id} deployment at {deploy_time}.",
            evidence,
        )
    except (OverflowError, TypeError, ValueError, OSError):
        return _result(
            action,
            "UNKNOWN",
            "PairDeployer provenance could not be deterministically resolved.",
            evidence,
        )
    except Exception:
        return _result(
            action,
            "UNKNOWN",
            "A block-pinned Resupply provenance RPC read failed.",
            evidence,
        )


def run(proposal: Proposal, web3: Web3) -> list[CheckResult]:
    results: list[CheckResult] = []
    for action in proposal.actions:
        try:
            data = bytes.fromhex(action.calldata[2:])
        except ValueError:
            continue
        if data[:4] == ADD_PAIR:
            results.append(_check(action, proposal, web3))
    return results
