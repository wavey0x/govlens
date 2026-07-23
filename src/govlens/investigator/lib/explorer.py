"""Etherscan V2 API client."""

import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

from .network import CHAINS, _normalize_chain, _UA

# ---------------------------------------------------------------------------
# Rate-limit state
# ---------------------------------------------------------------------------

_last_call_time = 0.0

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _http_get_with_retry(url: str, headers: dict | None = None, timeout: int = 30, max_retries: int = 3) -> bytes:
    """GET *url* with exponential backoff on transient failures.

    Retries on HTTP 429, 5xx, URLError, and TimeoutError.
    Does NOT retry other 4xx errors (they are permanent).
    Returns the raw response bytes.
    """
    headers = headers or {}
    delay = 1
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                last_exc = exc
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Etherscan V2
# ---------------------------------------------------------------------------


def etherscan_get(module: str, action: str, chain: str = "eth", **params) -> dict:
    """
    Make a GET request to Etherscan V2 API.

    Handles: URL construction, chain ID injection, API key injection,
    and rate limiting (3 calls/sec).

    Returns the parsed JSON response dict.
    Raises RuntimeError on API-level errors (status "0" with a message).

    Common module/action pairs:
        contract/getsourcecode    address=
        contract/getabi           address=
        contract/getcontractcreation  contractaddresses= (comma-separated)
        account/txlist            address=, startblock=, endblock=, sort=
        account/txlistinternal    address= or txhash=
        account/tokentx           address=, startblock=, endblock=
        account/tokennfttx        address=, startblock=, endblock=
        account/balance           address=, tag=latest
        account/balancemulti      address= (comma-separated), tag=latest
        logs/getLogs              address=, topic0=, fromBlock=, toBlock=
        proxy/eth_getTransactionByHash   txhash=
        proxy/eth_getTransactionReceipt  txhash=
        proxy/eth_getCode         address=, tag=latest
        proxy/eth_getStorageAt    address=, position= (hex slot), tag=latest
        proxy/eth_blockNumber     (no extra params)
    """
    slug = _normalize_chain(chain)
    cid = CHAINS[slug]["id"]

    base_url = os.environ.get("ETHERSCAN_BASE_URL", "https://api.etherscan.io/v2/api")
    api_key = os.environ.get("ETHERSCAN_KEY")
    if not api_key:
        raise EnvironmentError("Missing environment variable: ETHERSCAN_KEY")

    query = {
        **{k: str(v) for k, v in params.items()},
        "chainid": str(cid),
        "module": module,
        "action": action,
        "apikey": api_key,
    }
    url = f"{base_url}?{urlencode(query)}"

    # Rate limiting: 3 calls/sec → minimum 0.34s between calls
    global _last_call_time
    elapsed = time.time() - _last_call_time
    if elapsed < 0.34:
        time.sleep(0.34 - elapsed)

    headers = {"User-Agent": _UA}

    # Outer retry block handles Etherscan API-level rate limits
    max_api_retries = 3
    for api_attempt in range(max_api_retries):
        raw = _http_get_with_retry(url, headers=headers, timeout=30)
        _last_call_time = time.time()
        data = json.loads(raw.decode())

        if data.get("status") == "0" and data.get("message", "").upper() != "OK":
            result_str = str(data.get("result", ""))
            if "rate limit" in result_str.lower() and api_attempt < max_api_retries - 1:
                time.sleep(5)
                continue
            raise RuntimeError(f"Etherscan error: {data.get('message')} — {result_str}")

        return data

    return data  # unreachable, but satisfies type checkers
