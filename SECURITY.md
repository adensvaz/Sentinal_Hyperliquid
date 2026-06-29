# Security Policy

## Secrets & keys
- **Never commit credentials.** API keys live only in a local `.env` (git-ignored), never in `config.yaml` or code.
- The repo ships **no keys** and runs in **paper mode by default** — going live requires both `mode: live`
  in the config *and* an explicit `--live` CLI flag (double opt-in).
- The live dashboards are **read-only** displays of simulated paper accounts. They expose no keys and
  place no orders.

## Reporting a vulnerability
If you find a security issue (exposed secret, injection, auth bypass, etc.), please **do not open a public
issue**. Instead, report it privately via GitHub's *Security → Report a vulnerability* (private advisory)
on this repository. You'll get a response as soon as possible.

## Scope
- In scope: secret handling, the execution/reconciler path, the order-signing layer, the dashboard server.
- Out of scope: trading losses (this is software, not financial advice — see `LICENSE`), third-party
  exchange/API outages.
