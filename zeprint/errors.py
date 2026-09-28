"""Exception types. ``str(e)`` is always safe and useful to show to a user."""


class ZePrintError(Exception):
    status_code = 500


class NotFoundError(ZePrintError):
    """Unknown label, printer or job."""
    status_code = 404


class InvalidRequest(ZePrintError):
    """Bad parameters, unsupported size, no printer configured, ..."""
    status_code = 422


class LabelError(ZePrintError):
    """The label can't be made from these inputs (bad station id, corrupt TLE, ...)."""
    status_code = 422


class FetchError(ZePrintError):
    """An upstream data source was unreachable or answered with garbage."""
    status_code = 502


class PrinterError(ZePrintError):
    """The printer could not be reached or refused the job."""
    status_code = 502
