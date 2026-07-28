#!/usr/bin/env python3
"""
combine.py
----------
Third stage of the pipeline. Reads the raw data published by the two
upstream source repos, merges it with the curated FIR metadata in this
repo's data/fir_base.json, and asks Claude to clean the result up (tighten
wording, deduplicate near-identical cross-check text between sources) before
writing the final data/data.json that the Test_2_CZIB dashboard consumes.

Upstream repos are read over plain HTTPS via raw.githubusercontent.com — no
auth token needed, since czib-fetch-easa and czib-fetch-opsgroup are public.

  RAW_EASA_URL      <- czib-fetch-easa:      data/raw_easa.json
  RAW_OPSGROUP_URL  <- czib-fetch-opsgroup:  data/raw_opsgroup.json

Matching logic (same idea as a single-repo build script would use):
  - EASA CZIBs are matched to a FIR by simple country-name containment
    against each CZIB's title (e.g. "Airspace of Jordan" -> country "Jordan").
  - OpsGroup notes are matched by country name.

Anything that fails to match is left with its previous value (if data.json
already exists from a prior run) rather than silently guessing.

The Claude step (scripts/combine.py's "AI clean-up" pass) is optional: if
ANTHROPIC_API_KEY isn't set, this script still produces a complete, correct
data.json from the programmatic merge alone — just with more literal/raw
text and any near-duplicate cross-check entries left unmerged.
"""

import json
import os
import sys

import requests

RAW_EASA_URL = "https://raw.githubusercontent.com/portiz7/czib-fetch-easa/main/data/raw_easa.json"
RAW_OPSGROUP_URL = "https://raw.githubusercontent.com/portiz7/czib-fetch-opsgroup/main/data/raw_opsgroup.json"

BASE_PATH = "data/fir_base.json"
OUT_PATH = "data/data.json"

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-sonnet-4-6"
TIMEOUT = 20


def log(msg):
    print(f"[combine] {msg}", file=sys.stderr)


def load_local(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch_remote_json(url, label):
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"Failed to fetch {label} from {url}: {e}")
        return {}


def find_easa_bulletin(country, czibs):
    for c in czibs:
        title = (c.get("title") or "").lower()
        if country.lower() in title:
            return c
    return None


def find_opsgroup_note(country, notes):
    for heading, text in notes.items():
        if country.lower() in heading.lower():
            return text
    return None


def build_tier(fir):
    if fir.get("isInformationNote"):
        return "in"
    if fir.get("forcePartial"):
        return "partial"
    return "full"


def build_restriction_label(fir):
    if fir.get("restrictionOverride"):
        return fir["restrictionOverride"]
    if fir.get("isInformationNote"):
        floor = fir.get("altitudeFloor")
        base = "Caution only — factor into your own risk assessment and routing"
        return f"{base}, below {floor}" if floor else base
    if fir.get("altitudeFloor"):
        return f"Do not operate — below {fir['altitudeFloor']}"
    return "Do not operate — all levels"


def merge(base, easa_raw, opsgroup_raw, previous):
    czibs = easa_raw.get("czibs", [])
    ops_notes = opsgroup_raw.get("country_notes", {})

    out_firs = {}
    for code, fir in base["firs"].items():
        country = fir["country"]
        prev_entry = previous.get("firs", {}).get(code, {})

        bulletin_match = find_easa_bulletin(country, czibs)
        ops_note = find_opsgroup_note(country, ops_notes)

        cross = []
        if ops_note:
            cross.append({"src": "OpsGroup (latest fetch)", "detail": ops_note})
        # Preserve any previously-curated cross-check entries that this run
        # didn't refresh (e.g. hand-added FAA/AIC notes).
        for old in prev_entry.get("cross", []):
            if old.get("src") not in [c["src"] for c in cross] and old.get("src") != "OpsGroup (latest fetch)":
                cross.append(old)

        entry = {
            "name": fir["name"],
            "country": country,
            "region": fir["region"],
            "conflictType": fir["conflictType"],
            "narrative": fir.get("narrative", ""),
            "tags": fir["tags"],
            "coords": fir["coords"],
            "center": fir["center"],
            "tier": build_tier(fir),
            "restrictionLabel": build_restriction_label(fir),
            "cross": cross,
        }
        if fir.get("ext"):
            entry["ext"] = fir["ext"]

        if bulletin_match:
            entry["bulletin"] = bulletin_match.get("czib_number") or prev_entry.get("bulletin", "Unknown")
            entry["issued"] = bulletin_match.get("issue_date") or prev_entry.get("issued", "")
            entry["revised"] = bulletin_match.get("revision_date") or prev_entry.get("revised", "")
            entry["validUntil"] = bulletin_match.get("valid_until") or prev_entry.get("validUntil", "")
        else:
            entry["bulletin"] = prev_entry.get("bulletin", "Not matched this run — see fir_base.json")
            entry["issued"] = prev_entry.get("issued", "")
            entry["revised"] = prev_entry.get("revised", "")
            entry["validUntil"] = prev_entry.get("validUntil", "")

        out_firs[code] = entry

    return out_firs


