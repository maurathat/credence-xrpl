"""Smoke test: credence.api imports and builds its app (Product P0 replaced the Checkpoint 0 stub)."""

import importlib

from fastapi import FastAPI


def test_app_imports_and_builds() -> None:
    module = importlib.import_module("credence.api.app")
    assert module.__doc__
    assert isinstance(module.create_app(), FastAPI)
