"""Ethereum provider and environment helpers for proposal investigations."""

import os

from web3 import Web3

CHAINS = {"eth": {"id": 1, "explorer": "https://etherscan.io"}}
_ALIASES = {"ethereum": "eth", "mainnet": "eth"}
_NULL_ADDR = "0x0000000000000000000000000000000000000000"
_PLACEHOLDER_PREFIXES = ("your_", "your-", "changeme", "xxx", "todo")
_UA = "govlens-investigator"
_w3: Web3 | None = None


def _normalize_chain(name: str) -> str:
    slug = _ALIASES.get(name.casefold().strip(), name.casefold().strip())
    if slug != "eth":
        raise ValueError("GovLens investigation helpers support Ethereum only")
    return slug


def chain_id(name: str = "eth") -> int:
    _normalize_chain(name)
    return 1


def get_w3(chain: str = "eth") -> Web3:
    """Return the shared read-only Ethereum provider."""

    global _w3
    _normalize_chain(chain)
    if _w3 is not None:
        return _w3
    rpc_url = os.environ.get("RPC_URL_ETH", "").strip()
    if not rpc_url:
        raise OSError("RPC_URL_ETH is not configured")
    _w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 60}))
    return _w3


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().casefold()
    return not lowered or any(
        lowered == prefix or lowered.startswith(prefix) for prefix in _PLACEHOLDER_PREFIXES
    )


def preflight_check(chain: str = "eth") -> dict:
    """Check required read-only credentials without making a network call."""

    slug = _normalize_chain(chain)
    blocking = []
    warnings = []
    if _is_placeholder(os.environ.get("RPC_URL_ETH", "")):
        blocking.append("RPC_URL_ETH")
    if _is_placeholder(os.environ.get("ETHERSCAN_KEY", "")):
        warnings.append("ETHERSCAN_KEY")
    return {
        "ready": not blocking,
        "chain": slug,
        "blocking": blocking,
        "warnings": warnings,
    }
