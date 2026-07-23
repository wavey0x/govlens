"""Contract identity and read helpers."""

from __future__ import annotations

from web3 import Web3


def _abi_has_function(abi: list | None, function: str) -> bool:
    """Return True when an ABI exposes the requested function name."""

    if not abi:
        return False
    return any(
        isinstance(item, dict)
        and item.get("type") == "function"
        and item.get("name") == function
        for item in abi
    )


def _has_code(code: bytes | bytearray | str | None) -> bool:
    """Return True when a code response is non-empty."""

    if isinstance(code, (bytes, bytearray)):
        return len(code) > 0
    if isinstance(code, str):
        normalized = code.strip().lower()
        return normalized not in {"", "0x", "0x0"}
    return False


def _hex_value(value) -> str:
    """Convert bytes-like values to 0x-prefixed hex strings for log output."""

    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    if hasattr(value, "hex") and not isinstance(value, str):
        try:
            text = value.hex()
            return text if text.startswith("0x") else f"0x{text}"
        except Exception:
            pass
    return str(value)


def _block_identifier_to_number(w3, block_identifier: int | str) -> int:
    """Resolve an integer or simple string block identifier to a block number."""

    if isinstance(block_identifier, int):
        return block_identifier
    if block_identifier == "earliest":
        return 0
    if block_identifier == "latest":
        return int(w3.eth.block_number)
    if isinstance(block_identifier, str):
        if block_identifier.isdigit():
            return int(block_identifier)
        if block_identifier.startswith("0x"):
            return int(block_identifier, 16)
    raise ValueError(f"Unsupported block identifier: {block_identifier!r}")


def _normalize_optional_address(value: str | None) -> str | None:
    """Checksum an address when present; return None for blank/invalid values."""

    if not value:
        return None
    try:
        return Web3.to_checksum_address(value)
    except Exception:
        return None


def _normalize_optional_topic(value) -> str | None:
    """Normalize a topic value for Etherscan/RPC log filters."""

    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        raise ValueError("OR-topic lists are not supported by search_contract_logs")
    text = _hex_value(value).strip()
    if not text:
        return None
    if not text.startswith("0x"):
        text = f"0x{text}"
    return text.lower()


def _topic_filter(topic0=None, topics: list | tuple | None = None) -> list[str | None]:
    """Build a normalized positional topic filter."""

    normalized: list[str | None] = []
    if topics:
        normalized = [_normalize_optional_topic(topic) for topic in topics]
    if topic0 is not None:
        if not normalized:
            normalized = []
        while len(normalized) < 1:
            normalized.append(None)
        normalized[0] = _normalize_optional_topic(topic0)
    while normalized and normalized[-1] is None:
        normalized.pop()
    return normalized


def _serialize_log(log: dict) -> dict:
    """Normalize Etherscan/RPC log records into a JSON-friendly shape."""

    row = dict(log)
    if "topics" in row and isinstance(row["topics"], (list, tuple)):
        row["topics"] = [_hex_value(topic) for topic in row["topics"]]
    if "data" in row:
        row["data"] = _hex_value(row["data"])
    for key in ("blockNumber", "transactionIndex", "logIndex"):
        value = row.get(key)
        if isinstance(value, str) and value.startswith("0x"):
            try:
                row[key] = int(value, 16)
            except ValueError:
                pass
    return row


def _creation_block_from_etherscan(addr: str, *, slug: str, w3, etherscan_get) -> dict | None:
    """Return a contract creation block from Etherscan metadata when available."""

    resp = etherscan_get("contract", "getcontractcreation", chain=slug, contractaddresses=addr)
    entries = resp.get("result", [])
    if not isinstance(entries, list):
        return None
    for entry in entries:
        contract_addr = _normalize_optional_address(entry.get("contractAddress"))
        if contract_addr != addr:
            continue
        tx_hash = (entry.get("txHash") or "").strip()
        if not tx_hash:
            return None
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        block_number = int(receipt["blockNumber"])
        return {
            "address": addr,
            "chain": slug,
            "block": block_number,
            "source": "etherscan_contract_creation",
            "tx_hash": tx_hash,
        }
    return None


