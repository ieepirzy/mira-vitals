"""Orchestration: run the analyzers, build the snapshot, report, export.

CI calls this; CI does not contain any of the logic.  The exit code encodes
policy and only policy:

* analyzer failure and blocking-gate violations fail the run;
* telemetry failure (OTLP or HTTP) never does, by default.

That split is the point of the whole design.  "We could not measure" and "the
measurement was bad" must be loud; "we could not ship the measurement
somewhere" must not break a build whose code is fine.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import context, deltas as deltas_module, emit as emit_module, normalize, otel, report, schema
from .analyzers import radon_cc, ruff_lint, security as security_analyzer, tests_cov, typecheck
from .config import Config, load as load_config


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="code-health", description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output", default="code-health.json", help="Canonical artifact path.")
    parser.add_argument("--baseline", help="Previous snapshot to compute deltas against.")
    parser.add_argument("--junit", help="JUnit XML produced by the test run.")
    parser.add_argument("--coverage", help="coverage.py XML or JSON report.")
    parser.add_argument(
        "--tests-python-version",
        help="Interpreter that produced the consumed test reports; recorded in the snapshot.",
    )
    parser.add_argument("--bandit-json", help="Bandit JSON report.")
    parser.add_argument("--semgrep-json", help="Semgrep JSON report.")
    parser.add_argument("--osv-json", help="OSV-Scanner JSON report.")
    parser.add_argument("--gitleaks-json", help="Gitleaks JSON report.")
    parser.add_argument("--type-checker", choices=("pyright", "mypy"), help="Overrides pyproject.")
    parser.add_argument(
        "--fail-on-analyzer-error",
        action="store_true",
        default=True,
        help="Fail when an analyzer could not run (default: on).",
    )
    parser.add_argument("--no-fail-on-analyzer-error", dest="fail_on_analyzer_error", action="store_false")
    parser.add_argument(
        "--gate",
        action="store_true",
        help="Apply blocking quality gates (lint, and type-check when enabled).",
    )
    parser.add_argument("--emit-otlp", action="store_true", help="Export over OTLP when an endpoint is configured.")
    parser.add_argument("--emit-http", action="store_true", help="POST the snapshot to CODE_HEALTH_ENDPOINT.")
    parser.add_argument(
        "--blocking-telemetry",
        action="store_true",
        help="Fail the run when telemetry emission fails (off by default, and deliberately).",
    )
    parser.add_argument("--step-summary", help="Write a markdown summary here (default: $GITHUB_STEP_SUMMARY).")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def _load_baseline(path: str | None, log: Any) -> dict[str, Any] | None:
    """Read a baseline snapshot, tolerating its absence.

    A missing baseline is the normal state of the first run on a new branch and
    of every run before this system existed.  It yields ``None`` deltas, not an
    error and not zeros.
    """
    if not path:
        return None
    if not os.path.exists(path):
        log(f"code-health: no baseline at {path}; deltas will be reported as unavailable")
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            baseline = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"code-health: baseline at {path} could not be read ({exc}); deltas unavailable")
        return None
    if not isinstance(baseline, dict) or "schema_version" not in baseline:
        log(f"code-health: baseline at {path} is not a code-health snapshot; deltas unavailable")
        return None
    return baseline


def collect_snapshot(
    config: Config,
    args: argparse.Namespace,
    exporter: otel.Exporter,
    log: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run every analyzer under its own span and normalize the results."""
    from . import provenance as provenance_module

    root = args.repo_root
    checker = args.type_checker or config.type_checker

    with exporter.span("code_health.radon.cc", {"code.health.tool": "radon"}):
        symbols, radon_tool = radon_cc.collect_complexity(config.paths, cwd=root)
    with exporter.span("code_health.radon.raw", {"code.health.tool": "radon"}):
        raw, raw_tool = radon_cc.collect_raw(config.paths, cwd=root)
    with exporter.span("code_health.radon.mi", {"code.health.tool": "radon"}):
        maintainability, mi_tool = radon_cc.collect_maintainability(config.paths, cwd=root)
    with exporter.span("code_health.radon.hal", {"code.health.tool": "radon"}):
        halstead, hal_tool = radon_cc.collect_halstead(config.paths, cwd=root)
    with exporter.span("code_health.ruff", {"code.health.tool": "ruff"}):
        lint, lint_tool = ruff_lint.collect(
            config.effective_lint_paths,
            cwd=root,
            gate_paths=config.effective_lint_gate_paths,
            select=config.lint_select,
            ignore=config.lint_ignore,
        )
    with exporter.span("code_health.typecheck", {"code.health.tool": checker}):
        typing_result, type_tool = typecheck.collect(
            config.effective_typecheck_paths, tool_name=checker, cwd=root
        )
    with exporter.span("code_health.tests"):
        tests, tests_tool = tests_cov.collect(
            junit_path=args.junit,
            coverage_path=args.coverage,
            python_version=args.tests_python_version,
        )
    with exporter.span("code_health.security"):
        security, security_tool = security_analyzer.collect(
            bandit_json=args.bandit_json,
            osv_json=args.osv_json,
            semgrep_json=args.semgrep_json,
            gitleaks_json=args.gitleaks_json,
        )

    run, ci = context.collect(repo_root=root)
    context.finalize_observation_id(run, config.paths)

    target = {
        "language": config.language,
        "paths": config.paths,
        "lint_paths": config.effective_lint_paths,
        "typecheck_paths": config.effective_typecheck_paths,
        "python_version": sys.version.split()[0],
        "config_source": config.source,
    }

    tool_runs = [radon_tool, raw_tool, mi_tool, hal_tool, lint_tool, type_tool, tests_tool, security_tool]
    tools = {t.name: t.as_dict() for t in tool_runs}

    baseline = _load_baseline(args.baseline, log)
    snapshot = normalize.build(
        run=run,
        ci=ci,
        target=target,
        provenance=provenance_module.collect(
            agent_sources=config.provenance_agent_sources,
            labels=config.provenance_labels,
        ),
        symbols=symbols,
        raw=raw,
        maintainability=maintainability,
        halstead=halstead,
        lint=lint,
        typing=typing_result,
        tests=tests,
        security=security,
        tools=tools,
        complexity_status=radon_tool.status,
        summary_status=raw_tool.status,
        maintainability_status=mi_tool.status,
        halstead_status=hal_tool.status,
        hotspot_limit=config.hotspot_limit,
    )
    snapshot["deltas"] = deltas_module.compute(snapshot, baseline)
    # Re-validate: deltas were substituted after the builder's own check.
    schema.validate(snapshot)
    changes = deltas_module.symbol_changes(snapshot, baseline)
    return snapshot, changes


