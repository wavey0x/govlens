"""Small read-only tool surface for governance proposal investigations."""

from .analysis import decode_trace
from .analysis import decode_tx
from .analysis import first_code_block
from .analysis import identify
from .analysis import lookup_selector
from .analysis import read_contract
from .analysis import search_contract_logs
from .analysis import state_diff
from .analysis import trace_tx
from .explorer import etherscan_get
from .network import chain_id
from .network import get_w3
from .network import preflight_check

__all__ = [
    "chain_id",
    "decode_trace",
    "decode_tx",
    "etherscan_get",
    "first_code_block",
    "get_w3",
    "identify",
    "lookup_selector",
    "preflight_check",
    "read_contract",
    "search_contract_logs",
    "state_diff",
    "trace_tx",
]
