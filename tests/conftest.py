import os
import sys

# `src` layout: the package is not installed when running from a checkout.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest


@pytest.fixture
def snapshot_factory():
    """A minimal but schema-valid snapshot."""

    def build(**overrides):
        document = {
            "schema_version": 1,
            "generated_by": {"name": "code-health", "version": "0.1.0"},
            "definitions": dict(_definitions()),
            "run": {
                "repository": "mira-vitals",
                "repository_url": "https://github.com/ieepirzy/mira-vitals",
                "commit_sha": "a" * 40,
                "analyzed_tree_sha": "a" * 40,
                "branch": "main",
                "default_branch": "main",
                "is_default_branch": True,
                "ref_class": "default_branch",
                "change_id": None,
                "base_ref": None,
                "merge_base_sha": None,
                "timestamp": "2026-08-23T10:00:00Z",
                "canonical": True,
                "observation_id": "sha256:" + "b" * 64,
            },
            "ci": {"provider": "github-actions", "workflow": "ci", "job": "code-health", "run_id": "1"},
            "target": {"language": "python", "paths": ["src/mira_vitals"], "python_version": "3.12.0"},
            "provenance": {"authoring_mode": None, "agents": [], "human_authors": []},
            "summary": {"loc": 2377, "source_loc": 1651, "files": 7, "functions": 92},
            "complexity": {
                "status": "ok", "aggregate": 440, "mean": 4.78, "p50": 3, "p90": 9, "p95": 18,
                "max": 34, "min": 1, "functions_gt_10": 8, "functions_gt_15": 5,
                "functions_gt_20": 3, "high_complexity_fraction": 8 / 92, "density_per_kloc": 266.5,
            },
            "maintainability": {"status": "ok", "mean_index": 57.1},
            "halstead": {"status": "ok", "volume_total": 1.0},
            "lint": {"status": "ok", "tool": "ruff", "errors": 0, "warnings": 12, "total": 12,
                     "select": ["E4", "E7", "E9", "F", "W"]},
            "typing": {"status": "ok", "tool": "pyright", "errors": 55, "warnings": 0},
            "tests": {"status": "ok", "passed": 329, "failed": 0, "skipped": 1,
                      "coverage_percent": 89.18, "python_version": "3.12"},
            "security": {"status": "skipped", "sast_findings": None},
            "deltas": {"status": "unavailable", "values": {}},
            "hotspots": [],
            "files": [],
            "symbols": [],
            "tools": {},
        }
        document.update(overrides)
        return document

    return build


def _definitions():
    from mira_vitals.schema import DEFINITIONS

    return DEFINITIONS
