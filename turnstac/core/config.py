import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

_defaults = {}  # namespace: dict
_file = {}  # namespace: dict
_cli = {}  # namespace: dict


def register_defaults(namespace: str, defaults: dict) -> None:
    _defaults[namespace] = dict(defaults)


def load_config(path: Path) -> None:
    """Load namespaced YAML config into the file layer. Warns on unknown sections/keys."""
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise TypeError(
            f"Config {path} must be a YAML mapping, not {type(data).__name__}"
        )

    for ns, values in data.items():
        if ns not in _defaults:
            continue  # sibling tool's section in a shared config, ignore quietly
        if values is None:
            continue  # section present but empty (all keys commented) = no overrides
        if not isinstance(values, dict):
            raise TypeError(
                f"Section '{ns}' in {path} must be a mapping, not {type(values).__name__}"
            )
        for key in values:
            if key not in _defaults[ns]:
                log.warning(f"unknown key '{ns}.{key}' in {path}")
        _file.setdefault(ns, {}).update(values)
    log.debug(f"loaded config from {path}")


def merge_cli(namespace: str, cli_args) -> None:
    """Route non-None CLI args into the cli layer. loglevel is CLI-only, never a config key."""
    if namespace not in _defaults:
        raise KeyError(f"unregistered namespace {namespace!r}")
    for key, value in vars(cli_args).items():
        if key in ("config", "init", "loglevel") or value is None:
            continue
        _cli.setdefault(namespace, {})[key] = value


def section(namespace: str) -> dict:
    """Resolve a namespace: defaults < file < cli."""
    if namespace not in _defaults:
        raise KeyError(f"unregistered namespace {namespace!r}")
    merged = {}
    merged.update(_defaults.get(namespace, {}))
    merged.update(_file.get(namespace, {}))
    merged.update(_cli.get(namespace, {}))
    return merged


def generate_template_config(namespace: str, path: Path) -> None:
    """Write a commented YAML template of the given namespace's defaults."""
    if namespace not in _defaults:
        raise KeyError(f"Namespace '{namespace}' not registered")

    lines = [
        "# Configuration template. All values shown are defaults.",
        "# Uncomment and modify as needed. CLI args override values set here.",
        "",
        f"{namespace}:",
    ]
    body = yaml.safe_dump(_defaults[namespace], sort_keys=False).splitlines()
    lines += [f"  # {line}" for line in body]
    lines.append("")

    Path(path).write_text("\n".join(lines), encoding="utf-8")
    log.info(f"Template config written to: {path}")
