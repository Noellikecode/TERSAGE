"""San Francisco city adapter.

Everything municipality-specific lives behind :class:`~firstdue.ports.city.CityAdapter`.
Swapping to another city means writing another adapter, not editing the core.

Street addresses and coordinates here are real public reference data. Every
record attached to them -- EMS, Tier II, CAD, RMS, thermal, mutual aid,
referrals -- is synthetic.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from firstdue.errors import ConfigurationError
from firstdue.ports.city import NormalizedAddress

MUNICIPALITY_ID: Final[str] = "san-francisco-ca"
DEFAULT_JURISDICTION_ID: Final[str] = "sf-city-county"

_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[.,#]")

#: Street-type abbreviations SF data publishes inconsistently.
_ABBREVIATIONS: Final[dict[str, str]] = {
    "street": "st",
    "avenue": "ave",
    "boulevard": "blvd",
    "drive": "dr",
    "road": "rd",
    "place": "pl",
    "terrace": "ter",
    "court": "ct",
    "lane": "ln",
    "highway": "hwy",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
}


def _normalize_token(raw: str) -> str:
    """Fold an address to a comparable form: casing, punctuation, abbreviations."""
    text = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    text = _PUNCTUATION.sub(" ", text.lower())
    text = _WHITESPACE.sub(" ", text).strip()
    words = [_ABBREVIATIONS.get(word, word) for word in text.split(" ")]
    # Drop the city/state/zip tail so "450 Hayes St" matches the full form.
    stop = {"san", "francisco", "ca", "california", "usa"}
    kept: list[str] = []
    for word in words:
        if word in stop or re.fullmatch(r"9\d{4}", word):
            break
        kept.append(word)
    return " ".join(kept)


class SanFranciscoAdapter:
    """City adapter backed by a reference address file."""

    def __init__(self, fixtures_dir: Path) -> None:
        path = fixtures_dir / "san-francisco" / "addresses.json"
        if not path.is_file():
            raise ConfigurationError(
                "San Francisco reference address data is missing",
                details={"expected_path": str(path)},
            )
        data = json.loads(path.read_text(encoding="utf-8"))

        self._districts: tuple[str, ...] = tuple(data["districts"])
        self._by_id: dict[str, NormalizedAddress] = {}
        self._by_normalized: dict[str, str] = {}

        for entry in data["addresses"]:
            address = NormalizedAddress(
                address_id=entry["address_id"],
                display=entry["display"],
                district_id=entry["district_id"],
                jurisdiction_id=data.get("jurisdiction_id", DEFAULT_JURISDICTION_ID),
                latitude=entry["latitude"],
                longitude=entry["longitude"],
                parcel_ref=entry.get("parcel_ref"),
            )
            self._by_id[address.address_id] = address
            self._by_normalized[_normalize_token(address.display)] = address.address_id

    @property
    def municipality_id(self) -> str:
        return MUNICIPALITY_ID

    @property
    def default_jurisdiction_id(self) -> str:
        return DEFAULT_JURISDICTION_ID

    def normalize_address(self, raw: str) -> NormalizedAddress | None:
        address_id = self._by_normalized.get(_normalize_token(raw))
        if address_id is None:
            # A raw address_id is also accepted, so CAD can dispatch by either.
            return self._by_id.get(raw.strip())
        return self._by_id[address_id]

    def get_address(self, address_id: str) -> NormalizedAddress | None:
        return self._by_id.get(address_id)

    def list_districts(self) -> Sequence[str]:
        return self._districts

    def list_addresses(self, district_id: str | None = None) -> Sequence[NormalizedAddress]:
        addresses = [self._by_id[k] for k in sorted(self._by_id)]
        if district_id is None:
            return addresses
        return [a for a in addresses if a.district_id == district_id]

    def source_ids(self) -> Sequence[str]:
        """Sources configured for San Francisco."""
        return (
            "sf-permits",
            "sf-assessor",
            "sf-fire-inspections",
            "sf-violations",
            "sf-hydrants",
            "sf-parcels",
            "google-solar",
            "usgs-3dep",
            "epa-frs",
            "phmsa-pipelines",
            "nrel-ev",
            "tier-ii-confidential",
            "nws",
        )
