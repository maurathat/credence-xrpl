"""Product P0: the committed offline operation transcripts regenerate byte-for-byte."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "contracts" / "fixtures" / "build_api_p0_fixtures.py"


def test_offline_operation_transcripts_regenerate_exactly() -> None:
    spec = importlib.util.spec_from_file_location("build_api_p0_fixtures", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.OUT.read_text(encoding="ascii") == module.render()