def ai_cleanup(firs):
    """
    Optional pass: asks Claude to tighten wording and deduplicate
    near-identical cross-check text (e.g. an OpsGroup note that just
    restates the EASA narrative in different words). Returns the possibly
    revised firs dict, or the original unchanged if anything goes wrong.
    """
    if not API_KEY:
        log("No ANTHROPIC_API_KEY set - skipping AI clean-up pass, using raw merge as-is.")
        return firs

    try:
        import anthropic
    except ImportError:
        log("The 'anthropic' package isn't installed (see requirements.txt). Skipping.")
        return firs

    system_prompt = (
        "You are cleaning up a JSON object of airspace conflict-zone entries for a dashboard. "
        "Each key is a FIR code; each value has fields including 'conflictType', 'narrative', "
        "'restrictionLabel', and a 'cross' list of {src, detail} cross-check entries from other "
        "sources (e.g. OpsGroup). For each entry: "
        "1) You may lightly rewrite 'conflictType' and 'restrictionLabel' for clarity. "
        "2) You may tighten the wording of 'cross' entries whose src is 'OpsGroup (latest fetch)'. "
        "3) If a 'cross' entry substantially duplicates information already in 'narrative' (same "
        "facts, just reworded), merge the new/non-redundant detail into 'narrative' and remove or "
        "shorten the duplicate 'cross' entry rather than keeping both in full. "
        "Do NOT change any dates, FIR codes, country names, bulletin IDs, coordinates, or the 'src' "
        "field of any cross-check entry. Do NOT add or remove FIR keys. Do NOT invent facts not "
        "already present in the input. Return ONLY the complete, valid JSON object with the same "
        "top-level structure (FIR code -> entry) - no markdown fences, no commentary."
    )

    client = anthropic.Anthropic(api_key=API_KEY)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=system_prompt,
            messages=[{"role": "user", "content": json.dumps(firs, ensure_ascii=False)}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        revised = json.loads(cleaned)
        if not isinstance(revised, dict) or set(revised.keys()) != set(firs.keys()):
            log("AI clean-up output didn't match expected FIR keys - keeping raw merge instead.")
            return firs
        log("AI clean-up pass applied.")
        return revised
    except Exception as e:
        log(f"AI clean-up failed, keeping raw merge instead: {e}")
        return firs


def main():
    base = load_local(BASE_PATH)
    previous = load_local(OUT_PATH, default={"firs": {}})

    easa_raw = fetch_remote_json(RAW_EASA_URL, "czib-fetch-easa")
    opsgroup_raw = fetch_remote_json(RAW_OPSGROUP_URL, "czib-fetch-opsgroup")

    merged_firs = merge(base, easa_raw, opsgroup_raw, previous)
    final_firs = ai_cleanup(merged_firs)

    result = {
        "generated_at": easa_raw.get("fetched_at") or opsgroup_raw.get("fetched_at") or "unknown",
        "sources": {
            "easa": {"repo": "portiz7/czib-fetch-easa", "fetched_at": easa_raw.get("fetched_at", "unknown")},
            "opsgroup": {"repo": "portiz7/czib-fetch-opsgroup", "fetched_at": opsgroup_raw.get("fetched_at", "unknown")},
        },
        "firs": final_firs,
    }

    os.makedirs("data", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    log(f"Wrote {OUT_PATH} with {len(final_firs)} zones")


if __name__ == "__main__":
    main()
