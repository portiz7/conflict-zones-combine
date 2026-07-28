# conflict-zones-combine

Third repo in a small pipeline that feeds the
[Test_2_CZIB](https://github.com/portiz7/test_2_czib) conflict-zone dashboard.

```
czib-fetch-easa                     ─┐
czib-fetch-opsgroup                  ├─▶ conflict-zones-combine ─▶ Test_2_CZIB (dashboard)
                                     ─┘        (this repo)
```

## What this repo does

Every 6 hours, 25 minutes after the two source repos' cron (and on manual
`workflow_dispatch`), `scripts/combine.py`:

1. Reads `data/raw_easa.json` from
   [czib-fetch-easa](https://github.com/portiz7/czib-fetch-easa) and
   `data/raw_opsgroup.json` from
   [czib-fetch-opsgroup](https://github.com/portiz7/czib-fetch-opsgroup), both over
   plain `https://raw.githubusercontent.com/...` — no auth token needed, since those
   repos are public.
2. Merges them with the curated, slow-changing FIR metadata in this repo's
   `data/fir_base.json` (geography, conflict classification, tags — edited by hand
   when a new zone appears).
3. Sends the merged result to Claude, which tightens wording and deduplicates
   near-identical cross-check text between sources (e.g. an OpsGroup note that just
   restates the EASA narrative), without inventing facts, changing dates, or touching
   FIR codes / coordinates / country names.
4. Writes the final `data/data.json` — the exact file the Test_2_CZIB dashboard
   fetches at runtime — and commits it if it changed.

The AI clean-up step is optional but expected to be configured: without an
`ANTHROPIC_API_KEY` secret, this script still produces a complete, correct
`data/data.json` from the programmatic merge alone — just with more literal/raw text
and any near-duplicate cross-check entries left unmerged instead of folded together.

## Setup

In this repo's **Settings → Secrets and variables → Actions**, add a repository secret:

- `ANTHROPIC_API_KEY` — your Claude API key, used only for the clean-up/dedupe pass.

## Local run

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...   # optional — omit to skip the AI clean-up pass
python scripts/combine.py
```

## Editing curated FIR metadata

To add a new zone or change how an existing one is classified (region, conflict type,
tags, coordinates), edit `data/fir_base.json` directly and commit. This file is not
touched by the fetch pipeline — only dates, bulletin numbers and cross-check text get
refreshed automatically.
