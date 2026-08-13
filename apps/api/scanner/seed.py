"""Load prebuilt scanner JSONs, validate, upsert. Runs at app startup."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from apps.api.scanner.schema import parse_definition
from apps.api.scanner.store import ScannerStore

logger = logging.getLogger(__name__)
PREBUILT_DIR = Path(__file__).parent / "prebuilt"


def seed_prebuilt(store: ScannerStore) -> int:
    count = 0
    for path in sorted(PREBUILT_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        parse_definition(data["definition"])  # a broken seed should fail startup loudly
        store.upsert_prebuilt(data["name"], data["description"], data["definition"])
        count += 1
    logger.info("seeded %d prebuilt scanners", count)
    return count
