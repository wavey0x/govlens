"""Trace and decoded-trace helpers."""

from __future__ import annotations

from collections import defaultdict

from web3 import Web3


def _hex_text(value) -> str:
    """Normalize trace payload values to hex strings."""

    if isinstance(value, bytes):
        return "0x" + value.hex()
    return str(value or "")


def _debug_trace_request_impl(tx_hash: str, chain: str, tracer: str, config: dict, *, get_w3, normalize_chain) -> dict:
    """Execute debug_traceTransaction with consistent error handling."""

    w3 = get_w3(normalize_chain(chain))
    try:
        result = w3.provider.make_request(
            "debug_traceTransaction",
            [tx_hash, {"tracer": tracer, "tracerConfig": config}],
        )
        if "error" in result:
            return {"error": result["error"].get("message", str(result["error"]))}
        return result.get("result", result)
    except Exception as exc:
        return {"error": f"debug_traceTransaction failed: {exc}"}


def _flatten_trace(node: dict, depth: int = 0) -> list[dict]:
    """Recursively flatten a nested callTracer dict into a list of calls."""

    raw_value = node.get("value", "0x0")
    try:
        value = str(int(raw_value, 16)) if isinstance(raw_value, str) else str(raw_value)
    except (ValueError, TypeError):
        value = "0"

    raw_input = _hex_text(node.get("input", ""))
    selector = raw_input[:10] if len(raw_input) >= 10 else ""

    raw_output = _hex_text(node.get("output", ""))
    output_size = max(0, len(raw_output) // 2 - 1) if raw_output else 0

    flat = [{
        "type": node.get("type", ""),
        "from": node.get("from", ""),
        "to": node.get("to", ""),
        "value": value,
        "selector": selector,
        "input": raw_input,
        "output": raw_output,
        "gas_used": node.get("gasUsed", ""),
        "output_size": output_size,
        "error": node.get("error", ""),
        "depth": depth,
    }]
    for child in node.get("calls", []):
        flat.extend(_flatten_trace(child, depth + 1))
    return flat


def trace_tx_impl(tx_hash: str, chain: str = "eth", *, debug_trace_request) -> dict:
    """Fetch a debug_traceTransaction call tree for the given tx."""

    return debug_trace_request(tx_hash, chain, "callTracer", {"onlyTopCall": False})


def state_diff_impl(tx_hash: str, chain: str = "eth", *, debug_trace_request) -> dict:
    """Fetch pre/post state diff via prestateTracer."""

    return debug_trace_request(tx_hash, chain, "prestateTracer", {"diffMode": True})


def decode_trace_impl(
    tx_hash: str,
    chain: str = "eth",
    *,
    trace: dict | None = None,
    receipt=None,
    resolve_selector: bool = False,
    normalize_chain,
    get_w3,
    trace_tx,
    get_abi,
    lookup_selector,
    serialize_param,
    transfer_topic: str,
    eth_abi_decode,
) -> list[dict]:
    """Decode a nested call trace into a flat list with function names and params."""

    slug = normalize_chain(chain)
    w3 = get_w3(slug)
    abi_cache: dict[str, list | None] = {}

    if trace is None:
        trace = trace_tx(tx_hash, slug)
    if "error" in trace:
        return [{"error": trace["error"]}]

    if receipt is None:
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            receipt = {"logs": []}

    flat: list[dict] = []

    def _abi_for(address: str) -> list | None:
        cache_key = address.lower()
        if cache_key not in abi_cache:
            abi_cache[cache_key] = get_abi(address, slug)
        return abi_cache[cache_key]

    def _walk(node: dict, path: str) -> None:
        to_addr = node.get("to", "")
        call_type = node.get("type", "")
        raw_input = _hex_text(node.get("input", ""))
        selector = raw_input[:10] if len(raw_input) >= 10 else ""
        raw_output = _hex_text(node.get("output", ""))

        fn_name = None
        params = None
        decoded_output = None

        if to_addr and selector:
            abi = _abi_for(to_addr)
            if abi:
                try:
                    contract = w3.eth.contract(address=Web3.to_checksum_address(to_addr), abi=abi)
                    inp = bytes.fromhex(raw_input[2:]) if raw_input.startswith("0x") else bytes.fromhex(raw_input)
                    fn, decoded_params = contract.decode_function_input(inp)
                    fn_name = fn.fn_name
                    params = {k: serialize_param(v) for k, v in decoded_params.items()}

                    if raw_output and raw_output != "0x" and not node.get("error"):
                        try:
                            for abi_item in abi:
                                if abi_item.get("type") == "function" and abi_item.get("name") == fn_name:
                                    outputs = abi_item.get("outputs", [])
                                    if outputs and eth_abi_decode:
                                        out_bytes = (
                                            bytes.fromhex(raw_output[2:])
                                            if raw_output.startswith("0x")
                                            else bytes.fromhex(raw_output)
                                        )
                                        out_types = [o["type"] for o in outputs]
                                        decoded_vals = eth_abi_decode(out_types, out_bytes)
                                        decoded_output = {}
                                        for i, value in enumerate(decoded_vals):
                                            name = outputs[i].get("name") or f"_{i}"
                                            decoded_output[name] = serialize_param(value)
                                    break
                        except Exception:
                            pass
                except Exception:
                    pass

            if resolve_selector and fn_name is None:
                sigs = lookup_selector(selector)
                if sigs:
                    fn_name = sigs[0].split("(")[0] if "(" in sigs[0] else sigs[0]

        flat.append({
            "path": path,
            "depth": path.count("."),
            "type": call_type,
            "from": node.get("from", ""),
            "to": to_addr,
            "value": node.get("value"),
            "function": fn_name,
            "params": params,
            "selector": selector,
            "input": raw_input,
            "gas_used": node.get("gasUsed", ""),
            "raw_output": raw_output,
            "output": decoded_output,
            "error": node.get("error") or None,
            "events": [],
        })

        for i, child in enumerate(node.get("calls", [])):
            _walk(child, f"{path}.{i}")

    _walk(trace, "0")

    addr_calls: dict[str, list[int]] = defaultdict(list)
    for i, call in enumerate(flat):
        if call["to"]:
            addr_calls[call["to"].lower()].append(i)

    for log in receipt.get("logs", []):
        log_addr = str(log.get("address", "")).lower()
        candidates = addr_calls.get(log_addr)
        if not candidates:
            continue
        target = candidates[-1]

        topics = [("0x" + t.hex()) if isinstance(t, bytes) else str(t) for t in log.get("topics", [])]
        raw_data = log.get("data", b"")
        data = ("0x" + raw_data.hex()) if isinstance(raw_data, bytes) else str(raw_data)

        event = {"address": log_addr, "topics": topics, "data": data, "name": None, "params": None}

        call_to = flat[target]["to"]
        if call_to:
            call_abi = _abi_for(call_to)
            if call_abi:
                try:
                    contract = w3.eth.contract(address=Web3.to_checksum_address(call_to), abi=call_abi)
                    for ev_name in contract.events:
                        try:
                            processed = getattr(contract.events, ev_name).process_log(log)
                            event["name"] = processed["event"]
                            event["params"] = {
                                k: serialize_param(v)
                                for k, v in dict(processed["args"]).items()
                            }
                            break
                        except Exception:
                            continue
                except Exception:
                    pass

        if event["name"] is None and topics and topics[0] == transfer_topic and len(topics) == 3:
            try:
                sender = Web3.to_checksum_address("0x" + topics[1][-40:])
                receiver = Web3.to_checksum_address("0x" + topics[2][-40:])
                raw_amount = int(data, 16) if data else 0
                event["name"] = "Transfer"
                event["params"] = {"from": sender, "to": receiver, "value": str(raw_amount)}
            except Exception:
                pass

        flat[target]["events"].append(event)

    return flat
