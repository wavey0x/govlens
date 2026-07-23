"""The small amount of configuration the product actually needs."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured")
    return value


_CHAT_ID_NAME = re.compile(r"[A-Z][A-Z0-9_]*_CHAT_ID")
_CHAT_ID = re.compile(r"-?[0-9]+")


def _telegram_chat_id(selector: str) -> str:
    chat_id_name = _required(selector)
    if not _CHAT_ID_NAME.fullmatch(chat_id_name):
        raise RuntimeError(f"{selector} must name a *_CHAT_ID environment variable")
    chat_id = _required(chat_id_name)
    if not _CHAT_ID.fullmatch(chat_id):
        raise RuntimeError(f"{chat_id_name} must be a numeric Telegram chat ID")
    return chat_id


@dataclass(frozen=True)
class Settings:
    rpc_url: str
    archive_rpc_url: str
    etherscan_key: str
    gist_key: str
    telegram_token: str
    telegram_targets: dict[str, str]
    database: Path
    codex: Path

    @classmethod
    def load(cls, *, delivery: bool = True) -> Settings:
        rpc_url = _required("ETHEREUM_RPC_URL")
        archive_rpc_url = os.environ.get("ETHEREUM_ARCHIVE_RPC_URL", "").strip() or rpc_url
        codex_value = os.environ.get("GOVLENS_CODEX_BINARY", "").strip()
        codex = Path(codex_value or shutil.which("codex") or "")
        database_value = os.environ.get("GOVLENS_DB", "").strip()
        telegram_targets = (
            {
                "curve": _telegram_chat_id("CURVE_PROPOSALS_CHAT"),
                "resupply": _telegram_chat_id("RESUPPLY_PROPOSALS_CHAT"),
            }
            if delivery
            else {}
        )
        return cls(
            rpc_url=rpc_url,
            archive_rpc_url=archive_rpc_url,
            etherscan_key=os.environ.get("ETHERSCAN_API_KEY", "").strip(),
            gist_key=_required("WAVEY_GIST_API_KEY") if delivery else "",
            telegram_token=_required("TELEGRAM_BOT_TOKEN") if delivery else "",
            telegram_targets=telegram_targets,
            database=Path(
                database_value or Path.home() / ".local/state/govlens/state.db"
            ).expanduser(),
            codex=codex,
        )
