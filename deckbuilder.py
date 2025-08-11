from __future__ import annotations
import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import requests

SCRYFALL_COLLECTION_URL = "https://api.scryfall.com/cards/collection"
SCRYFALL_NAMED_URL = "https://api.scryfall.com/cards/named"  # ?exact=
SCRYFALL_SETS_URL = "https://api.scryfall.com/sets"
MAX_BATCH = 75  # Scryfall collection endpoint limit
CACHE_PATH = Path(".scryfall_cache.json")

ALLOWED_DUP_EXCEPTIONS = {"Relentless Rats", "Shadowborn Apostle"}

# --------- Data models ---------
@dataclass
class CsvRow:
    name: str
    set_input: Optional[str] = None
    quantity: int = 1
    card_number: Optional[str] = None
    scryfall_id: Optional[str] = None
    is_commander: bool = False  # may be filled via CLI

@dataclass
class Card:
    name: str
    set: Optional[str]
    collector_number: Optional[str]
    type_line: str
    color_identity: List[str]
    legalities: Dict[str, str]
    oracle_text: str
    keywords: List[str]
    is_basic_land: bool

    @classmethod
    def from_scryfall(cls, d: Dict) -> "Card":
        type_line = d.get("type_line", "")
        is_basic = "Basic Land" in type_line
        return cls(
            name=d.get("name", ""),
            set=d.get("set"),
            collector_number=d.get("collector_number"),
            type_line=type_line,
            color_identity=d.get("color_identity", []) or [],
            legalities=d.get("legalities", {}) or {},
            oracle_text=d.get("oracle_text", "") or "",
            keywords=d.get("keywords", []) or [],
            is_basic_land=is_basic,
        )

@dataclass
class DeckValidation:
    issues: List[str]
    warnings: List[str]
    deck_size: int
    commander_names: List[str]
    commander_color_id: List[str]

# --------- Simple on-disk cache to be kind to Scryfall ---------
class Cache:
    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}
        else:
            self.data = {}

    def get(self, key: str):
        return self.data.get(key)

    def set(self, key: str, value):
        self.data[key] = value

    def flush(self):
        try:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

# --------- CSV parsing ---------
TRUTHY = {"1", "true", "t", "yes", "y"}

REQ_COLS = ["card name", "set code / set name"]


