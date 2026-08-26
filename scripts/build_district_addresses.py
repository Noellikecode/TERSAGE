"""Build the municipality's reference address file from real SF parcels.

The console's whole premise is that months of survey work already happened, so
it has to open on a district with structures in it. Nine addresses is enough to
exercise the code and not enough to look like a city.

**Real geography, synthesised records.** The addresses, block and lot numbers,
and coordinates here are pulled live from San Francisco's published parcel
dataset, so a district is a real place with real parcels in it and the massing
model sits where the building actually is. What the seed then *says* about those
structures -- storeys, construction type, inspection findings, roof geometry --
is synthetic, deterministic, and marked ``"synthetic": true`` in the seed
document itself. That line is the one that matters: this script decides which
buildings exist, never what is true about them.

No person appears in the output. The parcel feed carries no owner or occupant
information and nothing here reads any other source.

    .venv/bin/python scripts/build_district_addresses.py --limit 400

Rewrites ``fixtures/san-francisco/addresses.json``. Re-run ``firstdue seed``
afterwards to rebuild the demo state over the new address list.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

PARCELS_URL = "https://data.sfgov.org/resource/acdm-wktn.json"

#: Which real supervisor districts stand in for the two fire districts. Chosen
#: because both are dense, mixed-age housing stock -- the conditions the
#: conflict rules are about -- rather than for any operational correspondence.
DISTRICT_FOR_SUPERVISOR: dict[str, str] = {
    "5": "sffd-district-03",
    "8": "sffd-district-05",
}

JURISDICTION_ID = "sf-city-county"

#: Every address the hand-written fixture already carried is kept, whatever the
#: feed returns. They are load-bearing in a way a generated row is not: one of
#: them (``sf-0415-mission``) is deliberately *absent* from the seed so the
#: cold-start path has an address with nothing on record, and others carry the
#: disagreement the demo promises and the values tests assert on. Pinning only
#: the famous one dropped the rest and broke eleven tests -- the fixture is
#: reference data, and this script may only ever add to it.
PINNED_IDS = frozenset(
    {
        "sf-0450-hayes",
        "sf-1215-fell",
        "sf-0899-valencia",
        "sf-2130-mission",
        "sf-0350-rhode-island",
        "sf-1550-bryant",
        "sf-0621-clement",
        "sf-3120-24th",
        # Cold start: named in `demo/seed.py:RECORDS_ONLY`, and the reason a
        # brief can say UNKNOWN about a structure nobody has surveyed.
        "sf-0415-mission",
    }
)


def _title(text: str) -> str:
    return " ".join(word.capitalize() for word in text.split())


def _address_id(row: dict[str, Any]) -> str | None:
    number = (row.get("from_address_num") or "").strip()
    street = (row.get("street_name") or "").strip()
    if not number or not street or not number.isdigit():
        return None
    slug = street.lower().replace(" ", "-")
    return f"sf-{int(number):04d}-{slug}"


def _display(row: dict[str, Any]) -> str:
    number = (row.get("from_address_num") or "").strip()
    street = _title((row.get("street_name") or "").strip())
    suffix = _title((row.get("street_type") or "").strip())
    return f"{int(number)} {street} {suffix}, San Francisco, CA".replace("  ", " ")


#: Metres per degree, near San Francisco. Good to well under a metre at parcel
#: scale, which is the scale a massing model is drawn at.
_M_PER_DEG_LAT: float = 110_540.0
_M_PER_DEG_LON: float = 111_320.0

#: A footprint with fewer vertices than this is not a building outline.
_MIN_VERTICES: int = 4

#: And one with more is a parcel nobody needs at this fidelity -- the renderer
#: extrudes it, and a hundred-vertex ring costs more than it shows.
_MAX_VERTICES: int = 24


def _footprint(row: dict[str, Any], lat: float, lon: float) -> list[list[float]] | None:
    """The parcel's real outline, in metres relative to its own centroid.

    The seed used to extrude a literal rectangle for every structure, so every
    building in the district was the same box at a different height. The feed
    already carries the true polygon; converting it here means the massing model
    is the shape of the actual parcel.

    Projected with a local equirectangular approximation rather than a real
    projection: over a single parcel the error is centimetres, and pulling in a
    projection library to be more right than the source data would be false
    precision.
    """
    shape = row.get("shape") or {}
    coords = shape.get("coordinates") or []
    # MultiPolygon -> first polygon -> outer ring.
    ring = coords[0][0] if coords and coords[0] else None
    if not ring or len(ring) < _MIN_VERTICES:
        return None

    import math

    scale_lon = _M_PER_DEG_LON * math.cos(math.radians(lat))
    points = [
        [round((float(x) - lon) * scale_lon, 2), round((float(y) - lat) * _M_PER_DEG_LAT, 2)]
        for x, y in ring
    ]
    # A GeoJSON ring repeats its first point; the renderer closes it itself.
    if points and points[0] == points[-1]:
        points.pop()
    if len(points) < 3 or len(points) > _MAX_VERTICES:
        return None
    return points


def fetch(limit: int, timeout: float) -> list[dict[str, Any]]:
    """Pull parcels for the supervisor districts we map, newest page first."""
    rows: list[dict[str, Any]] = []
    with httpx.Client(timeout=timeout) as client:
        for supervisor, district in DISTRICT_FOR_SUPERVISOR.items():
            page = client.get(
                PARCELS_URL,
                params={
                    "active": "true",
                    "supervisor_district": supervisor,
                    "$limit": limit,
                    "$order": "blklot",
                },
            )
            page.raise_for_status()
            for row in page.json():
                row["_district_id"] = district
                rows.append(row)
    return rows


def build(rows: list[dict[str, Any]], existing: dict[str, Any]) -> dict[str, Any]:
    """Turn parcel rows into reference addresses, keeping the pinned ones."""
    kept: dict[str, dict[str, Any]] = {
        entry["address_id"]: entry
        for entry in existing.get("addresses", [])
        if entry["address_id"] in PINNED_IDS
    }

    for row in rows:
        address_id = _address_id(row)
        lat, lon = row.get("centroid_latitude"), row.get("centroid_longitude")
        if not address_id or address_id in kept or not lat or not lon:
            continue
        block, lot = (row.get("block_num") or "").strip(), (row.get("lot_num") or "").strip()
        kept[address_id] = {
            "address_id": address_id,
            "display": _display(row),
            "district_id": row["_district_id"],
            "latitude": round(float(lat), 6),
            "longitude": round(float(lon), 6),
            "parcel_ref": f"{block}-{lot}" if block and lot else None,
            # The real outline, when the feed gives a usable one. Absent, the
            # seed falls back to its rectangle -- a building drawn as a box is
            # worse than one drawn correctly and better than none at all.
            "footprint": _footprint(row, float(lat), float(lon)),
        }

    return {
        "jurisdiction_id": existing.get("jurisdiction_id", JURISDICTION_ID),
        "districts": existing["districts"],
        # Sorted, so re-running against the same feed produces the same file and
        # the seed's content hash stays a meaningful check.
        "addresses": [kept[k] for k in sorted(kept)],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=400, help="parcels per district")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("fixtures/san-francisco/addresses.json"),
    )
    args = parser.parse_args()

    existing = json.loads(args.out.read_text(encoding="utf-8"))
    before = len(existing["addresses"])

    try:
        rows = fetch(args.limit, args.timeout)
    except Exception as exc:
        print(
            f"error: could not read the parcel feed: {type(exc).__name__}: {exc}", file=sys.stderr
        )
        return 1

    document = build(rows, existing)
    args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    per_district: dict[str, int] = {}
    for entry in document["addresses"]:
        per_district[entry["district_id"]] = per_district.get(entry["district_id"], 0) + 1

    print(f"{args.out}: {before} -> {len(document['addresses'])} addresses")
    for district, count in sorted(per_district.items()):
        print(f"  {district}  {count}")
    print("\nnext: .venv/bin/python -m firstdue.cli seed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
