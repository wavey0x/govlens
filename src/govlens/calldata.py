"""Best-effort calldata decoding for report presentation."""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
from web3 import Web3

from .model import Action, Proposal

ETHERSCAN_API = "https://api.etherscan.io/v2/api"
MAX_ABI_ITEMS = 2_048
MAX_INPUTS = 64
MAX_VALUE_CHARS = 1_000
DECODE_BUDGET_SECONDS = 15
REQUEST_TIMEOUT_SECONDS = 5
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_SIGNATURE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\([A-Za-z0-9_,()[\]]*\)")
_TYPE = re.compile(r"[A-Za-z0-9_()[\],]{1,128}")


def _fetch_abi(
    client: httpx.Client,
    api_key: str,
    address: str,
    timeout: float,
) -> list[dict[str, Any]] | None:
    response = client.get(
        ETHERSCAN_API,
        params={
            "chainid": "1",
            "module": "contract",
            "action": "getabi",
            "address": address,
            "apikey": api_key,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()
    raw = body.get("result") if isinstance(body, dict) else None
    if body.get("status") != "1" or not isinstance(raw, str):
        return None
    abi = json.loads(raw)
    if (
        not isinstance(abi, list)
        or not abi
        or len(abi) > MAX_ABI_ITEMS
        or not all(isinstance(item, dict) for item in abi)
    ):
        return None
    return abi


def _plain(value: Any) -> Any:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _display(value: Any, solidity_type: str) -> str:
    plain = _plain(value)
    if isinstance(plain, str) and solidity_type != "string":
        rendered = plain
    elif isinstance(plain, bool):
        rendered = str(plain).lower()
    elif isinstance(plain, int):
        rendered = f"{plain:,}"
    else:
        rendered = json.dumps(plain, ensure_ascii=True, separators=(",", ":"))
    if len(rendered) > MAX_VALUE_CHARS:
        return rendered[: MAX_VALUE_CHARS - 1] + "…"
    return rendered


def _decode(action: Action, abi: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        contract = Web3().eth.contract(
            address=Web3.to_checksum_address(action.target),
            abi=abi,
        )
        function, arguments = contract.decode_function_input(action.calldata)
        signature = function.signature
        inputs = function.abi.get("inputs", [])
        if (
            not isinstance(signature, str)
            or not _SIGNATURE.fullmatch(signature)
            or not isinstance(inputs, list)
            or len(inputs) > MAX_INPUTS
        ):
            return None

        ordered: list[Any] = []
        decoded_inputs: list[dict[str, str]] = []
        for index, item in enumerate(inputs):
            if not isinstance(item, dict):
                return None
            original_name = item.get("name")
            solidity_type = item.get("type")
            if (
                not isinstance(original_name, str)
                or original_name not in arguments
                or not isinstance(solidity_type, str)
                or not _TYPE.fullmatch(solidity_type)
            ):
                return None
            value = arguments[original_name]
            ordered.append(value)
            name = original_name if _IDENTIFIER.fullmatch(original_name) else f"arg{index}"
            decoded_inputs.append(
                {
                    "name": name,
                    "type": solidity_type,
                    "value": _display(value, solidity_type),
                }
            )

        encoded = contract.encode_abi(signature, args=ordered)
        if encoded.casefold() != action.calldata.casefold():
            return None
        return {
            "action_index": action.index,
            "function": signature,
            "inputs": decoded_inputs,
        }
    except Exception:
        return None


def decode_actions(proposal: Proposal, api_key: str) -> list[dict[str, Any]]:
    """Decode complete actions against verified target ABIs without blocking delivery."""

    if not api_key or not proposal.actions:
        return []
    decoded: list[dict[str, Any]] = []
    cache: dict[str, list[dict[str, Any]] | None] = {}
    deadline = time.monotonic() + DECODE_BUDGET_SECONDS
    try:
        with httpx.Client() as client:
            for action in proposal.actions:
                address = action.target.casefold()
                if address not in cache:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        cache[address] = _fetch_abi(
                            client,
                            api_key,
                            action.target,
                            min(REQUEST_TIMEOUT_SECONDS, remaining),
                        )
                    except Exception:
                        cache[address] = None
                abi = cache[address]
                if abi is not None:
                    call = _decode(action, abi)
                    if call is not None:
                        decoded.append(call)
    except Exception:
        return decoded
    return decoded