def first_code_block_impl(
    address: str,
    chain: str = "eth",
    *,
    upper_block: int | str = "latest",
    normalize_chain,
    get_w3,
    etherscan_get,
    first_code_block_cache: dict[str, dict],
) -> dict:
    """Find the first block where an address has code.

    Etherscan contract-creation metadata is the fast path. If that is missing,
    fall back to a direct RPC binary search over eth_getCode.
    """

    slug = normalize_chain(chain)
    try:
        addr = Web3.to_checksum_address(address)
    except Exception:
        return {"error": f"Invalid address: {address!r}"}

    w3 = get_w3(slug)
    try:
        upper_number = _block_identifier_to_number(w3, upper_block)
    except Exception as exc:
        return {"error": str(exc), "address": addr, "chain": slug}
    if upper_number < 0:
        return {"error": f"upper_block must be non-negative, got {upper_number}", "address": addr, "chain": slug}

    cache_key = f"{slug}:{addr.lower()}:{upper_number}"
    if cache_key in first_code_block_cache:
        return dict(first_code_block_cache[cache_key])

    errors: list[str] = []
    try:
        result = _creation_block_from_etherscan(addr, slug=slug, w3=w3, etherscan_get=etherscan_get)
        if result is not None:
            result["upper_block"] = upper_number
            result["after_upper_block"] = result["block"] > upper_number
            first_code_block_cache[cache_key] = dict(result)
            return result
    except Exception as exc:
        errors.append(f"etherscan_contract_creation: {exc}")

    rpc_checks = 0

    def _code_at(block_number: int):
        nonlocal rpc_checks
        rpc_checks += 1
        return w3.eth.get_code(addr, block_identifier=block_number)

    try:
        if not _has_code(_code_at(upper_number)):
            return {
                "error": f"No code found at {addr} by upper_block {upper_number}",
                "address": addr,
                "chain": slug,
                "upper_block": upper_number,
                "source": "rpc_binary_search",
                "rpc_checks": rpc_checks,
                "errors": errors,
            }

        low = 0
        high = upper_number
        while low < high:
            mid = (low + high) // 2
            if _has_code(_code_at(mid)):
                high = mid
            else:
                low = mid + 1
    except Exception as exc:
        return {
            "error": f"RPC first-code search failed: {exc}",
            "address": addr,
            "chain": slug,
            "upper_block": upper_number,
            "source": "rpc_binary_search",
            "rpc_checks": rpc_checks,
            "errors": errors,
        }

    result = {
        "address": addr,
        "chain": slug,
        "block": low,
        "source": "rpc_binary_search",
        "upper_block": upper_number,
        "after_upper_block": False,
        "rpc_checks": rpc_checks,
        "errors": errors,
    }
    first_code_block_cache[cache_key] = dict(result)
    return result


def search_contract_logs_impl(
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
    normalize_chain,
    get_w3,
    etherscan_get,
    first_code_block,
) -> dict:
    """Search contract logs without allowing accidental block-zero scans."""

    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1")
    if max_chunks < 1:
        raise ValueError("max_chunks must be at least 1")

    slug = normalize_chain(chain)
    try:
        addr = Web3.to_checksum_address(address)
    except Exception:
        return {"error": f"Invalid address: {address!r}"}

    w3 = get_w3(slug)
    try:
        to_number = _block_identifier_to_number(w3, to_block)
    except Exception as exc:
        return {"error": str(exc), "address": addr, "chain": slug}

    deploy = first_code_block(addr, chain=slug, upper_block=to_number)
    if "error" in deploy:
        return {"error": f"Could not resolve first-code block: {deploy['error']}", "deploy": deploy}

    deploy_block = int(deploy["block"])
    requested_from = from_block
    actual_from = deploy_block if from_block is None else max(int(from_block), deploy_block)

    if actual_from > to_number:
        return {
            "address": addr,
            "chain": slug,
            "deploy_block": deploy_block,
            "from_block": actual_from,
            "to_block": to_number,
            "requested_from_block": requested_from,
            "source": "none",
            "logs": [],
            "log_count": 0,
            "chunks": 0,
            "clamped": requested_from is None or (requested_from is not None and requested_from < deploy_block),
            "errors": [],
        }

    block_count = to_number - actual_from + 1
    chunks_needed = (block_count + chunk_size - 1) // chunk_size
    if chunks_needed > max_chunks and not allow_large:
        return {
            "error": (
                f"Refusing log search across {block_count} blocks ({chunks_needed} chunks). "
                "Pass a tighter block range or allow_large=True."
            ),
            "address": addr,
            "chain": slug,
            "deploy_block": deploy_block,
            "from_block": actual_from,
            "to_block": to_number,
            "chunk_size": chunk_size,
            "max_chunks": max_chunks,
        }

    topic_filter = _topic_filter(topic0, topics)
    logs: list[dict] = []
    errors: list[str] = []
    source = "etherscan_logs"

    def _ranges():
        start = actual_from
        while start <= to_number:
            end = min(start + chunk_size - 1, to_number)
            yield start, end
            start = end + 1

    try:
        for start, end in _ranges():
            params = {"address": addr, "fromBlock": start, "toBlock": end}
            for idx, topic in enumerate(topic_filter):
                if topic is not None:
                    params[f"topic{idx}"] = topic
            try:
                resp = etherscan_get("logs", "getLogs", chain=slug, **params)
                chunk_logs = resp.get("result", [])
                if isinstance(chunk_logs, list):
                    logs.extend(_serialize_log(row) for row in chunk_logs)
            except Exception as exc:
                if "no records found" in str(exc).lower():
                    continue
                raise
    except Exception as exc:
        errors.append(f"etherscan_logs: {exc}")
        logs = []
        source = "rpc_get_logs"
        for start, end in _ranges():
            params = {
                "address": addr,
                "fromBlock": start,
                "toBlock": end,
                "topics": topic_filter,
            }
            try:
                chunk_logs = w3.eth.get_logs(params)
                logs.extend(_serialize_log(row) for row in chunk_logs)
            except Exception as rpc_exc:
                errors.append(f"rpc_get_logs {start}-{end}: {rpc_exc}")

    return {
        "address": addr,
        "chain": slug,
        "deploy_block": deploy_block,
        "deploy": deploy,
        "from_block": actual_from,
        "to_block": to_number,
        "requested_from_block": requested_from,
        "clamped": requested_from is None or (requested_from is not None and requested_from < deploy_block),
        "topic0": topic_filter[0] if topic_filter else None,
        "topics": topic_filter,
        "source": source,
        "chunk_size": chunk_size,
        "chunks": chunks_needed,
        "log_count": len(logs),
        "logs": logs,
        "errors": errors,
    }




