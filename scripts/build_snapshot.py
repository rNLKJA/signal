"""Rebuild the bundled SA Police snapshot from live open data.

Run from the repo root (CI runs this monthly):

    python scripts/build_snapshot.py

Uses the same fetch + aggregate code the runtime uses, so the snapshot can
never drift from the live shape. Writes signalkit/data/sample/sa_crime_monthly.json.
"""

from __future__ import annotations

import json
from datetime import date

from signalkit.data import sa_crime


def main() -> None:
    records, _ = sa_crime.fetch_live()
    records.sort(key=lambda r: (r.month, r.region, r.offense))
    months = sorted({r.month for r in records})
    regions = sorted({r.region for r in records})

    payload = {
        "meta": {
            "source": "SA Police Crime statistics (data.sa.gov.au) — FY2024-25 + FY2025-26 YTD",
            "fetched_at": date.today().isoformat(),
            "window": {"start": months[0], "end": months[-1]},
            "aggregation": "offence counts by month x region x harmonised offence x ANZSOC division",
            "note": (
                f"Top {sa_crime.TOP_REGIONS} suburbs kept as distinct regions; the tail is "
                f"folded into '{sa_crime.OTHER_REGION}'. Offence labels harmonised across the "
                "SA Police 2025-26 taxonomy revision."
            ),
            "resources": [sa_crime.PREV_FY_RESOURCE, sa_crime.CURRENT_FY_RESOURCE],
        },
        "records": [r.model_dump() for r in records],
    }

    out = sa_crime.SNAPSHOT_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        json.dump(payload, f, indent=2)

    print(f"wrote {len(records)} records to {out}")
    print(f"window: {months[0]} .. {months[-1]} ({len(months)} months); regions: {len(regions)}")


if __name__ == "__main__":
    main()
