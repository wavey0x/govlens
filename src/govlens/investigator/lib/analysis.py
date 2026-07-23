"""Read-only analysis facade.

The public import surface remains `lib.analysis`, while the heavier
implementations live in focused submodules.
"""

import json
import os
import urllib.request
from urllib.parse import urlencode

try:
    from eth_abi import decode as _eth_abi_decode
except ImportError:
    _eth_abi_decode = None

from .analysis_cache import (
    _ERC20_TRANSFER_TOPIC,
    _EIP1967_IMPL_SLOT,
    _UNVERIFIED_ABI,
    _abi_cache,
    _first_code_block_cache,
    _get_abi,
    _identify_cache,
    _selector_cache,
    _source_cache,
    _stash_abi,
)
from .analysis_contracts import (
    first_code_block_impl,
    identify_impl,
    read_contract_impl,
    search_contract_logs_impl,
)
from .analysis_decode import _serialize_param, decode_tx_impl
from .analysis_trace import (
    _debug_trace_request_impl,
    decode_trace_impl,
    state_diff_impl,
    trace_tx_impl,
)
from .explorer import etherscan_get
from .network import _NULL_ADDR, _UA, _normalize_chain, get_w3


_SOURCIFY_4BYTE_BASE_URL = os.getenv(
    "SOURCIFY_4BYTE_BASE_URL",
    "https://api.4byte.sourcify.dev",
).rstrip("/")
_SOURCIFY_4BYTE_LOOKUP_PATH = "/signature-database/v1/lookup"


def decode_tx(tx_hash: str, chain: str = "eth", abi: list | None = None,
              *, _tx=None, _receipt=None) -> dict:
    """Decode a transaction's function call and ERC-20 transfers."""

    return decode_tx_impl(
        tx_hash,
        chain,
        abi,
        _tx=_tx,
        _receipt=_receipt,
        normalize_chain=_normalize_chain,
        get_w3=get_w3,
        get_abi=_get_abi,
        transfer_topic=_ERC20_TRANSFER_TOPIC,
    )


def identify(
    address: str,
    chain: str = "eth",
    *,
    verify_empty_code: bool = False,
    shallow: bool = False,
) -> dict:
    """Profile an address: EOA vs contract, name, proxy status, verification."""

    return identify_impl(
        address,
        chain,
        normalize_chain=_normalize_chain,
        get_w3=get_w3,
        etherscan_get=etherscan_get,
        stash_abi=_stash_abi,
        abi_cache=_abi_cache,
        source_cache=_source_cache,
        identify_cache=_identify_cache,
        unverified_abi=_UNVERIFIED_ABI,
        null_addr=_NULL_ADDR,
        impl_slot=_EIP1967_IMPL_SLOT,
        verify_empty_code=verify_empty_code,
        shallow=shallow,
    )


def read_contract(address: str, function: str, *args,
                  chain: str = "eth", block: int | str = "latest",
                  abi: list | None = None):
    """Call a view function on a contract at any block using cached ABI.

    Function arguments are positional after `function`; do not pass `args=[...]`.

    Examples:
    - `read_contract(token, "symbol", chain="op")`
    - `read_contract(token, "balanceOf", wallet, chain="op")`
    - `read_contract(controller, "positions", vault_id, chain="op", block=123456)`
    """

    return read_contract_impl(
        address,
        function,
        *args,
        chain=chain,
        block=block,
        abi=abi,
        normalize_chain=_normalize_chain,
        get_w3=get_w3,
        get_abi=_get_abi,
        identify=identify,
    )


def first_code_block(address: str, chain: str = "eth", *, upper_block: int | str = "latest") -> dict:
    """Find the first block where an address had code."""

    return first_code_block_impl(
        address,
        chain,
        upper_block=upper_block,
        normalize_chain=_normalize_chain,
        get_w3=get_w3,
        etherscan_get=etherscan_get,
        first_code_block_cache=_first_code_block_cache,
    )


def search_contract_logs(
    address: str,
    chain: str = "eth",
    *,
    topic0: str | bytes | None = None,
    topics: list | tuple | None = None,
    from_block: int | None = None,
    to_block: int | str = "latest",
    chunk_size: int = 50_000,
    max_chunks: int = 200,
    allow_large: bool = False,
) -> dict:
    """Search logs for a contract, clamping the start block to first code."""

    return search_contract_logs_impl(
        address,
        chain,
        topic0=topic0,
        topics=topics,
        from_block=from_block,
        to_block=to_block,
        chunk_size=chunk_size,
        max_chunks=max_chunks,
        allow_large=allow_large,
        normalize_chain=_normalize_chain,
        get_w3=get_w3,
        etherscan_get=etherscan_get,
        first_code_block=first_code_block,
    )


