"""
Backwards-compatible shim.

The hourly reporter was replaced by reporting/session_report.py, which sends
two reports (6-hour and 24-hour) instead of twenty-four, and reports on
uptime and all-in cost. This module keeps the old import path working so the
dashboard and both control bots do not break on upgrade.

`read_hourly_log` now lives in session_report.py and reads both files, so
existing history remains visible through the old name.
"""
from reporting.session_report import (  # noqa: F401
    SessionReporter,
    read_hourly_log,
)

# The old class is aliased to the new one on purpose: any caller that still
# constructs HourlyReporter gets the 6h/24h behaviour rather than a silent
# return to hourly spam.
HourlyReporter = SessionReporter
