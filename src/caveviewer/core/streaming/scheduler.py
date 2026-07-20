"""Thread-safe ready backlog and pure chunk scheduling policies."""

from __future__ import annotations

import heapq
import queue
import threading
import time
from collections.abc import Callable
from typing import Generic, TypeVar


Cell = tuple[int, int, int]
ReadyItem = TypeVar("ReadyItem")


class BoundedReadyBacklog(Generic[ReadyItem]):
    """Bounded worker-to-render handoff retaining deferred items in place."""

    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("ready backlog capacity must be positive")
        self.capacity = int(capacity)
        self._items: list[ReadyItem] = []
        self._condition = threading.Condition()

    def put(self, data: ReadyItem, timeout: float | None = None) -> None:
        if timeout is not None and timeout < 0.0:
            raise ValueError("timeout must be non-negative")

        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while len(self._items) >= self.capacity:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    raise queue.Full
                self._condition.wait(remaining)
            self._items.append(data)
            self._condition.notify_all()

    def get_closest_nowait(
        self,
        distance_key: Callable[[ReadyItem], int] | None = None,
    ) -> ReadyItem:
        while True:
            with self._condition:
                if not self._items:
                    raise queue.Empty
                if distance_key is None:
                    data = self._items.pop(0)
                    self._condition.notify_all()
                    return data
                snapshot = tuple(self._items)

            selected = min(snapshot, key=distance_key)
            with self._condition:
                for item_index, data in enumerate(self._items):
                    if data is selected:
                        self._items.pop(item_index)
                        self._condition.notify_all()
                        return data

    def qsize(self) -> int:
        with self._condition:
            return len(self._items)

    def discard_if(
        self, predicate: Callable[[ReadyItem], bool]
    ) -> list[ReadyItem]:
        with self._condition:
            snapshot = tuple(self._items)
        discard_ids = {
            id(data)
            for data in snapshot
            if predicate(data)
        }
        if not discard_ids:
            return []

        with self._condition:
            discarded: list[ReadyItem] = []
            retained: list[ReadyItem] = []
            for data in self._items:
                if id(data) in discard_ids:
                    discarded.append(data)
                else:
                    retained.append(data)
            if not discarded:
                return []
            self._items = retained
            self._condition.notify_all()
            return discarded

    def clear(self) -> list[ReadyItem]:
        with self._condition:
            discarded = self._items
            self._items = []
            self._condition.notify_all()
            return discarded


def cell_distance_sq(cell: Cell, center: Cell) -> int:
    return sum((coordinate - origin) ** 2 for coordinate, origin in zip(cell, center))


def cell_in_cube_radius(cell: Cell, center: Cell, radius: int) -> bool:
    return all(
        abs(coordinate - origin) <= radius
        for coordinate, origin in zip(cell, center)
    )


def select_wanted_cells(
    available_cells: set[Cell],
    center: Cell,
    radius: int,
    max_loaded_chunks: int,
) -> set[Cell]:
    """Return all available cells inside the requested render-distance cube.

    ``max_loaded_chunks`` is retained for API compatibility with older call
    sites, but it does not trim the wanted set. The render-distance control is
    an explicit user request for visual coverage; residency limits are enforced
    by eviction policy for cells outside the wanted set and by bounded worker
    queues/backlogs.
    """
    cx, cy, cz = center
    wanted: set[Cell] = set()
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                cell = (cx + dx, cy + dy, cz + dz)
                if cell in available_cells:
                    wanted.add(cell)

    return wanted


def cells_outside_cube_radius(
    loaded_cells: set[Cell], center: Cell, radius: int
) -> set[Cell]:
    return {
        cell
        for cell in loaded_cells
        if not cell_in_cube_radius(cell, center, radius)
    }


def select_evictions(
    loaded_cells: set[Cell],
    wanted_cells: set[Cell],
    center: Cell | None,
    max_loaded_chunks: int,
) -> list[Cell]:
    effective_cap = max(max_loaded_chunks, len(wanted_cells))
    over_budget = len(loaded_cells) - effective_cap
    if over_budget <= 0:
        return []
    if center is None:
        return list(loaded_cells)[:over_budget]

    preferred = heapq.nlargest(
        over_budget,
        (cell for cell in loaded_cells if cell not in wanted_cells),
        key=lambda cell: cell_distance_sq(cell, center),
    )
    remaining = over_budget - len(preferred)
    if remaining <= 0:
        return preferred
    fallback = heapq.nlargest(
        remaining,
        (cell for cell in loaded_cells if cell in wanted_cells),
        key=lambda cell: cell_distance_sq(cell, center),
    )
    return preferred + fallback
