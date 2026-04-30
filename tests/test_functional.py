"""Functional tests for Oracle EBS writer using DB + HTTP VCR cassettes."""

import re
from pathlib import Path

import pytest
from keboola.datadirtest.vcr import VCRDataDirTester, get_test_cases
from keboola.vcr.db_recorder import OracleDBAdapter

FUNCTIONAL_DIR = str(Path(__file__).parent / "functional")
COMPONENT_SCRIPT = str(Path(__file__).parent.parent / "src" / "component.py")

_PROJECT_ROOT = str(Path(__file__).parent.parent) + "/"
LOG_NORMALIZERS = [
    (re.escape(_PROJECT_ROOT), ""),
]


@pytest.mark.parametrize("test_name", get_test_cases(FUNCTIONAL_DIR))
def test_functional(test_name):
    """Run a single functional test case with DB VCR support."""
    tester = VCRDataDirTester(
        data_dir=FUNCTIONAL_DIR,
        component_script=COMPONENT_SCRIPT,
        selected_tests=[test_name],
        db_adapter=OracleDBAdapter(),
    )
    tester.run()
