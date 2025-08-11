# Commander Deck Builder — Starter (Scryfall API)

## What this does
- Reads a CSV of desired cards to assemble a Commander deck.
- Uses the Scryfall API to enrich each card with color identity, types, and legality.
- Validates basic Commander rules (size, singleton, color identity, commander legality, banned list).
- Writes a plain-text deck export and a JSON report with details and any validation issues.

## CSV format (headers are case-insensitive):
- Card name (required)
- Set code / Set name (required)
- Quantity
- Card number
- Scryfall ID

## Notes
- If **Scryfall ID** is supplied, it is used directly.
- Otherwise, the tool will try `{set code + card number}`, then `{name + set code}`, then just `{name}`.
- If you provide a **set name** instead of the 3–5 letter set code, the script resolves it to the correct code automatically.
- Quantity defaults to 1 when omitted.
- Commander(s) can be specified via the CLI (see `--commander`).

## Dependencies
```bash
pip install requests
```

## Quick start

```bash
python starter_commander_builder.py \

    --csv my_deck.csv \

    --out-prefix out/my_deck \

    --commander "Atraxa, Praetors' Voice"
```

Tested with Python 3.9+