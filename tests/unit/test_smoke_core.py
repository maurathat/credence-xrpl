"""Smoke test: credence.core imports, and every stub raises NotImplementedError."""

import importlib
import inspect

import pytest

MODULES = [
    "credence.core.policy_engine",
    "credence.core.reservation_ledger",
    "credence.core.verifier",
    "credence.core.settlement_coordinator",
    "credence.core.repayment_scheduler",
    "credence.core.underwriting",
    "credence.core.decision_records",
    "credence.core.store.store",
]


@pytest.mark.parametrize("name", MODULES)
def test_imports_and_stubs_raise(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__doc__
    for _, fn in inspect.getmembers(module, inspect.isfunction):
        if fn.__module__ == name:
            with pytest.raises(NotImplementedError):
                fn()