def _is_eip7702_designator(code: bytes | bytearray | str | None) -> bool:
    """Return True when code matches the 23-byte EIP-7702 designator shape."""

    raw: bytes | None = None
    if isinstance(code, (bytes, bytearray)):
        raw = bytes(code)
    elif isinstance(code, str):
        normalized = code.strip().lower()
        if normalized.startswith("0x"):
            try:
                raw = bytes.fromhex(normalized[2:])
            except ValueError:
                raw = None

    return bool(raw and len(raw) == 23 and raw[:3] == b"\xef\x01\x00")


def _extract_eip7702_delegate(code: bytes | bytearray | str | None) -> str | None:
    """Return the delegated target when account code is an EIP-7702 designator."""

    raw: bytes | None = None
    if isinstance(code, (bytes, bytearray)):
        raw = bytes(code)
    elif isinstance(code, str):
        normalized = code.strip().lower()
        if normalized.startswith("0x"):
            try:
                raw = bytes.fromhex(normalized[2:])
            except ValueError:
                raw = None

    if not raw or len(raw) != 23 or raw[:3] != b"\xef\x01\x00":
        return None
    if raw[3:] == b"\x00" * 20:
        return None

    try:
        return Web3.to_checksum_address("0x" + raw[3:].hex())
    except Exception:
        return None




