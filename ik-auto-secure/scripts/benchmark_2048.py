from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from ik_chrome_auto.game2048 import (
    Board,
    LegacySmart2048Solver,
    Smart2048Solver,
    move_board,
)


EMPTY_BOARD: Board = ((0, 0, 0, 0),) * 4


def spawn(board: Board, random_source: random.Random) -> Board:
    empty = [
        (row, column)
        for row in range(4)
        for column in range(4)
        if board[row][column] == 0
    ]
    if not empty:
        return board
    row, column = random_source.choice(empty)
    rows = [list(source) for source in board]
    rows[row][column] = 1 if random_source.random() < 0.9 else 2
    return tuple(tuple(source) for source in rows)


def play(
    solver_factory: Callable[[], LegacySmart2048Solver | Smart2048Solver],
    seed: int,
) -> tuple[int, int, int]:
    random_source = random.Random(seed)
    board = spawn(spawn(EMPTY_BOARD, random_source), random_source)
    solver = solver_factory()
    score = moves = 0
    while True:
        direction, _value, _depth = solver.choose_move(board)
        if direction is None:
            break
        result = move_board(board, direction)
        score += result.score
        board = spawn(result.board, random_source)
        moves += 1
    return max(value for row in board for value in row), score, moves


def main() -> None:
    parser = argparse.ArgumentParser(description="So sánh solver 2048 bằng cùng tập seed")
    parser.add_argument("--games", type=int, default=10)
    parser.add_argument("--budget-ms", type=int, default=3)
    parser.add_argument("--depth", type=int, default=4)
    args = parser.parse_args()
    solvers = (LegacySmart2048Solver, Smart2048Solver)
    for solver_type in solvers:
        started = time.perf_counter()
        results = [
            play(
                lambda solver_type=solver_type: solver_type(
                    time_budget_ms=args.budget_ms,
                    max_depth=args.depth,
                ),
                seed,
            )
            for seed in range(args.games)
        ]
        average_score = statistics.mean(result[1] for result in results)
        average_level = statistics.mean(result[0] for result in results)
        reaches = {
            level: sum(result[0] >= level for result in results)
            for level in range(9, 14)
        }
        print(
            f"{solver_type.__name__}: avg_score={average_score:.0f}; "
            f"avg_level={average_level:.2f}; reach={reaches}; "
            f"elapsed={time.perf_counter() - started:.1f}s"
        )


if __name__ == "__main__":
    main()