def load_csv(path: Path) -> List[CsvRow]:
    rows: List[CsvRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV appears to have no header row.")
        headers = {h.lower().strip(): h for h in reader.fieldnames}
        for col in REQ_COLS:
            if col not in headers:
                raise ValueError(f"CSV must include a '{col}' column")

        # Optional columns
        h_name = headers["card name"]
        h_set = headers["set code / set name"]
        h_qty = headers.get("quantity")
        h_num = headers.get("card number")
        h_id = headers.get("scryfall id")

        for i, raw in enumerate(reader, start=2):
            name = (raw.get(h_name) or "").strip()
            if not name:
                continue
            set_input = (raw.get(h_set) or "").strip() or None
            qty = 1
            if h_qty:
                v = (raw.get(h_qty) or "1").strip()
                try:
                    qty = max(1, int(v))
                except Exception:
                    raise ValueError(f"Row {i}: invalid Quantity '{v}' for '{name}'")
            card_number = (raw.get(h_num) or "").strip() or None
            scryfall_id = (raw.get(h_id) or "").strip() or None
            rows.append(CsvRow(name=name, set_input=set_input, quantity=qty, card_number=card_number, scryfall_id=scryfall_id))
    if not rows:
        raise ValueError("No rows found in CSV.")
    return rows

# --------- Scryfall API helpers ---------

def _req_with_backoff(method: str, url: str, **kwargs):
    delay = 0.25
    while True:
        resp = requests.request(method, url, timeout=30, **kwargs)
        if resp.status_code in (429, 503):
            time.sleep(delay)
            delay = min(2.0, delay * 2)
            continue
        resp.raise_for_status()
        return resp


def get_all_sets(cache: Cache) -> List[Dict]:
    cached = cache.get("sets:list")
    if cached:
        return cached
    data = _req_with_backoff("GET", SCRYFALL_SETS_URL).json()
    sets = data.get("data", [])
    cache.set("sets:list", sets)
    cache.flush()
    return sets


def resolve_set_code(set_input: Optional[str], cache: Cache) -> Optional[str]:
    if not set_input:
        return None
    s = set_input.strip()
    # If it looks like a code (3–5 alnum, no spaces), use it
    if 2 < len(s) <= 5 and s.isalnum() and " " not in s:
        return s.lower()
    # Otherwise resolve by name
    sets = get_all_sets(cache)
    # exact name match first
    for st in sets:
        if st.get("name", "").lower() == s.lower():
            return st.get("code")
    # fallback: contains
    for st in sets:
        name = st.get("name", "").lower()
        if s.lower() in name:
            return st.get("code")
    return None


def _row_matches_card(row: CsvRow, card_obj: Dict, cache: Cache) -> bool:
    """Heuristic match between a requested row and a returned Scryfall card."""
    # ID exact
    if row.scryfall_id and card_obj.get("id") == row.scryfall_id:
        return True
    # set + number
    set_code = resolve_set_code(row.set_input, cache)
    if set_code and row.card_number:
        if card_obj.get("set") == set_code and str(card_obj.get("collector_number")) == str(row.card_number):
            return True
    # name (+ optional set)
    if card_obj.get("name", "").lower() == row.name.lower():
        if set_code:
            return card_obj.get("set") == set_code
        return True
    return False


def build_identifiers(rows: List[CsvRow], cache: Cache) -> List[Dict]:
    idents: List[Dict] = []
    for r in rows:
        # Priority: Scryfall ID -> (set, number) -> (name, set) -> (name)
        if r.scryfall_id:
            idents.append({"id": r.scryfall_id})
            continue
        set_code = resolve_set_code(r.set_input, cache)
        if set_code and r.card_number:
            idents.append({"set": set_code, "collector_number": r.card_number})
        elif set_code:
            idents.append({"name": r.name, "set": set_code})
        else:
            idents.append({"name": r.name})
    return idents


def fetch_cards(rows: List[CsvRow], cache: Cache) -> Dict[int, Card]:
    """Fetch card objects for CSV rows using the collection endpoint with rich identifiers.
    Returns a mapping of original row index -> Card
    """
    result: Dict[int, Card] = {}

    identifiers = build_identifiers(rows, cache)

    # Batch fetch
    for i in range(0, len(identifiers), MAX_BATCH):
        batch = identifiers[i : i + MAX_BATCH]
        resp = _req_with_backoff("POST", SCRYFALL_COLLECTION_URL, json={"identifiers": batch})
        data = resp.json()
        if "data" not in data:
            raise RuntimeError("Unexpected Scryfall response format for collection endpoint")

        # We cannot rely on a 1:1 positional mapping when some are not_found.
        # So we greedily match each returned card to the first unresolved row that fits.
        unresolved_indices = list(range(i, min(i + len(batch), len(rows))))
        for card_obj in data["data"]:
            for idx in unresolved_indices:
                if idx in result:
                    continue
                if _row_matches_card(rows[idx], card_obj, cache):
                    result[idx] = Card.from_scryfall(card_obj)
                    break
        # Optionally, you could inspect data.get("not_found", []) here.
        cache.flush()

    # Fallback pass for any missing using /named exact by name
    for idx, r in enumerate(rows):
        if idx not in result:
            try:
                r2 = _req_with_backoff("GET", SCRYFALL_NAMED_URL, params={"exact": r.name})
                result[idx] = Card.from_scryfall(r2.json())
                cache.set(f"card:row:{idx}", r2.json())
            except Exception:
                pass
    cache.flush()
    return result

# --------- Commander rules checks ---------

def detect_commanders(rows: List[CsvRow], cards_by_index: Dict[int, Card]) -> List[Card]:
    commanders: List[Card] = []
    for idx, r in enumerate(rows):
        if r.is_commander and (idx in cards_by_index):
            commanders.append(cards_by_index[idx])
    return commanders


def _has_partner(c: Card) -> bool:
    if any(k.lower().startswith("partner") for k in c.keywords):
        return True
    text = c.oracle_text.lower()
    return "partner" in text or "friends forever" in text


def _is_eligible_commander(c: Card) -> bool:
    t = c.type_line
    if "Legendary Creature" in t:
        return True
    if "can be your commander" in c.oracle_text.lower():
        return True
    return False


def validate_deck(rows: List[CsvRow], cards_by_index: Dict[int, Card]) -> DeckValidation:
    issues: List[str] = []
    warnings: List[str] = []

    # Resolve commanders
    commanders = detect_commanders(rows, cards_by_index)
    if not commanders:
        issues.append("No commander specified. Add --commander 'Name' or two names for partners.")
        commander_colors: List[str] = []
    elif len(commanders) == 1:
        if not _is_eligible_commander(commanders[0]):
            issues.append(f"Commander '{commanders[0].name}' may not be eligible (not a Legendary Creature or explicitly allowed).")
        commander_colors = sorted(set(commanders[0].color_identity))
    elif len(commanders) == 2:
        if not (_has_partner(commanders[0]) and _has_partner(commanders[1])):
            issues.append("Two commanders detected, but they don't both have Partner/Friends forever.")
        commander_colors = sorted(set(commanders[0].color_identity) | set(commanders[1].color_identity))
    else:
        issues.append("More than two commanders marked. EDH supports one (or two with Partner).")
        commander_colors = []

    # Size check (100 including commander(s))
    total_cards = sum(r.quantity for r in rows)
    if total_cards != 100:
        issues.append(f"Deck must contain exactly 100 total cards including commander(s); found {total_cards}.")

    # Name counts for singleton
    name_counts: Dict[str, int] = {}
    for r in rows:
        name_counts[r.name] = name_counts.get(r.name, 0) + r.quantity

    for idx, r in enumerate(rows):
        c = cards_by_index.get(idx)
        if not c:
            issues.append(f"Card could not be resolved on Scryfall: '{r.name}' (set '{r.set_input}' number '{r.card_number}')")
            continue
        # Commander legality
        legality = c.legalities.get("commander")
        if legality == "banned":
            issues.append(f"BANNED in Commander: {c.name}")
        elif legality not in {"legal", "restricted"}:  # restricted doesn't exist for EDH but keep flexible
            warnings.append(f"Not legal in Commander (status {legality}): {c.name}")

        # Singleton rule
        if not (c.is_basic_land or c.name in ALLOWED_DUP_EXCEPTIONS):
            if name_counts.get(c.name, 0) > 1:
                issues.append(f"Singleton violation: {c.name} appears {name_counts[c.name]} times.")

        # Color identity
        if commander_colors:
            ci = set(c.color_identity or [])
            if not ci.issubset(set(commander_colors)):
                issues.append(
                    f"Color identity mismatch: {c.name} has {sorted(ci)} not within commander identity {commander_colors}."
                )

    return DeckValidation(
        issues=issues,
        warnings=warnings,
        deck_size=total_cards,
        commander_names=[c.name for c in commanders],
        commander_color_id=commander_colors,
    )

# --------- Outputs ---------

def write_text_export(rows: List[CsvRow], cards_by_index: Dict[int, Card], dest: Path) -> None:
    """Writes a simple text decklist: `count name (SET) #number`.
    """
    # Put commanders first
    cmd_rows = [(i, r) for i, r in enumerate(rows) if r.is_commander]
    non_cmd_rows = [(i, r) for i, r in enumerate(rows) if not r.is_commander]

    def line_for(i: int, r: CsvRow) -> str:
        c = cards_by_index.get(i)
        if c and c.set and c.collector_number:
            return f"{r.quantity} {c.name} ({c.set.upper()}) {c.collector_number}"
        return f"{r.quantity} {r.name}"

    lines: List[str] = ["// Commander(s)"] + [line_for(i, r) for i, r in cmd_rows]
    lines += ["", "// Main"] + [line_for(i, r) for i, r in non_cmd_rows]

    dest.write_text("".join(lines) + "", encoding="utf-8")


def write_json_report(rows: List[CsvRow], cards_by_index: Dict[int, Card], validation: DeckValidation, dest: Path) -> None:
    payload = {
        "commander_names": validation.commander_names,
        "commander_color_identity": validation.commander_color_id,
        "deck_size": validation.deck_size,
        "issues": validation.issues,
        "warnings": validation.warnings,
        "cards": [
            {
                "name": r.name,
                "quantity": r.quantity,
                "is_commander": r.is_commander,
                "resolved": bool(cards_by_index.get(i)),
                "set": cards_by_index.get(i).set if cards_by_index.get(i) else None,
                "collector_number": cards_by_index.get(i).collector_number if cards_by_index.get(i) else None,
                "color_identity": cards_by_index.get(i).color_identity if cards_by_index.get(i) else None,
                "type_line": cards_by_index.get(i).type_line if cards_by_index.get(i) else None,
                "commander_legality": cards_by_index.get(i).legalities.get("commander") if cards_by_index.get(i) else None,
            }
            for i, r in enumerate(rows)
        ],
    }
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

# --------- CLI ---------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Commander (EDH) deck builder + validator using Scryfall API")
    p.add_argument("--csv", required=True, help="Path to CSV with required columns: Card name, Set code / Set name")
    p.add_argument("--out-prefix", default="deck_output", help="Output file prefix (writes .txt and .json)")
    p.add_argument("--no-cache", action="store_true", help="Disable on-disk Scryfall cache")
    p.add_argument("--commander", nargs="*", default=None, help="Commander name(s). Provide one or two names for partners.")
    args = p.parse_args(argv)

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 2

    try:
        rows = load_csv(csv_path)
    except Exception as e:
        print(f"Failed to parse CSV: {e}", file=sys.stderr)
        return 2

    # Mark commanders from CLI, if provided
    if args.commander:
        want = {n.lower() for n in args.commander}
        for r in rows:
            if r.name.lower() in want:
                r.is_commander = True

    # Use a real null device for platforms (Windows/Linux/Mac)
    null_path = Path(os.devnull)
    cache = Cache(CACHE_PATH) if not args.no_cache else Cache(null_path)

    # Fetch Scryfall data
    cards_by_index = fetch_cards(rows, cache)

    # Validate
    validation = validate_deck(rows, cards_by_index)

    # Outputs
    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    txt_path = out_prefix.with_suffix(".txt")
    json_path = out_prefix.with_suffix(".json")

    write_text_export(rows, cards_by_index, txt_path)
    write_json_report(rows, cards_by_index, validation, json_path)

    # Summary to console
    print("=== Commander Deck Builder Report ===")
    if validation.commander_names:
        print("Commander(s):", ", ".join(validation.commander_names))
        print("Color Identity:", validation.commander_color_id or [])
    print("Deck size:", validation.deck_size)

    if validation.issues:
        print("Issues (must fix):")
        for s in validation.issues:
            print(" -", s)
    else:
        print("No blocking issues detected.")

    if validation.warnings:
        print("Warnings:")
        for s in validation.warnings:
            print(" -", s)

    print(f"Wrote: {txt_path}")
    print(f"Wrote: {json_path}")

    return 0 if not validation.issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
