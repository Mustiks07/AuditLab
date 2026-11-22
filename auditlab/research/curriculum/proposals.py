"""Materialize new financial configurations during budgeted search.

The allowed axes and training roots are frozen before any evaluation. An
adaptive choice can refine an observed weakness by one neighboring value on
one axis. Both arms can explore the same finite space without generating
snapshots or copying candidate configurations in advance.
"""
from __future__ import annotations

import hashlib
import random
from bisect import bisect_right
from collections.abc import Collection, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .search import Candidate, RunAttempt, SearchConfig, ValidateCandidate, _compare, _json


@dataclass(frozen=True)
class ProposalAxis:
    """An existing generator config path and its ordered, permitted JSON values."""

    path: tuple[str, ...]
    values: tuple[Any, ...]

    def __post_init__(self) -> None:
        if isinstance(self.path, str) or not self.path or any(not isinstance(key, str) or not key for key in self.path):
            raise ValueError("axis path must be a nonempty sequence of nonempty keys")
        values = tuple(deepcopy(self.values))
        if len(values) < 2 or len({_json(value) for value in values}) != len(values):
            raise ValueError("axis requires at least two distinct JSON values")
        object.__setattr__(self, "path", tuple(self.path))
        object.__setattr__(self, "values", values)


def _container(config: dict, path: tuple[str, ...]) -> dict:
    node = config
    for key in path[:-1]:
        node = node.get(key)
        if not isinstance(node, dict):
            raise ValueError(f"axis path {path!r} must identify an existing config value")
    if path[-1] not in node:
        raise ValueError(f"axis path {path!r} must identify an existing config value")
    return node


@dataclass(frozen=True)
class _Grid:
    root: Candidate
    axes: tuple[ProposalAxis, ...]
    origin: tuple[int, ...]
    strides: tuple[int, ...]
    offset: int
    size: int

    @classmethod
    def build(cls, root: Candidate, axes: tuple[ProposalAxis, ...], offset: int) -> _Grid:
        axes = tuple(sorted(axes, key=lambda axis: axis.path))
        origin = []
        for index, axis in enumerate(axes):
            if any(axis.path[:len(previous.path)] == previous.path for previous in axes[:index]):
                raise ValueError("proposal axes must not overlap")
            base = _json(_container(root.config, axis.path)[axis.path[-1]])
            matches = tuple(map(_json, axis.values))
            if base not in matches:
                raise ValueError(f"axis {axis.path!r} must include the training root's value")
            origin.append(matches.index(base))
        strides = []
        size = 1
        for axis in reversed(axes):
            strides.append(size)
            size *= len(axis.values)
        return cls(deepcopy(root), axes, tuple(origin), tuple(reversed(strides)), offset, size)

    def positions(self, coordinate: int) -> tuple[int, ...]:
        remainder = coordinate - self.offset
        positions = []
        for stride in self.strides:
            position, remainder = divmod(remainder, stride)
            positions.append(position)
        return tuple(positions)

    def candidate(self, positions: tuple[int, ...]) -> Candidate:
        config = deepcopy(self.root.config)
        candidate_id = self.root.candidate_id
        if positions != self.origin:
            for axis, position in zip(self.axes, positions):
                _container(config, axis.path)[axis.path[-1]] = deepcopy(axis.values[position])
            variant = hashlib.sha256(_json([self.root.seed, config]).encode("utf-8")).hexdigest()[:16]
            candidate_id = f"{candidate_id}-p-{variant}"
        return Candidate(candidate_id, self.root.family_id, self.root.split, self.root.stratum, self.root.seed, config)


class _Space:
    def __init__(self, roots: list[Candidate], axes: Mapping[str, tuple[ProposalAxis, ...]]) -> None:
        self.grids = []
        self.offsets = []
        self.size = 0
        for root in roots:
            grid = _Grid.build(root, tuple(deepcopy(axes.get(root.candidate_id, ()))), self.size)
            self.grids.append(grid)
            self.offsets.append(grid.offset)
            self.size += grid.size

    def grid(self, coordinate: int) -> _Grid:
        return self.grids[bisect_right(self.offsets, coordinate) - 1]


class _Permutation:
    """An incremental Fisher–Yates draw with storage proportional to draws."""

    def __init__(self, size: int, seed: int) -> None:
        self.remaining = size
        self.swaps: dict[int, int] = {}
        self.random = random.Random(seed)

    def draw(self) -> int:
        position = self.random.randrange(self.remaining)
        self.remaining -= 1
        coordinate = self.swaps.pop(position, position)
        last = self.swaps.pop(self.remaining, self.remaining)
        if position != self.remaining:
            self.swaps[position] = last
        return coordinate


