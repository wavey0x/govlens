"""Timer, dependency check, and one-off audit commands."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from web3 import Web3

from .audit import investigate
from .config import Settings
from .curve import Curve
from .gist import Gist
from .pipeline import ProposalSource, run_once
from .report import build_report, report_title
from .resupply import Resupply
from .store import Store
from .telegram import Telegram, build_message

LOG = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("govlens").setLevel(logging.INFO)
    for name in ("httpx", "httpcore", "urllib3", "web3"):
        logging.getLogger(name).setLevel(logging.WARNING)


@contextmanager
def _run_lock(database: Path) -> Iterator[bool]:
    database.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(f"{database}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="govlens")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="check once for new proposals")
    test = commands.add_parser("test", help="replay a proposal")
    test.add_argument("--protocol", choices=("resupply", "curve"), default="resupply")
    test.add_argument("--source", choices=("ownership", "parameter"))
    test.add_argument("--proposal", type=int, help="proposal number; defaults to latest")
    test.add_argument("--send", action="store_true", help="publish and send instead of previewing")
    commands.add_parser("check", help="check dependencies without publishing or sending")
    return parser


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True))


def _sources(settings: Settings) -> tuple[ProposalSource, ...]:
    return (
        Resupply(settings.rpc_url),
        Curve(settings.rpc_url, "ownership"),
        Curve(settings.rpc_url, "parameter"),
    )


def _close_sources(sources: Sequence[ProposalSource]) -> None:
    for source in sources:
        close = getattr(source, "close", None)
        if callable(close):
            close()


def _telegram(settings: Settings, protocol: str) -> Telegram:
    return Telegram(settings.telegram_token, settings.telegram_targets[protocol])


def _telegrams(settings: Settings) -> dict[str, Telegram]:
    return {protocol: _telegram(settings, protocol) for protocol in settings.telegram_targets}


def _close_telegrams(telegrams: dict[str, Telegram]) -> None:
    for telegram in telegrams.values():
        telegram.close()


def _sandbox_ready() -> bool:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return False
    try:
        completed = subprocess.run(  # noqa: S603
            [
                bwrap,
                "--unshare-user",
                "--uid",
                "0",
                "--gid",
                "0",
                "--ro-bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "/usr/bin/true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _run() -> int:
    settings = Settings.load()
    with _run_lock(settings.database) as acquired:
        if not acquired:
            LOG.info("event=run_skipped reason=already_running")
            _print({"status": "already_running"})
            return 0
        sources = _sources(settings)
        store = Store(settings.database)
        recovered = store.recover_interrupted()
        if recovered:
            LOG.warning("event=delivery_recovered proposals=%d", recovered)
        gist = Gist(settings.gist_key)
        telegrams = _telegrams(settings)
        try:
            result = run_once(
                store,
                sources,
                lambda proposal: investigate(settings, proposal),
                gist,
                telegrams,
            )
        finally:
            _close_telegrams(telegrams)
            gist.close()
            store.close()
            _close_sources(sources)
    _print(result)
    return 0 if result["status"] in {"ok", "initialized"} else 1


def _test(
    protocol_slug: str,
    source_name: str | None,
    proposal_id: int | None,
    send: bool,
) -> int:
    settings = Settings.load(delivery=send)
    if protocol_slug == "resupply":
        if source_name is not None:
            raise ValueError("--source applies only to Curve")
        source: ProposalSource = Resupply(settings.rpc_url)
    else:
        curve_source: Literal["ownership", "parameter"] = (
            "parameter" if source_name == "parameter" else "ownership"
        )
        source = Curve(settings.rpc_url, curve_source)
    try:
        block = source.finalized_block()
        selected = proposal_id if proposal_id is not None else source.count(block) - 1
        if selected < 0:
            raise RuntimeError(f"{protocol_slug} has no proposals")
        proposal = source.proposal(selected, block)
        analysis = investigate(settings, proposal)
        markdown = build_report(source.protocol, proposal, analysis)
        if not send:
            print(markdown)
            print("--- TELEGRAM PREVIEW — NO EXTERNAL WRITES ---")
            print(build_message(source.protocol, proposal, analysis, None))
            return 0

        telegram = _telegram(settings, protocol_slug)
        gist = Gist(settings.gist_key)
        try:
            if not telegram.verify_destination():
                raise RuntimeError("configured Telegram chat ID did not resolve to a group")
            published = gist.publish(report_title(source.protocol, proposal, analysis), markdown)
            message = build_message(
                source.protocol,
                proposal,
                analysis,
                published.url,
            )
            message_id = telegram.send(message)
        finally:
            gist.close()
            telegram.close()
        _print(
            {
                "sent": True,
                "protocol": proposal.protocol,
                "source": proposal.source,
                "proposal": selected,
                "gist_url": published.url,
                "message_id": message_id,
            }
        )
        return 0
    finally:
        _close_sources((source,))


def _check() -> int:
    settings = Settings.load()
    checks: dict[str, bool] = {
        "anvil": shutil.which("anvil") is not None,
        "cast": shutil.which("cast") is not None,
        "codex": settings.codex.is_file(),
        "gist": bool(settings.gist_key),
        "investigator_helpers": Path(__file__).with_name("investigator").joinpath("lib").is_dir(),
        "sandbox": _sandbox_ready(),
    }
    sources: tuple[ProposalSource, ...] = ()
    try:
        sources = _sources(settings)
        resupply, ownership, parameter = sources
        checks["ethereum"] = Web3(Web3.HTTPProvider(settings.rpc_url)).eth.chain_id == 1
        checks["archive_ethereum"] = (
            Web3(Web3.HTTPProvider(settings.archive_rpc_url)).eth.chain_id == 1
        )
        checks["resupply"] = resupply.count() >= 0
        checks["curve_ownership"] = ownership.count() >= 0
        checks["curve_parameter"] = parameter.count() >= 0
    except Exception:
        checks["ethereum"] = False
        checks["archive_ethereum"] = False
        checks["resupply"] = False
        checks["curve_ownership"] = False
        checks["curve_parameter"] = False
    finally:
        _close_sources(sources)
    try:
        checks["database"] = Store.check(settings.database)
    except Exception:
        checks["database"] = False
    for protocol in settings.telegram_targets:
        telegram: Telegram | None = None
        try:
            telegram = _telegram(settings, protocol)
            checks[f"telegram_{protocol}"] = telegram.verify_destination()
        except Exception:
            checks[f"telegram_{protocol}"] = False
        finally:
            if telegram is not None:
                telegram.close()
    _print({"ready": all(checks.values()), "checks": checks})
    return 0 if all(checks.values()) else 1


def main(argv: Sequence[str] | None = None) -> None:
    _configure_logging()
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            code = _run()
        elif args.command == "test":
            code = _test(args.protocol, args.source, args.proposal, args.send)
        else:
            code = _check()
    except Exception as exc:
        LOG.error("event=command_failed command=%s error=%s", args.command, type(exc).__name__)
        _print({"ok": False, "error": type(exc).__name__})
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main(sys.argv[1:])
