"""Shared caches, constants, and ABI helpers for analysis helpers."""

from __future__ import annotations

import json

from .explorer import etherscan_get

_abi_cache: dict[str, list] = {}
_source_cache: dict[str, list] = {}
_identify_cache: dict[str, dict] = {}
_selector_cache: dict[str, list[str]] = {}
_first_code_block_cache: dict[str, dict] = {}

# Etherscan returns this literal string for unverified contracts.
_UNVERIFIED_ABI = "Contract source code not verified"

# ERC-20 Transfer(address,address,uint256) topic0
_ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# EIP-1967 implementation slot
_EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"

def _stash_abi(entry: dict, slug: str, address: str) -> list | None:
    """Parse ABI from an Etherscan getsourcecode entry and cache it."""

    abi_raw = entry.get("ABI", "")
    if not abi_raw or abi_raw == _UNVERIFIED_ABI:
        return None
    try:
        parsed = json.loads(abi_raw)
        if isinstance(parsed, list) and parsed:
            _abi_cache[f"{slug}:{address.lower()}"] = parsed
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def _get_abi(address: str, chain: str) -> list | None:
    """Fetch and cache ABI for a verified contract."""

    key = f"{chain}:{address.lower()}"
    if key in _abi_cache:
        return _abi_cache[key]
    try:
        resp = etherscan_get("contract", "getabi", chain=chain, address=address)
        abi = json.loads(resp["result"]) if isinstance(resp.get("result"), str) else resp.get("result")
        if isinstance(abi, list) and abi:
            _abi_cache[key] = abi
            return abi
    except Exception:
        pass
    _abi_cache[key] = None
    return None
