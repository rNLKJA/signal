"""Data access layer: live NYC Open Data queries with an offline snapshot fallback."""

from signalkit.data.nypd import (  # noqa: F401
    DataUnavailable,
    MonthlyRecord,
    get_records,
    load_snapshot,
)
