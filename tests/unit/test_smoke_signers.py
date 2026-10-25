"""Smoke test: credence.signers imports; the loan broker signer is still a stub."""

import importlib
import inspect

import pytest

from credence.signers.policy_signer import PolicySigner

MODULES = [
    "credence.signers.loan_broker_signer",
    "credence.signers.payment_authorization",
    "credence.signers.policy_signer",
]


@pytest.mark.parametrize("name", MODULES)
def test_imports(name: str) -> None:
    assert importlib.import_module(name).__doc__


def test_loan_broker_stub_raises() -> None:
    module = importlib.import_module("credence.signers.loan_broker_signer")
    for _, fn in inspect.getmembers(module, inspect.isfunction):
        if fn.__module__ == module.__name__:
            with pytest.raises(NotImplementedError):
                fn()


def test_policy_signer_is_implemented() -> None:
    assert PolicySigner.sign_payment.__doc__
