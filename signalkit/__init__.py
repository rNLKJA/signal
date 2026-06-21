"""Signal: an interactive governed data product over South Australian (and NYC)
crime data, with a statistical analyst layer and a DTA / EU AI Act aligned
governance decision log.

The package is named ``signalkit`` (not ``signal``) so it never shadows
Python's standard-library ``signal`` module.

See ``signalkit.governance.decision_log`` for the audit-trail core,
``signalkit.analyst.core`` for the analyst layer that writes to it, and
``signalkit.analyst.stats`` for the statistical methods it reports.
"""

__version__ = "1.14.0"
