# Investigator helper provenance

The curated read-only helper modules under `lib/` were copied from
`yearn/web3-investigator` at commit
`b2228e6d0c2f53c5eebd822c318c36be19756365`.

Only the contract/source, RPC, log, trace, and state-diff dependencies used by
proposal audits are retained. Case workflow, evidence persistence, Chifra,
`gist_publish.py`, terminal scripts, and repository skills are deliberately not
vendored. GovLens keeps publication and Telegram delivery in its own reviewed
writers, outside the isolated Codex workspace.
