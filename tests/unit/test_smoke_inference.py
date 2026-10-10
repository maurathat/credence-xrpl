"""Smoke test: credence.inference imports, and every stub raises NotImplementedError."""

import importlib
import inspect

import pytest

MODULES = [
    "credence.inference.gateway",
    "credence.inference.provider_a",
    "credence.inference.provider_b_sidecar",
]


@pytest.mark.parametrize("name", MODULES)
def test_imports_and_stubs_raise(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__doc__
    for _, fn in inspect.getmembers(module, inspect.isfunction):
        if fn.__module__ == name:
            with pytest.raises(NotImplementedError):
                fn()
