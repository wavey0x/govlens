"""Curve gauge-add checks against the deployed canonical validator."""

from __future__ import annotations

from eth_abi import decode, encode
from web3 import Web3

from ..model import Action, Proposal
from . import CheckResult, CheckStatus, RpcEvidence, rpc_call, rpc_code

GAUGE_CONTROLLER = Web3.to_checksum_address("0x2F50D538606Fa9EDD2B11E2446BEb18C9D5846bB")
GAUGE_VALIDATOR = Web3.to_checksum_address("0xd9B076a960B74ECc17ee4C76a29aa9AFff19F3C7")
ADD_GAUGE_2 = bytes(Web3.keccak(text="add_gauge(address,int128)")[:4])
ADD_GAUGE_3 = bytes(Web3.keccak(text="add_gauge(address,int128,uint256)")[:4])
VALIDATE_GAUGE = bytes(Web3.keccak(text="validateGauge(address)")[:4])


def _result(
    action: Action,
    status: CheckStatus,
    summary: str,
    evidence: list[RpcEvidence],
) -> CheckResult:
    return CheckResult(
        id="curve.gauge_validator",
        action_index=action.index,
        status=status,
        summary=summary,
        evidence=tuple(evidence),
    )


def _validator_changed_earlier(proposal: Proposal, before: int) -> bool:
    return any(action.target == GAUGE_VALIDATOR for action in proposal.actions[:before])


def _check(action: Action, proposal: Proposal, web3: Web3) -> CheckResult:
    evidence: list[RpcEvidence] = []
    try:
        data = bytes.fromhex(action.calldata[2:])
        types = (
            ["address", "int128"]
            if data[:4] == ADD_GAUGE_2
            else [
                "address",
                "int128",
                "uint256",
            ]
        )
        values = decode(types, data[4:])
        if data != data[:4] + encode(types, values):
            return _result(action, "UNKNOWN", "Gauge add calldata is not canonical.", evidence)
        gauge = Web3.to_checksum_address(values[0])
        if action.unresolved or action.value_wei != 0:
            return _result(
                action,
                "UNKNOWN",
                "Gauge add action is unresolved or carries value.",
                evidence,
            )
        if _validator_changed_earlier(proposal, action.index):
            return _result(
                action,
                "UNKNOWN",
                "An earlier action calls the gauge validator, so pre-state validation is stale.",
                evidence,
            )
        code, item = rpc_code(web3, GAUGE_VALIDATOR, proposal.creation_block)
        evidence.append(item)
        if not code:
            return _result(
                action,
                "UNKNOWN",
                "The configured Curve gauge validator had no code in proposal pre-state.",
                evidence,
            )
        calldata = VALIDATE_GAUGE + encode(["address"], [gauge])
        raw_valid, item = rpc_call(web3, GAUGE_VALIDATOR, calldata, proposal.creation_block)
        evidence.append(item)
        valid_values = decode(["bool"], raw_valid)
        if raw_valid != encode(["bool"], valid_values):
            raise ValueError("gauge validator response was not canonical")
        if not bool(valid_values[0]):
            return _result(
                action,
                "FAIL",
                (
                    "The on-chain Curve gauge validator rejects this gauge "
                    "under its current configuration."
                ),
                evidence,
            )
        return _result(
            action,
            "PASS",
            "The on-chain Curve gauge validator accepts this gauge.",
            evidence,
        )
    except (OverflowError, TypeError, ValueError, OSError):
        return _result(
            action,
            "UNKNOWN",
            "The Curve gauge validator result could not be decoded conclusively.",
            evidence,
        )
    except Exception:
        return _result(
            action,
            "UNKNOWN",
            "The block-pinned Curve gauge validator call failed.",
            evidence,
        )


def run(proposal: Proposal, web3: Web3) -> list[CheckResult]:
    results: list[CheckResult] = []
    for action in proposal.actions:
        try:
            data = bytes.fromhex(action.calldata[2:])
        except ValueError:
            continue
        if action.target == GAUGE_CONTROLLER and data[:4] in {ADD_GAUGE_2, ADD_GAUGE_3}:
            results.append(_check(action, proposal, web3))
    return results