#: Analyzers the configuration asks for unconditionally.  If one of these is
#: missing or broken, the run did not measure what it claims to measure.
REQUIRED_ANALYZERS = frozenset({"radon", "radon-raw", "radon-mi", "radon-hal", "ruff"})

#: Analyzers that are opt-in per invocation (they consume reports CI may or may
#: not have produced).  Their absence is a configuration choice, not a fault.
OPTIONAL_ANALYZERS = frozenset({"tests", "security"})


def _analyzer_failures(snapshot: dict[str, Any], *, type_checker: str) -> list[str]:
    """Analyzers whose absence or failure invalidates this run.

    Three statuses, three meanings, and collapsing them is how a pipeline ends
    up silently reporting nothing for months:

    * ``error`` -- the tool ran and something went wrong.  Always a failure: it
      is a measurement we believed we had and do not.
    * ``unavailable`` -- the tool is not installed.  A failure *for a required
      analyzer*.  Without this case a runner that lost radon would emit a
      snapshot whose every complexity field is ``null``, export no complexity
      metrics at all, and exit 0 -- so the series would simply stop, looking
      from the outside like a repository nobody touched.
    * ``skipped`` -- deliberately not run.  Never a failure.

    The configured type checker counts as required: if a repository asks for
    pyright and pyright is missing, that is a broken setup, not a measurement
    of zero type errors.
    """
    required = REQUIRED_ANALYZERS | {type_checker}
    failures = []
    for name, info in snapshot["tools"].items():
        status = info.get("status")
        if status == "error":
            failures.append(f"{name}: {info.get('error') or 'error'}")
        elif status == "unavailable" and name in required:
            failures.append(f"{name}: required analyzer unavailable ({info.get('error')})")
    return failures


