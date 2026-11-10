"""Load the real API client without importing Home Assistant's integration entry."""

import importlib.util
from pathlib import Path
import sys
import types


def load_api():
    name = "air352_standalone"
    if f"{name}.api" in sys.modules:
        return sys.modules[f"{name}.api"]
    root = Path(__file__).resolve().parents[1] / "custom_components" / "air352"
    package = types.ModuleType(name)
    package.__path__ = [str(root)]
    sys.modules[name] = package
    spec = importlib.util.spec_from_file_location(f"{name}.api", root / "api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