class _Picker:
    def __init__(self, space: _Space, seed: int, adaptive: bool, explore_every: int) -> None:
        self.space = space
        self.permutation = _Permutation(space.size, seed)
        self.adaptive = adaptive
        self.explore_every = explore_every
        self.refinement_random = random.Random(f"{seed}:refinement")
        self.seen: set[int] = set()

    def __bool__(self) -> bool:
        return len(self.seen) < self.space.size

    def _refinement(self, rows):
        parents = sorted(
            (row for row in reversed(rows) if row["status"] == "accepted"),
            key=lambda row: sum(attempt["weak"] for attempt in row["attempts"]),
            reverse=True,
        )
        for parent in parents:
            source = parent["proposal"]["coordinate"]
            grid = self.space.grid(source)
            neighbors = []
            for axis, stride, value in zip(grid.axes, grid.strides, grid.positions(source)):
                for step in (-1, 1):
                    coordinate = source + step * stride
                    target = value + step
                    if 0 <= target < len(axis.values) and coordinate not in self.seen:
                        neighbors.append((coordinate, axis, value, target))
            if neighbors:
                coordinate, axis, value, target = self.refinement_random.choice(neighbors)
                return coordinate, parent["candidate_id"], {
                    "path": list(axis.path), "before": deepcopy(axis.values[value]), "after": deepcopy(axis.values[target]),
                }
        return None

    def offer(self, rows: list[dict[str, Any]]) -> tuple[Candidate, dict[str, Any]]:
        refinement = None
        if self.adaptive and len(rows) % self.explore_every:
            refinement = self._refinement(rows)
        if refinement is not None:
            coordinate, parent, change = refinement
            method = "refinement"
        else:
            coordinate = self.permutation.draw()
            while coordinate in self.seen:
                coordinate = self.permutation.draw()
            parent, change = None, None
            method = "exploration" if self.adaptive else "uniform"
        self.seen.add(coordinate)
        grid = self.space.grid(coordinate)
        candidate = grid.candidate(grid.positions(coordinate))
        return candidate, {"coordinate": coordinate, "root_candidate_id": grid.root.candidate_id,
                           "candidate": asdict(candidate), "method": method, "parent": parent, "change": change}


def search_proposals(
    search_config: SearchConfig,
    run_attempt: RunAttempt,
    validate_candidate: ValidateCandidate,
    output_dir: str | Path,
    *,
    axes: Mapping[str, tuple[ProposalAxis, ...]],
    holdout_families: Collection[str] = (),
    holdout_seeds: Collection[int] = (),
    explore_every: int = 4,
) -> dict[str, Any]:
    """Compare online proposals with uniform sampling under identical per-arm caps.

    The validation callback materializes the proposed case and independently
    checks finance and evidence before the attempt callback can measure it.
    Recorded validation wall time includes materialization by the callback.
    The same root and seed are retained for every financial variant.
    """
    if type(explore_every) is not int or explore_every < 2:
        raise ValueError("explore_every must be an integer of at least two")
    roots = sorted(search_config.candidates, key=lambda candidate: candidate.candidate_id)
    root_ids = {root.candidate_id for root in roots}
    if not axes or axes.keys() - root_ids or not any(axes.values()):
        raise ValueError("proposal axes must be supplied for known training roots")
    if any(root.family_id in holdout_families or root.seed in holdout_seeds for root in roots):
        raise ValueError("proposal roots overlap the frozen holdout registry")
    if len({root.seed for root in roots}) != len(roots):
        raise ValueError("proposal roots must have distinct seeds to avoid overlapping configuration grids")

    space = _Space(roots, axes)
    plan = asdict(search_config)
    plan.update(candidates=[asdict(root) for root in roots], proposal_space={
        "coordinate_count": space.size,
        "explore_every": explore_every,
        "axes": {grid.root.candidate_id: [{"path": list(axis.path), "values": list(axis.values)} for axis in grid.axes]
                 for grid in space.grids},
        "holdout_registry_sha256": hashlib.sha256(_json({
            "family_ids": sorted(set(holdout_families)), "seeds": sorted(set(holdout_seeds)),
        }).encode("utf-8")).hexdigest(),
    })
    method = {"adaptive": "adjacent_unseen_refinement_of_accepted_proposals_with_periodic_exploration",
              "uniform": "seeded_uniform_coordinate_sampling_without_replacement",
              "parent_priority": "more_scored_weak_attempts_then_more_recently_accepted",
              "exploration_source": "same_seeded_coordinate_stream_as_uniform"}
    return _compare(search_config, run_attempt, validate_candidate, Path(output_dir), [], plan, method,
                    lambda adaptive: _Picker(space, search_config.seed, adaptive, explore_every))
