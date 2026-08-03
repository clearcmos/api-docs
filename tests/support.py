from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def load_fetcher(vendor: str) -> ModuleType:
    """Load a standalone vendor fetcher without requiring package-compatible directory names."""
    module_name = "external_docs_" + re.sub(r"[^a-z0-9_]", "_", vendor.lower())
    path = ROOT / vendor / "fetch.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load fetcher: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
