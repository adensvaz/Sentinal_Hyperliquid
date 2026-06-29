#!/usr/bin/env bash
# One-shot GitHub "About" + release setup via the gh CLI.
# Prereq (one time, your login — cannot be automated):  gh auth login
# Usage:  bash scripts/setup_repo_meta.sh [owner/repo]   (defaults to the public showcase repo)
set -euo pipefail

GH="${GH:-$HOME/.local/bin/gh}"; command -v gh >/dev/null 2>&1 && GH=gh
REPO="${1:-adensvaz/Sentinal_Hyperliquid}"

DESC="Three uncorrelated algorithmic crypto-futures strategies on KoinBay — market-neutral momentum, regime-gated momentum, and funding-carry — with live paper-trading dashboards."
HOMEPAGE="http://136.113.89.123:8787"
TOPICS="algorithmic-trading,trading-bot,quantitative-finance,cryptocurrency,crypto-futures,market-neutral,momentum-strategy,funding-rate,backtesting,python,quant,perpetual-futures,copy-trading,koinbay"

if ! "$GH" auth status >/dev/null 2>&1; then
  echo "!! Not authenticated. Run:  $GH auth login   (then re-run this script)"; exit 1
fi

echo "→ About (description + homepage + topics) for $REPO"
"$GH" repo edit "$REPO" --description "$DESC" --homepage "$HOMEPAGE"
"$GH" repo edit "$REPO" --add-topic "$TOPICS"

echo "→ Release v1.0.0"
if "$GH" release view v1.0.0 --repo "$REPO" >/dev/null 2>&1; then
  echo "  v1.0.0 already exists — skipping."
else
  "$GH" release create v1.0.0 --repo "$REPO" --title "v1.0.0 — Three-strategy launch" --notes \
"Three uncorrelated strategies live in paper on KoinBay:
- Market-Neutral momentum (long/short, dollar-neutral)
- Momentum + Regime / Champion (long-only, BTC-regime gated)
- Funding Carry (collects perpetual funding, market-neutral)

135 tests · CI · walk-forward-validated backtests · live paper-trading dashboards."
fi
echo "✓ Done: https://github.com/$REPO"
