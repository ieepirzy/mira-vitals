"""Analyzer adapters.

Each adapter turns one tool's native output into a normalized fragment plus a
:class:`~mira_vitals.analyzers.base.ToolRun` record.  Adapters never
raise on tool failure -- they record it, so that "the analyzer broke" stays
distinguishable from "the analyzer found nothing".
"""
