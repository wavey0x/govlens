"""Transaction decode and transfer summarization helpers."""

from __future__ import annotations

from decimal import Decimal

from web3 import Web3


def _serialize_param(v):
    """Convert web3 decoded values to JSON-safe types."""
    if isinstance(v, bytes):
        return "0x" + v.hex()
    if isinstance(v, dict):
        return {k: _serialize_param(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_serialize_param(i) for i in v]
    return v


def _serialize_authorization_list(entries) -> list[dict]:
    """Normalize an EIP-7702 authorization list to plain JSON-safe dicts."""

    if not entries:
        return []

    normalized = []
    for entry in entries:
        if hasattr(entry, "items"):
            item = dict(entry.items())
        elif isinstance(entry, dict):
            item = dict(entry)
        else:
            continue
        normalized.append({key: _serialize_param(value) for key, value in item.items()})
    return normalized


def decode_tx_impl(
    tx_hash: str,
    chain: str = "eth",
    abi: list | None = None,
    *,
    _tx=None,
    _receipt=None,
    normalize_chain,
    get_w3,
    get_abi,
    transfer_topic: str,
) -> dict:
    """Decode a transaction's function call and ERC-20 transfers."""

    slug = normalize_chain(chain)
    w3 = get_w3(slug)

    try:
        tx = _tx or w3.eth.get_transaction(tx_hash)
        receipt = _receipt or w3.eth.get_transaction_receipt(tx_hash)
    except Exception as exc:
        return {"error": f"Failed to fetch transaction: {exc}"}

    to_addr = tx.get("to")
    to_addr_cs = Web3.to_checksum_address(to_addr) if to_addr else None
    from_addr = str(tx.get("from", ""))

    function_info = None
    if to_addr_cs and tx.get("input") and len(tx["input"]) >= 4:
        call_abi = abi or get_abi(to_addr_cs, slug)
        if call_abi:
            try:
                contract = w3.eth.contract(address=to_addr_cs, abi=call_abi)
                fn, params = contract.decode_function_input(tx["input"])
                function_info = {
                    "name": fn.fn_name,
                    "params": {k: _serialize_param(v) for k, v in params.items()},
                }
            except Exception:
                pass

    decoded_logs = []
    transfers = []

    for log in receipt.get("logs", []):
        topics = [("0x" + t.hex()) if isinstance(t, bytes) else str(t) for t in log.get("topics", [])]
        raw_data = log.get("data", b"")
        data = ("0x" + raw_data.hex()) if isinstance(raw_data, bytes) else str(raw_data)
        log_addr = str(log.get("address", ""))

        entry = {
            "address": log_addr,
            "name": None,
            "params": None,
            "topics": topics,
            "data": data,
        }

        if topics and topics[0] == transfer_topic and len(topics) == 3:
            try:
                sender = Web3.to_checksum_address("0x" + topics[1][-40:])
                receiver = Web3.to_checksum_address("0x" + topics[2][-40:])
                raw_amount = int(data, 16) if data else 0
                entry["name"] = "Transfer"
                entry["params"] = {"from": sender, "to": receiver, "value": str(raw_amount)}
                transfers.append({
                    "token": log_addr,
                    "from": sender,
                    "to": receiver,
                    "amount": Decimal(raw_amount),
                })
            except Exception:
                pass

        if entry["name"] is None and abi and to_addr_cs and log_addr.lower() == to_addr.lower():
            try:
                contract = w3.eth.contract(address=Web3.to_checksum_address(log_addr), abi=abi)
                decoded = contract.events
                for event in decoded:
                    try:
                        processed = getattr(contract.events, event).process_log(log)
                        entry["name"] = processed["event"]
                        entry["params"] = {k: _serialize_param(v) for k, v in dict(processed["args"]).items()}
                        break
                    except Exception:
                        continue
            except Exception:
                pass

        decoded_logs.append(entry)

    return {
        "tx_hash": tx_hash,
        "block": receipt.get("blockNumber"),
        "from": from_addr,
        "to": str(to_addr) if to_addr else None,
        "tx_type": tx.get("type"),
        "authorization_list": _serialize_authorization_list(
            tx.get("authorizationList") or tx.get("authorization_list")
        ),
        "value_wei": str(tx.get("value", 0)),
        "function": function_info,
        "logs": decoded_logs,
        "transfers": transfers,
    }
