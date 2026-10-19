"""Phase 6-B: QC content checks (P2, advisory — ADR-0010, D-06).

- CHECKS registry has 15 entries (13 format + 2 advisory content).
- Advisory checks report stub=true / warn with "vision 모델 미연결" (never
  hidden as auto-pass), and can never emit fail themselves.
- _overall() excludes advisory codes from the fail branch: even a "fail"
  from an advisory check cannot turn overall into "fail", so the render
  gate is never blocked by content checks.
"""
from __future__ import annotations

import pytest

from app.core import defaults
from app.services import qc_content, qc_orchestrator, vision_client
from app.services.qc_orchestrator import (
    ADVISORY_CHECKS,
    CHECKS,
    CheckResult,
    QCContext,
    _overall,
)


@pytest.fixture(autouse=True)
def _no_vision_endpoint(monkeypatch):
    # Stub-path tests must not depend on ambient env.
    monkeypatch.delenv("QC_VISION_BASE_URL", raising=False)
    monkeypatch.delenv("QC_VISION_MODEL", raising=False)


def _ctx() -> QCContext:
    return QCContext(timeline=None, preset=None)


def test_registry_has_15_checks_with_2_advisory():
    assert len(CHECKS) == 15
    codes = [code for code, _ in CHECKS]
    assert len(set(codes)) == 15  # no duplicates
    assert "story_fidelity" in codes
    assert "performance_quality" in codes
    assert ADVISORY_CHECKS == {"story_fidelity", "performance_quality"}
    # the 13 P0 format checks are untouched
    assert "text_ocr_match" in codes and "black_frame" in codes


def test_content_checks_stub_warn_not_hidden():
    for fn in (qc_content.check_story_fidelity, qc_content.check_performance_quality):
        result = fn(_ctx())
        assert result.status == "warn", result
        assert result.stub is True, result
        assert "vision 모델 미연결" in result.details, result


def test_thresholds_live_in_defaults():
    # Vision endpoint is env-configured now (QC_VISION_* in vision_client);
    # unset by default -> checks stay stubs.
    assert vision_client.available() is False
    assert vision_client.AVAILABLE is False
    assert defaults.QC_VISION_TIMEOUT_SEC == 60.0
    assert defaults.QC_VISION_FRAME_MAX_DIM == 768
    assert 0.0 < defaults.QC_STORY_FIDELITY_WARN_SCORE < 1.0
    assert 0.0 < defaults.QC_PERFORMANCE_WARN_SCORE < 1.0


def _results_with(status_by_code: dict) -> list[CheckResult]:
    out = []
    for code, _ in CHECKS:
        status = status_by_code.get(code, "pass")
        out.append(CheckResult(code=code, status=status))
    return out


def test_advisory_fail_cannot_fail_overall():
    # Even if an advisory check reported "fail" (it never does by itself),
    # the overall verdict must not become "fail" — the render gate stays open.
    results = _results_with({"story_fidelity": "fail",
                             "performance_quality": "fail"})
    assert _overall(results) == "pass"


def test_advisory_warn_yields_overall_warn():
    results = _results_with({"story_fidelity": "warn"})
    assert _overall(results) == "warn"


def test_format_fail_still_fails_gate():
    # Advisory exemption does not weaken the P0 format gate.
    results = _results_with({"subtitle_timing": "fail",
                             "story_fidelity": "warn"})
    assert _overall(results) == "fail"


def test_all_pass_stays_pass():
    assert _overall(_results_with({})) == "pass"


def test_content_check_never_emits_fail_without_vision_model():
    # Stub path is the only reachable path without a vision model; it warns.
    statuses = {qc_content.check_story_fidelity(_ctx()).status,
                qc_content.check_performance_quality(_ctx()).status}
    assert statuses == {"warn"}
