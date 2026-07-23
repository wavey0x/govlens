# GovLens

GovLens is a read-only governance proposal watcher. It discovers finalized
proposals, preserves every action byte, runs deterministic protocol checks,
asks Codex for a focused audit, and alerts Telegram when risk is `MEDIUM` or
higher.

Supported sources:

- Curve ownership proposals
- Curve parameter proposals
- Resupply proposals

GovLens never votes, queues, executes, signs, or broadcasts transactions. It
contains no signer or private key.

## Design

The production system is deliberately small:

- one Python application;
- one SQLite database;
- one scheduled command;
- explicit protocol readers and deterministic checks;
- one isolated, protocol-neutral audit flow;
- Gist and Telegram as the only external writes.

Proposal actions are read at finalized Ethereum state and checked against their
creation block. Ambiguous external writes are never retried automatically.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Run a proposal audit without external writes:

```bash
uv run --env-file .env govlens test --protocol curve --source ownership --proposal 1452
uv run --env-file .env govlens test --protocol resupply --proposal 23
```

## Documentation

- [SPEC.md](SPEC.md) — behavior and architecture
