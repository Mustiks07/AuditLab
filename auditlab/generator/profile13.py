"""Profile 1.3: varied defaults for unspecified fields and config-bound, optionally keyed randomness.

Profiles 1.0–1.2 derive every random stream from the integer seed alone. Their public IDs are the
first draws of ``sha256("AuditLab/v1/ids/<kind>/<seed>")``. Anyone holding the open source can
therefore enumerate a small seed space against a public ``invoice_id`` and then regenerate the
whole snapshot, truth included (docs/reviews/generator-hardening-v3/REPORT.md).

Profile 1.3 derives two opaque materials from the full generation input:

* without ``episode_key``: ``sha256(domain || canonical(seed, config))``. This is still a public,
  deterministic function. A small seed space with a guessable config stays enumerable, and the
  profile does not claim otherwise;
* with ``episode_key`` (32–128 lowercase hex characters, i.e. at least 128 bits chosen by the
  trusted side): ``HMAC-SHA256(key, canonical(seed, config without key))``. Without the key,
  public records do not identify the seed, the config or the answer. Brute force means guessing
  the key itself.

The same ``(seed, config)`` always gives the same bytes, so reproducibility across machines is
unchanged: the key is part of the hidden config, never a global or ambient secret. The materials
replace the seed only inside the generator's streams. The economic material excludes
``snapshot_nonce``, so a nonce renames and reorders an episode without changing its economics.
"""

from __future__ import annotations

import hmac
import json
import re
from hashlib import sha256

from .core import random_stream

PROFILE = "1.3"
KEY_FIELD = "episode_key"
KEY_PATTERN = re.compile(r"[0-9a-f]{32,128}")
# Zones without daylight saving in 2025–2026: whole and half-hour offsets on both sides of UTC.
ZONES = ("Asia/Almaty", "Asia/Tokyo", "Asia/Kolkata", "Europe/Moscow", "America/Bogota", "UTC")
# Foreign currencies with a plausible KZT quote table and exponents 2, 0 and 3.
FOREIGN = ("USD", "JPY", "KWD")
PERIODS = tuple(f"{year}-{month:02d}" for year in (2025, 2026) for month in range(1, 13))
ALWAYS_DISTRACTORS = ("split_payment", "equal_amount_different_supplier", "archive_copy")
OPTIONAL_DISTRACTORS = ("refund", "next_day_settlement")


def episode_key(config: dict) -> bytes | None:
    """Validate the optional trusted key; reject anything with less than 128 bits of hex."""
    if KEY_FIELD not in config:
        return None
    key = config[KEY_FIELD]
    if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key) or len(key) % 2:
        raise ValueError("invalid_episode_key: expected 32-128 lowercase hex characters (at least 128 bits)")
    return bytes.fromhex(key)


def _canonical(seed: int, config: dict) -> bytes:
    public_config = {key: value for key, value in config.items() if key != KEY_FIELD}
    return json.dumps({"profile": PROFILE, "seed": seed, "config": public_config},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _material(seed: int, config: dict, purpose: str) -> str:
    key = episode_key(config)
    payload = purpose.encode("ascii") + b"\0" + _canonical(seed, config)
    if key is None:
        return sha256(b"AuditLab/1.3/unkeyed\0" + payload).hexdigest()
    return hmac.new(key, b"AuditLab/1.3/keyed\0" + payload, sha256).hexdigest()


def materials(seed: int, config: dict) -> tuple[str, str]:
    """(economic material, identifier/order material) for the generator's random streams."""
    economic = {key: value for key, value in config.items() if key != "snapshot_nonce"}
    return _material(seed, economic, "economics"), _material(seed, config, "identifiers")


def randomness_record(config: dict) -> dict:
    """Trusted provenance note: how the streams were derived (no key material)."""
    keyed = KEY_FIELD in config
    return {"profile": PROFILE, "mode": "keyed" if keyed else "unkeyed",
            "derivation": "HMAC-SHA256(episode_key, seed+config)" if keyed else "SHA-256(seed+config)",
            "brute_force_resistance": ("key space of at least 2^128" if keyed else
                                       "none beyond the size of the seed and config space")}


def resolve(config: dict, economic_material: str) -> dict:
    """Fill only fields the caller left out. Explicit fields keep their meaning unchanged."""
    rng = random_stream(economic_material, "profile-1.3/defaults")
    resolved = {key: value for key, value in config.items() if key != KEY_FIELD}
    base = resolved.get("base_currency", "KZT")
    # Every default is drawn in a fixed order, used or not. The material already binds the whole
    # config, so any change of an explicit field gives a different, equally valid company.
    zone, period, foreign =rng.choice(ZONES), rng.choice(PERIODS), rng.choice(FOREIGN)
    tier = rng.random()
    if tier < 0.55:
        rows = rng.randrange(200, 601)
    elif tier < 0.85:
        rows = rng.randrange(600, 1_201)
    else:
        rows = rng.randrange(1_200, 2_001)
    documents = min(180, max(20, rows // rng.randrange(4, 11)))
    optional = [name for name in OPTIONAL_DISTRACTORS if rng.getrandbits(1)]
    resolved.setdefault("timezone", zone)
    resolved.setdefault("target_period", period)
    if "currencies" not in resolved and base == "KZT":
        resolved["currencies"] = ["KZT", foreign]
    resolved.setdefault("company_size", {"ledger_rows_target": rows, "documents_target": documents})
    resolved.setdefault("clean_distractors", [*ALWAYS_DISTRACTORS, *optional])
    return resolved


def lookalike_count(company) -> int:
    """Number of correctly settled multi-tranche payments that finish just before the boundary."""
    return random_stream(company._scenario_seed, "profile-1.3/lookalikes").randint(1, 2)


def lookalike_case(company, index: int):
    from .financial_cases import AllocationCase

    rng = random_stream(company._scenario_seed, f"profile-1.3/lookalike/{index}")
    scale = 10 ** company.exponents[company.base_currency]
    return AllocationCase(rng.randrange(5_000 * scale, 150_000 * scale), rng.randrange(1_700, 8_701),
                          rng.randint(1, 3), rng.randrange(0, 1_441))
