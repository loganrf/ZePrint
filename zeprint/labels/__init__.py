"""Label registry: built-in labels plus drop-in plugins from a directory."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil
import sys
from pathlib import Path

from ..zpl.builder import SIZES
from .base import Label, RenderContext, RenderResult

log = logging.getLogger(__name__)

_REGISTRY: dict[str, type[Label]] = {}
_builtin_loaded = False


def register(cls: type[Label]) -> type[Label]:
    """Class decorator that makes a label available everywhere."""
    for attr in ("id", "name", "Params"):
        if not hasattr(cls, attr):
            raise TypeError(f"label {cls.__name__} is missing {attr!r}")
    unknown = [s for s in cls.sizes if s not in SIZES]
    if unknown:
        raise TypeError(f"label {cls.id!r} declares unknown sizes {unknown}")
    if cls.id in _REGISTRY and _REGISTRY[cls.id] is not cls:
        log.warning("label %r from %s replaces %s", cls.id, cls.__module__,
                    _REGISTRY[cls.id].__module__)
    _REGISTRY[cls.id] = cls
    return cls


def load_builtin() -> None:
    global _builtin_loaded
    if _builtin_loaded:
        return
    for mod in pkgutil.iter_modules(__path__):
        if mod.name != "base" and not mod.name.startswith("_"):
            importlib.import_module(f"{__name__}.{mod.name}")
    _builtin_loaded = True


def load_plugins(directory: Path | str | None) -> list[str]:
    """Import every ``*.py`` in ``directory``; broken plugins are logged, not fatal."""
    loaded = []
    if not directory:
        return loaded
    d = Path(directory)
    if not d.is_dir():
        return loaded
    for path in sorted(d.glob("*.py")):
        if path.name.startswith("_"):
            continue
        name = f"zeprint_plugin_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            loaded.append(path.name)
            log.info("loaded plugin %s", path)
        except Exception:
            log.exception("could not load plugin %s", path)
    return loaded


def get(label_id: str) -> type[Label] | None:
    load_builtin()
    return _REGISTRY.get(label_id)


def all_labels() -> list[type[Label]]:
    load_builtin()
    return sorted(_REGISTRY.values(), key=lambda c: c.name.lower())


__all__ = ["Label", "RenderContext", "RenderResult", "register", "load_builtin", "load_plugins",
           "get", "all_labels"]