def identify_impl(
    address: str,
    chain: str = "eth",
    *,
    normalize_chain,
    get_w3,
    etherscan_get,
    stash_abi,
    abi_cache: dict[str, list],
    source_cache: dict[str, list],
    identify_cache: dict[str, dict],
    unverified_abi: str,
    null_addr: str,
    impl_slot: str,
    verify_empty_code: bool = False,
    shallow: bool = False,
) -> dict:
    """Profile an address: EOA vs contract, name, proxy status, verification."""

    slug = normalize_chain(chain)

    try:
        addr = Web3.to_checksum_address(address)
    except Exception:
        return {"error": f"Invalid address: {address!r}"}

    cache_key = f"{slug}:{addr.lower()}"
    if shallow:
        cache_key = f"{cache_key}:shallow"
    if cache_key in identify_cache:
        return identify_cache[cache_key]

    w3 = get_w3(slug)

    try:
        code = w3.eth.get_code(addr)
    except Exception as exc:
        return {"error": f"RPC call failed: {exc}"}

    code_confirmed = True
    if verify_empty_code and not _has_code(code):
        try:
            fallback = etherscan_get(
                "proxy",
                "eth_getCode",
                chain=slug,
                address=addr,
                tag="latest",
            )
            code = fallback.get("result")
        except Exception:
            code_confirmed = False
            code = b""

    if not _has_code(code):
        if not verify_empty_code:
            result = {
                "address": addr,
                "type": "eoa",
                "name": None,
                "proxy": False,
                "implementation": None,
                "verified": False,
                "delegated": False,
                "delegated_to": None,
            }
            identify_cache[cache_key] = result
            return result

        creation_checked = False
        try:
            creation_resp = etherscan_get(
                "contract",
                "getcontractcreation",
                chain=slug,
                contractaddresses=addr,
            )
            creation_checked = True
            entries = creation_resp.get("result", [])
            if isinstance(entries, list):
                for entry in entries:
                    contract_addr = _normalize_optional_address(entry.get("contractAddress"))
                    if contract_addr == addr:
                        result = {
                            "address": addr,
                            "type": "contract",
                            "name": None,
                            "proxy": False,
                            "implementation": None,
                            "verified": False,
                            "delegated": False,
                            "delegated_to": None,
                        }
                        identify_cache[cache_key] = result
                        return result
        except Exception:
            creation_checked = False

        result = {
            "address": addr,
            "type": "eoa" if code_confirmed and creation_checked else "unknown",
            "name": None,
            "proxy": False,
            "implementation": None,
            "verified": False,
            "delegated": False,
            "delegated_to": None,
        }
        identify_cache[cache_key] = result
        return result

    if _is_eip7702_designator(code):
        delegated_to = _extract_eip7702_delegate(code)
        if delegated_to:
            result = {
                "address": addr,
                "type": "delegated_eoa",
                "name": None,
                "proxy": False,
                "implementation": None,
                "verified": False,
                "delegated": True,
                "delegated_to": delegated_to,
            }
        else:
            result = {
                "address": addr,
                "type": "unknown",
                "name": None,
                "proxy": False,
                "implementation": None,
                "verified": False,
                "delegated": False,
                "delegated_to": None,
            }
        identify_cache[cache_key] = result
        return result

    if shallow:
        result = {
            "address": addr,
            "type": "contract",
            "name": None,
            "proxy": False,
            "implementation": None,
            "verified": False,
            "delegated": False,
            "delegated_to": None,
        }
        identify_cache[cache_key] = result
        return result

    name = None
    verified = False
    etherscan_proxy = False
    etherscan_impl = None

    try:
        resp = etherscan_get("contract", "getsourcecode", chain=slug, address=addr)
        result = resp.get("result", [])
        if result and isinstance(result, list):
            source_cache[f"{slug}:{addr.lower()}"] = result
            entry = result[0]
            contract_name = (entry.get("ContractName") or "").strip()
            if contract_name:
                name = contract_name
                verified = True
            if entry.get("Proxy") == "1":
                etherscan_proxy = True
                etherscan_impl = (entry.get("Implementation") or "").strip() or None
            abi_raw = entry.get("ABI", "")
            if abi_raw and abi_raw != unverified_abi:
                stash_abi(entry, slug, addr)
    except Exception:
        pass

    eip1967_impl = None
    try:
        slot_value = w3.eth.get_storage_at(addr, int(impl_slot, 16))
        if slot_value and int.from_bytes(slot_value, "big") != 0:
            impl_addr = Web3.to_checksum_address("0x" + slot_value[-20:].hex())
            if impl_addr != null_addr:
                eip1967_impl = impl_addr
    except Exception:
        pass

    is_proxy = etherscan_proxy or eip1967_impl is not None
    implementation = eip1967_impl or etherscan_impl

    result = {
        "address": addr,
        "type": "contract",
        "name": name,
        "proxy": is_proxy,
        "implementation": implementation,
        "verified": verified,
        "delegated": False,
        "delegated_to": None,
    }
    identify_cache[cache_key] = result
    return result




def read_contract_impl(
    address: str,
    function: str,
    *args,
    chain: str = "eth",
    block: int | str = "latest",
    abi: list | None = None,
    normalize_chain,
    get_w3,
    get_abi,
    identify,
):
    """Call a view function on a contract at any block using cached ABI."""

    slug = normalize_chain(chain)
    w3 = get_w3(slug)
    addr = Web3.to_checksum_address(address)

    resolved_abi = abi
    if resolved_abi is None:
        direct_abi = get_abi(addr, slug)
        if _abi_has_function(direct_abi, function):
            resolved_abi = direct_abi
        else:
            info = identify(addr, slug)
            impl = info.get("implementation") if info.get("proxy") else None
            if impl:
                impl_abi = get_abi(impl, slug)
                if impl_abi:
                    resolved_abi = impl_abi
            if resolved_abi is None:
                resolved_abi = direct_abi
    if not resolved_abi:
        raise ValueError(f"No ABI available for {addr} — pass abi= explicitly")

    contract = w3.eth.contract(address=addr, abi=resolved_abi)
    fn = getattr(contract.functions, function)(*args)
    return fn.call(block_identifier=block)