def _gate(snapshot: dict[str, Any], config: Config, log: Any) -> list[str]:
    """Blocking policy, evaluated from the normalized measurements.

    Complexity is measured, never gated -- there is no baseline yet, and
    failing a build because a function written in 2024 has CC 34 would teach
    everyone to distrust the whole lane.  Ratchets come after the data.
    """
    violations: list[str] = []

    lint = snapshot["lint"]
    if lint.get("status") == "ok" and lint.get("gate_errors") and not config.lint_blocking:
        log(
            f"code-health: ruff reports {lint['gate_errors']} finding(s) in the gated paths "
            f"-- measured, not blocking (set lint_blocking = true once the baseline is clean)"
        )
    elif lint.get("status") == "ok" and lint.get("gate_errors"):
        violations.append(
            f"ruff: {lint['gate_errors']} blocking finding(s) in {lint.get('gate_paths')} "
            f"({', '.join(lint['blocking_rule_prefixes'])})"
        )
    elif lint.get("status") == "ok" and lint.get("errors"):
        log(
            f"code-health: ruff reports {lint['errors']} blocking-class finding(s), "
            f"none inside the gated paths {lint.get('gate_paths')} -- measured, not blocking"
        )

    typing_result = snapshot["typing"]
    if config.typecheck_blocking and typing_result.get("status") == "ok" and typing_result.get("errors"):
        violations.append(f"{typing_result['tool']}: {typing_result['errors']} type error(s)")
    elif typing_result.get("status") == "ok" and typing_result.get("errors"):
        log(
            f"code-health: {typing_result['tool']} reports {typing_result['errors']} error(s) "
            f"-- measured, not blocking (set tool.code_health.typecheck_blocking to gate)"
        )

    tests = snapshot["tests"]
    if tests.get("status") == "ok" and (tests.get("failed") or tests.get("errors")):
        violations.append(f"tests: {tests.get('failed')} failed, {tests.get('errors')} errored")

    return violations


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    log = (lambda *a, **k: None) if args.quiet else print

    config = load_config(args.repo_root)
    exporter = otel.Exporter()

    # Tracing starts before the analyzers so their spans nest under the run.
    # A stub snapshot supplies resource identity; the real one does not exist
    # yet, and the resource must be fixed at provider construction.
    stub_run, stub_ci = context.collect(repo_root=args.repo_root)
    stub_run["observation_id"] = ""
    if args.emit_otlp:
        exporter.start_tracing(
            metrics_resource_stub({"run": stub_run, "ci": stub_ci})
        )

    with exporter.span("code_health.analysis"):
        snapshot, changes = collect_snapshot(config, args, exporter, log)

    with open(os.path.join(args.repo_root, args.output), "w", encoding="utf-8") as handle:
        handle.write(schema.dumps(snapshot))
    log(f"code-health: wrote {args.output} (schema v{snapshot['schema_version']})")

    log("")
    log(report.summary_text(snapshot))
    log("")
    log(report.hotspots_text(snapshot))
    if snapshot["deltas"].get("status") != "unavailable" or changes:
        log("")
        log(report.changes_text(snapshot, changes))
    log("")

    step_summary = args.step_summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(report.markdown_summary(snapshot, changes) + "\n")

    telemetry: dict[str, Any] = {}
    if args.emit_otlp:
        telemetry["otlp"] = exporter.export(snapshot, max_detail_events=config.max_detail_events)
        log(f"code-health: OTLP export {telemetry['otlp']['status']} "
            f"({telemetry['otlp']['metrics_exported']} metrics, {telemetry['otlp']['events_exported']} events)")
    exporter.shutdown_tracing()

    if args.emit_http:
        # Never `blocking=True` here: that raises, which escapes main() before
        # the exit-code policy runs and terminates with a traceback and exit 1
        # instead of the documented telemetry exit code. The status is enough --
        # the policy below owns the exit code, which is the whole point of
        # keeping policy in one place.
        telemetry["http"] = emit_module.emit(snapshot, log=log)
        log(f"code-health: HTTP emission {telemetry['http']['status']}")

    exit_code = 0

    failures = _analyzer_failures(snapshot, type_checker=args.type_checker or config.type_checker)
    if failures:
        for failure in failures:
            log(f"code-health: ANALYZER FAILURE -- {failure}")
        if args.fail_on_analyzer_error:
            exit_code = 2

    if args.gate:
        violations = _gate(snapshot, config, log)
        for violation in violations:
            log(f"code-health: GATE FAILURE -- {violation}")
        if violations:
            exit_code = max(exit_code, 1)

    if args.blocking_telemetry:
        # Both channels, not just OTLP. The HTTP path was documented as exiting
        # 3 and did not.
        failed = [
            channel
            for channel in ("otlp", "http")
            if telemetry.get(channel, {}).get("status") == "error"
        ]
        if failed:
            log(f"code-health: telemetry emission failed on {', '.join(failed)} (blocking)")
            exit_code = max(exit_code, 3)

    return exit_code


def metrics_resource_stub(stub: dict[str, Any]) -> dict[str, str]:
    """Trace resource built before the snapshot exists.

    Traces get the rich context resource -- unbounded run identity is correct
    on a span -- so this reuses the same builder with the partial document.
    """
    from .metrics import context_resource_attributes

    return context_resource_attributes({**stub, "target": {"language": "python"}})


def run() -> None:
    """Console-script entry point.

    ``main`` returns the exit code rather than raising, because the exit code
    *is* the policy and keeping it in one place is the point; this wrapper is
    the only thing that turns it into a process status.
    """
    import sys

    sys.exit(main())
