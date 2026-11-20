"""Run standalone unittest suites and record their JSON check evidence."""

import traceback
from typing import TextIO
import unittest


class _EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed_ids: set[str] = set()
        self.failure_details: list[dict[str, str]] = []

    @property
    def skipped_details(self) -> list[dict[str, str]]:
        return [{"test": test.id(), "reason": reason} for test, reason in self.skipped]

    def _record_failure(self, test, err, subtest=None):
        exception_type, exception, _ = err
        detail = {
            "test": test.id(),
            "exception_type": f"{exception_type.__module__}.{exception_type.__qualname__}",
            "message": str(exception),
            "traceback": "".join(traceback.format_exception(*err)),
        }
        if subtest is not None:
            detail["subtest"] = str(subtest)
        self.failure_details.append(detail)

    def addSuccess(self, test):
        self.passed_ids.add(test.id())
        super().addSuccess(test)

    def addFailure(self, test, err):
        self._record_failure(test, err)
        super().addFailure(test, err)

    def addError(self, test, err):
        self._record_failure(test, err)
        super().addError(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._record_failure(test, err, subtest)
        super().addSubTest(test, subtest, err)


def run_cases(
    *case_types: type[unittest.TestCase], stream: TextIO | None = None,
) -> tuple[dict[str, bool], _EvidenceResult]:
    suites = [unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in case_types]
    test_ids = [test.id() for suite in suites for test in suite]
    result = unittest.TextTestRunner(
        stream=stream, verbosity=2, resultclass=_EvidenceResult,
    ).run(unittest.TestSuite(suites))
    completed_ids = result.passed_ids | {detail["test"] for detail in result.failure_details}
    checks = {
        test_id.rsplit(".", 1)[-1].removeprefix("test_"): test_id in result.passed_ids
        for test_id in test_ids if test_id in completed_ids
    }
    return checks, result