def _debug_trace_request(tx_hash: str, chain: str, tracer: str, config: dict) -> dict:
    """Execute debug_traceTransaction with consistent error handling."""

    return _debug_trace_request_impl(
        tx_hash,
        chain,
        tracer,
        config,
        get_w3=get_w3,
        normalize_chain=_normalize_chain,
    )


def trace_tx(tx_hash: str, chain: str = "eth") -> dict:
    """Fetch a debug_traceTransaction call tree for the given tx."""

    return trace_tx_impl(tx_hash, chain, debug_trace_request=_debug_trace_request)


def _normalize_signature_hash(hex_selector: str) -> tuple[str, str] | None:
    """Normalize a function/error selector or event topic for signature lookup."""

    if not isinstance(hex_selector, str):
        return None

    sel = hex_selector.strip().lower()
    if sel.startswith("0x"):
        sel = sel[2:]

    if len(sel) not in (8, 64):
        return None

    try:
        int(sel, 16)
    except ValueError:
        return None

    kind = "event" if len(sel) == 64 else "function"
    return kind, f"0x{sel}"


def _parse_sourcify_signature_response(
    data: dict,
    kind: str,
    normalized_hash: str,
) -> set[str]:
    """Extract signature names from a Sourcify 4byte lookup response."""

    if not isinstance(data, dict) or data.get("ok") is not True:
        return set()

    result = data.get("result", {})
    if not isinstance(result, dict):
        return set()

    signatures_by_hash = result.get(kind, {})
    if not isinstance(signatures_by_hash, dict):
        return set()

    entries = signatures_by_hash.get(normalized_hash)
    if entries is None:
        return set()
    if not isinstance(entries, list):
        return set()

    results = set()
    for entry in entries:
        if isinstance(entry, dict):
            sig = entry.get("name", "").strip()
        elif isinstance(entry, str):
            sig = entry.strip()
        else:
            sig = ""
        if sig:
            results.add(sig)
    return results


def _is_valid_sourcify_signature_response(
    data: dict,
    kind: str,
    normalized_hash: str,
) -> bool:
    """Return True when Sourcify answered with a valid lookup/no-match shape."""

    if not isinstance(data, dict) or data.get("ok") is not True:
        return False

    result = data.get("result")
    if not isinstance(result, dict):
        return False

    signatures_by_hash = result.get(kind)
    if not isinstance(signatures_by_hash, dict):
        return False

    if normalized_hash not in signatures_by_hash:
        return False

    entries = signatures_by_hash.get(normalized_hash)
    return entries is None or isinstance(entries, list)


def lookup_selector(hex_selector: str) -> list[str]:
    """Resolve a function selector, error selector, or event topic."""

    normalized = _normalize_signature_hash(hex_selector)
    if normalized is None:
        return []

    kind, normalized_hash = normalized
    cache_key = normalized_hash[2:]
    if cache_key in _selector_cache:
        return _selector_cache[cache_key]

    results = set()
    request_succeeded = False

    try:
        query = urlencode({kind: normalized_hash, "filter": "true"})
        url = f"{_SOURCIFY_4BYTE_BASE_URL}{_SOURCIFY_4BYTE_LOOKUP_PATH}?{query}"
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        request_succeeded = _is_valid_sourcify_signature_response(data, kind, normalized_hash)
        if request_succeeded:
            results = _parse_sourcify_signature_response(data, kind, normalized_hash)
    except Exception:
        pass

    resolved = sorted(results)
    if request_succeeded or results:
        _selector_cache[cache_key] = resolved
    return resolved


def state_diff(tx_hash: str, chain: str = "eth") -> dict:
    """Fetch pre/post state diff via prestateTracer."""

    return state_diff_impl(tx_hash, chain, debug_trace_request=_debug_trace_request)


def decode_trace(tx_hash: str, chain: str = "eth", *,
                 trace: dict | None = None,
                 receipt=None,
                 resolve_selector: bool = False) -> list[dict]:
    """Decode a nested call trace into a flat list with function names and params."""

    return decode_trace_impl(
        tx_hash,
        chain,
        trace=trace,
        receipt=receipt,
        resolve_selector=resolve_selector,
        normalize_chain=_normalize_chain,
        get_w3=get_w3,
        trace_tx=trace_tx,
        get_abi=_get_abi,
        lookup_selector=lookup_selector,
        serialize_param=_serialize_param,
        transfer_topic=_ERC20_TRANSFER_TOPIC,
        eth_abi_decode=_eth_abi_decode,
    )
