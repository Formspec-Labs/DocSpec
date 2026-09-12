"""Independent access to a pinned exported DocSpec result.

The installed Rulespec artifact package verifies container identity, membership
and exact bytes. DocSpec verifies its active rows and typed receipt relations.
No original workspace, live processing implementations or network access is
needed. Use ``open_result_export`` as a context manager.
"""

from docspec.adapters.result_export.admission import ExportAdmissionError
from docspec.adapters.result_export.reader import AdmittedResultExport, open_result_export

__all__ = ["AdmittedResultExport", "ExportAdmissionError", "open_result_export"]
