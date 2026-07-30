#!/usr/bin/env python3
"""
combine.py
----------
Third stage of the pipeline. Reads the raw data published by the two
upstream source repos, merges it with the curated FIR metadata in this
repo's data/fir_base.json, and asks Claude to clean the result up (tighten
wording, deduplicate near-identical cross-check text across sources) before
writing the final data/data.json that the Test_2_CZIB dashboard consumes.

Upstream repos are read over plain HTTPS via raw.githubusercontent.com — no
auth token needed, since czib-fetch-easa and czib-fetch-opsgroup are public.

  RAW_EASA_URL      <- czib-fetch-easa:      data/raw_easa.json
                         {czibs: [...], information_notes: [...]}
  RAW_OPSGROUP_URL  <- czib-fetch-opsgroup:  data/raw_opsgroup.json
                         {opsgroup: {countries: {...}},
                          safeairspace: {countries: {...}}}
                         opsgroup.countries and safeairspace.countries share
                         one record shape: {country, risk_level_number,
                         risk_level_label, narrative, related_reading,
                         warnings} - unified upstream so this script doesn't
                         need two different matching functions for them.

Matching logic - exact country-name match (after normalization/alias), NOT
substring containment, across all three sources. Substring matching was
tried first for EASA and produced a real false positive: "mali" is a
literal substring of "somalia", silently matching Somalia's FIR to Mali's
bulletin. Every source here uses the same _norm_country() + exact-match
approach as a result.

EASA CZIBs take priority over EASA Information Notes for the bulletin/
dates fields (a FIR only has an Information Note match at all when it has
no CZIB - true for exactly the two FIRs in fir_base.json flagged
isInformationNote: Ethiopia and Myanmar). OpsGroup and safeairspace.net
both contribute cross-check entries regardless.

Anything that fails to match is left with its previous value (if data.json
already exists from a prior run) rather than silently guessing.

The Claude step (ai_cleanup()) is optional: if ANTHROPIC_API_KEY isn't set,
this script still produces a complete, correct data.json from the
programmatic merge alone - just with more literal/raw text and any
near-duplicate cross-check entries left unmerged.
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


# Country naming doesn't always match 1:1 across sources (we say "UAE",
# EASA/safeairspace say "United Arab Emirates") even though most other
# names differ only by extra wording, which exact-after-normalization
# matching doesn't need aliases for ("Russia" IS "Russia" on every source
# actually used here, confirmed against live data).
COUNTRY_ALIASES = {"uae": "united arab emirates"}


def _norm_country(name):
    n = (name or "").strip().lower()
    return COUNTRY_ALIASES.get(n, n)


def find_easa_item(country, items):
    """
    Matches a FIR's country against EASA's affected_countries list - used
    for both czibs and information_notes (same field shape on both).
    """
    target = _norm_country(country)
    if not target:
        return None
    for item in items:
        for easa_country in item.get("affected_countries", []):
            if _norm_country(easa_country) == target:
                return item
    return None


def find_country_record(country, countries):
    """
    One matching function for BOTH opsgroup.countries and
    safeairspace.countries now that czib-fetch-opsgroup normalizes both to
    the same {country, risk_level_number, risk_level_label, narrative,
    related_reading, warnings} shape - they used to be two different
    schemas (OpsGroup was a flat {heading: text} map with combined-country
    headings like "Armenia/Azerbaijan/Afghanistan"), which needed two
    separate matching functions with different logic. OpsGroup now splits
    combined headings into one clean record per country before this ever
    sees it, so plain exact-match (after normalization) works for both -
    no more substring matching, and no more special-casing the UAE alias
    per source (applying _norm_country() to both sides symmetrically
    is what was missing before: comparing an aliased target against an
    un-aliased dict key broke the match, not the alias itself).
    """
    target = _norm_country(country)
    if not target:
        return None
    for data in countries.values():
        if _norm_country(data.get("country", "")) == target:
            return data
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


def safeairspace_cross_label(safe_data):
    n = safe_data.get("risk_level_number")
    label = safe_data.get("risk_level_label")
    if n is not None and label and label != "None":
        return f"safeairspace.net (Risk Level {n} – {label})"
    return "safeairspace.net"


def merge(base, easa_raw, opsgroup_raw, previous):
    # czib-fetch-easa deliberately scrapes ALL CZIBs, Withdrawn included (by
    # explicit request, for historical completeness) - matching must filter
    # to Active only, or a country whose only *current* coverage is an
    # Information Note can silently match an old Withdrawn CZIB instead.
    # Confirmed: Ethiopia matched a 2022 Withdrawn Tigray-region CZIB this
    # way, hiding the real, current IN-ETHIOPIA Information Note match.
    czibs = [c for c in easa_raw.get("czibs", []) if c.get("status") == "Active"]
    information_notes = easa_raw.get("information_notes", [])
    ops_countries = opsgroup_raw.get("opsgroup", {}).get("countries", {})
    safe_countries = opsgroup_raw.get("safeairspace", {}).get("countries", {})

    out_firs = {}
    for code, fir in base["firs"].items():
        country = fir["country"]
        prev_entry = previous.get("firs", {}).get(code, {})

        czib_match = find_easa_item(country, czibs)
        # An Information Note is only relevant when there's no CZIB - a FIR
        # never has both, but this keeps the two from silently colliding if
        # that ever changes.
        in_match = find_easa_item(country, information_notes) if not czib_match else None
        easa_match = czib_match or in_match

        ops_record = find_country_record(country, ops_countries)
        safe_data = find_country_record(country, safe_countries)

        cross = []
        if ops_record and ops_record.get("narrative"):
            cross.append({"src": "OpsGroup (latest fetch)", "detail": ops_record["narrative"]})
        if safe_data and safe_data.get("narrative"):
            cross.append({"src": safeairspace_cross_label(safe_data), "detail": safe_data["narrative"]})
        # Preserve any previously-curated cross-check entries whose source
        # category wasn't refreshed this run (e.g. hand-added AIC notes).
        fresh_srcs = [c["src"] for c in cross]
        for old in prev_entry.get("cross", []):
            src = old.get("src", "")
            if src in fresh_srcs:
                continue
            if src == "OpsGroup (latest fetch)" or src.startswith("safeairspace.net"):
                continue  # stale from a prior run where this source didn't match - drop, not carry forward
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

        if czib_match:
            entry["bulletin"] = czib_match.get("czib_number") or prev_entry.get("bulletin", "Unknown")
            entry["issued"] = czib_match.get("issue_date") or prev_entry.get("issued", "")
            entry["revised"] = czib_match.get("revision_date") or prev_entry.get("revised", "")
            entry["validUntil"] = czib_match.get("valid_until") or prev_entry.get("validUntil", "")
        elif in_match:
            entry["bulletin"] = in_match.get("id") or prev_entry.get("bulletin", "Unknown")
            entry["issued"] = in_match.get("issue_date") or prev_entry.get("issued", "")
            entry["revised"] = prev_entry.get("revised", "")  # Information Notes expose no revision date
            entry["validUntil"] = in_match.get("valid_until") or prev_entry.get("validUntil", "")
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
    near-identical cross-check text (e.g. an OpsGroup or safeairspace.net
    note that just restates the EASA narrative in different words).
    Returns the possibly revised firs dict, or the original unchanged if
    anything goes wrong.
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
        "sources (src is one of 'OpsGroup (latest fetch)' or starts with 'safeairspace.net'). "
        "For each entry: "
        "1) You may lightly rewrite 'conflictType' and 'restrictionLabel' for clarity. "
        "2) You may tighten the wording of any 'cross' entry's 'detail' text. "
        "3) If a 'cross' entry substantially duplicates information already in 'narrative' (same "
        "facts, just reworded), merge the new/non-redundant detail into 'narrative' and shorten the "
        "duplicate 'cross' entry rather than keeping both in full. "
        "4) If two 'cross' entries (e.g. OpsGroup and safeairspace.net) substantially overlap with "
        "each other, you may shorten one to just its non-redundant detail - but keep both entries "
        "present with their original 'src', never delete one outright. "
        "Do NOT change any dates, FIR codes, country names, bulletin IDs, coordinates, or the 'src' "
        "field of any cross-check entry. Do NOT add or remove FIR keys or cross-check entries. Do "
        "NOT invent facts not already present in the input. Return ONLY the complete, valid JSON "
        "object with the same top-level structure (FIR code -> entry) - no markdown fences, no "
        "commentary."
    )

    client = anthropic.Anthropic(api_key=API_KEY)
    try:
        # Streaming, not messages.create(): confirmed in production that a
        # non-streaming call with this large a max_tokens is rejected
        # client-side by the SDK ("Streaming is required for operations
        # that may take longer than 10 minutes") - the earlier attempt to
        # just raise max_tokens (8000 -> 32000, to stop responses getting
        # cut off mid-string) tripped this guard immediately.
        with client.messages.stream(
            model=MODEL,
            max_tokens=32000,
            system=system_prompt,
            messages=[{"role": "user", "content": json.dumps(firs, ensure_ascii=False)}],
        ) as stream:
            final_message = stream.get_final_message()
        if final_message.stop_reason == "max_tokens":
            log("AI clean-up response was truncated (hit max_tokens) - keeping raw merge instead.")
            return firs
        text = "".join(block.text for block in final_message.content if block.type == "text")
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
