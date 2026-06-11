"""Load and represent the feature catalogue (features.yaml).

The catalogue has two top-level sections:

    features:       per-API adoption signals (does the charm use X?), each
                    with a scoring rule attached in scoring.py.
    architecture:   architectural-pattern labels (delta / holistic /
                    component-graph / shim). No scoring — these are facts
                    about the charm's overall structure that feed *into*
                    the scoring rules for features.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Detector:
    kind: str
    config: dict[str, Any]


@dataclass(frozen=True)
class Feature:
    name: str
    library: str
    summary: str
    scope: str  # "src" | "tests" | "any"
    detectors: tuple[Detector, ...]


@dataclass(frozen=True)
class Pattern:
    """An architectural pattern label. Same detector mechanics as Feature,
    but no library/scoring — patterns are descriptive, not prescriptive."""
    name: str
    summary: str
    scope: str
    detectors: tuple[Detector, ...]


def _parse_detectors(raw: list[dict[str, Any]]) -> tuple[Detector, ...]:
    return tuple(
        Detector(kind=d["kind"], config={k: v for k, v in d.items() if k != "kind"})
        for d in raw
    )


def load(path: Path) -> list[Feature]:
    data = yaml.safe_load(path.read_text())
    out: list[Feature] = []
    for raw in data["features"]:
        out.append(
            Feature(
                name=raw["name"],
                library=raw["library"],
                summary=raw["summary"],
                scope=raw["scope"],
                detectors=_parse_detectors(raw["detect"]),
            )
        )
    return out


def load_patterns(path: Path) -> list[Pattern]:
    """Load the architecture patterns. Returns [] if the section is absent."""
    data = yaml.safe_load(path.read_text())
    patterns_raw = data.get("architecture") or []
    return [
        Pattern(
            name=raw["name"],
            summary=raw["summary"],
            scope=raw["scope"],
            detectors=_parse_detectors(raw["detect"]),
        )
        for raw in patterns_raw
    ]
