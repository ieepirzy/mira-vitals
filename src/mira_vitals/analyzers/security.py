"""Security-tool report ingestion.

These tools already run in CI and already gate the build; this adapter does not
re-run them or re-decide their policy.  It reads whatever reports the pipeline
produced so that the security posture travels in the same snapshot as
everything else, which is what makes "does code health predict incident rate?"
answerable later.

Every finding count is ``None`` unless a report was actually read.  A pipeline
where the Bandit step was skipped must not record "0 SAST findings".
"""

from __future__ import annotations

import json
import os
from typing import Any

from .base import ToolRun


def collect(
    *,
    bandit_json: str | None = None,
    osv_json: str | None = None,
    semgrep_json: str | None = None,
    gitleaks_json: str | None = None,
) -> tuple[dict[str, Any], ToolRun]:
    tool = ToolRun(name="security")
    data: dict[str, Any] = {
        "status": "skipped",
        "sast_findings": None,
        "sast_high": None,
        "sast_tool": None,
        "secret_findings": None,
        "secret_tool": None,
        "dependency_findings": None,
        "dependency_tool": None,
        "sources": {},
    }
    problems: list[str] = []
    read_any = False

    if bandit_json:
        payload, error = _load(bandit_json)
        if error:
            problems.append(error)
        else:
            results = payload.get("results", [])
            data["sast_findings"] = len(results)
            data["sast_high"] = sum(1 for r in results if r.get("issue_severity") == "HIGH")
            data["sast_tool"] = "bandit"
            data["sources"]["bandit"] = bandit_json
            read_any = True

    if semgrep_json:
        payload, error = _load(semgrep_json)
        if error:
            problems.append(error)
        else:
            results = payload.get("results", [])
            # Semgrep is an additional SAST signal alongside Bandit; summing
            # the two would invent a number neither tool reports, so semgrep
            # gets its own namespaced keys.
            data["semgrep_findings"] = len(results)
            data["semgrep_errors"] = len(payload.get("errors", []))
            data["sources"]["semgrep"] = semgrep_json
            read_any = True

    if osv_json:
        payload, error = _load(osv_json)
        if error:
            problems.append(error)
        else:
            count = sum(len(pkg.get("vulnerabilities", [])) for result in payload.get("results", [])
                        for pkg in result.get("packages", []))
            data["dependency_findings"] = count
            data["dependency_tool"] = "osv-scanner"
            data["sources"]["osv"] = osv_json
            read_any = True

    if gitleaks_json:
        payload, error = _load(gitleaks_json)
        if error:
            problems.append(error)
        else:
            # gitleaks emits a bare JSON array of findings.
            data["secret_findings"] = len(payload) if isinstance(payload, list) else None
            data["secret_tool"] = "gitleaks"
            data["sources"]["gitleaks"] = gitleaks_json
            read_any = True

    if problems:
        data["status"] = "error"
        data["error"] = "; ".join(problems)
        tool.status = "error"
        tool.error = data["error"]
    elif read_any:
        data["status"] = "ok"
    else:
        tool.status = "skipped"
        tool.error = "no security reports configured"
    return data, tool


def _load(path: str) -> tuple[Any, str | None]:
    if not os.path.exists(path):
        return None, f"security report not found: {path}"
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"could not read {path}: {exc}"
