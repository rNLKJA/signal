"""Data access layer: live SA Police open-data queries with an offline snapshot fallback."""

from signalkit.data.sa_crime import (  # noqa: F401
    DataUnavailable,
    MonthlyRecord,
    get_records,
    load_snapshot,
)
