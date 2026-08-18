from pathlib import Path

import pytest

from ik_chrome_auto.game2048 import (
    Auto2048Player,
    REFERENCE_BOARD,
    Smart2048Solver,
    TileVision,
    _board_to_bits,
    _best_regular_five,
    _move_bits,
    _transpose_bits,
    available_moves,
    cell_feature,
    cell_spatial_feature,
    decode_png,
    detect_grid,
    feature_distance,
    is_valid_spawn_successor,
    move_board,
    region_feature,
    region_spatial_feature,
)


ASSET = (
    Path(__file__).parents[1]
    / "src"
    / "ik_chrome_auto"
    / "assets"
    / "2048-reference.png"
)
ASSET_DIR = ASSET.parent
FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_merge_uses_incrementing_levels_without_double_merge() -> None:
    board = (
        (1, 1, 1, 1),
        (1, 1, 2, 0),
        (0, 0, 0, 0),
        (0, 0, 0, 0),
    )
    result = move_board(board, "left")
    assert result.board[0] == (2, 2, 0, 0)
    assert result.board[1] == (2, 2, 0, 0)
    assert result.score == 12


def test_move_down() -> None:
    board = (
        (1, 0, 0, 0),
        (1, 0, 0, 0),
        (2, 0, 0, 0),
        (2, 0, 0, 0),
    )
    assert move_board(board, "down").board == (
        (0, 0, 0, 0),
        (0, 0, 0, 0),
        (2, 0, 0, 0),
        (3, 0, 0, 0),
    )


def test_reference_image_scans_exact_board() -> None:
    scan = TileVision(ASSET).scan_png(ASSET.read_bytes())
    assert scan.board == REFERENCE_BOARD
    assert scan.unknown_cells == ()
    assert scan.confidence > 0.70


def test_supplemental_levels_9_10_11_12_are_loaded() -> None:
    vision = TileVision(ASSET)
    level_9 = decode_png((ASSET_DIR / "2048-level-9-live.png").read_bytes())
    level_9_alt = decode_png((ASSET_DIR / "2048-level-9-live-alt.png").read_bytes())
    combined = decode_png((ASSET_DIR / "2048-levels-10-12.png").read_bytes())
    level_11 = decode_png((ASSET_DIR / "2048-level-11.png").read_bytes())
    level_11_live = decode_png((ASSET_DIR / "2048-level-11-live.png").read_bytes())
    features = {
        9: region_feature(level_9, 0, 0, level_9.width, level_9.height),
        10: region_feature(combined, 0, 0, 130, combined.height),
        11: region_feature(level_11, 0, 0, level_11.width, level_11.height),
        12: region_feature(combined, 134, 0, combined.width, combined.height),
    }
    assert {level for level in (9, 10, 11, 12) if level in vision.prototypes} == {
        9,
        10,
        11,
        12,
    }
    for level, feature in features.items():
        assert vision._level_distances(feature)[0][1] == level
    level_9_alt_feature = region_feature(
        level_9_alt,
        0,
        0,
        level_9_alt.width,
        level_9_alt.height,
    )
    assert vision._level_distances(level_9_alt_feature)[0][1] == 9
    live_feature = region_feature(
        level_11_live,
        0,
        0,
        level_11_live.width,
        level_11_live.height,
    )
    assert vision._level_distances(live_feature)[0][1] == 11


def test_every_viewport_sized_prototype_from_level_0_to_12_classifies_itself() -> None:
    vision = TileVision(ASSET)
    levels_seen: set[int] = set()
    for path in ASSET_DIR.glob("2048-live-level-*.png"):
        level = int(path.stem.split("-")[3])
        image = decode_png(path.read_bytes())
        colour = region_feature(image, 0, 0, image.width, image.height)
        spatial = region_spatial_feature(image, 0, 0, image.width, image.height)
        assert vision._level_distances(colour, spatial)[0][1] == level
        levels_seen.add(level)
    assert levels_seen == set(range(13))
    assert set(range(13)).issubset(vision.prototypes)


def test_live_prototypes_still_classify_after_holding_each_sample_out() -> None:
    for path in ASSET_DIR.glob("2048-live-level-*.png"):
        level = int(path.stem.split("-")[3])
        image = decode_png(path.read_bytes())
        colour = region_feature(image, 0, 0, image.width, image.height)
        spatial = region_spatial_feature(image, 0, 0, image.width, image.height)
        vision = TileVision(ASSET)
        colour_pool = vision.prototypes[level]
        spatial_pool = vision.spatial_prototypes[level]
        colour_pool.pop(
            min(
                range(len(colour_pool)),
                key=lambda index: feature_distance(colour, colour_pool[index]),
            )
        )
        spatial_pool.pop(
            min(
                range(len(spatial_pool)),
                key=lambda index: feature_distance(spatial, spatial_pool[index]),
            )
        )
        assert vision._level_distances(colour, spatial)[0][1] == level


def test_live_board_distinguishes_banana_3_from_pineapple_9() -> None:
    vision = TileVision(ASSET)
    scan = vision.scan_png(
        (FIXTURE_DIR / "2048-live-board-levels-3-9.png").read_bytes()
    )
    assert scan.board == (
        (1, 0, 0, 2),
        (0, 0, 7, 2),
        (0, 1, 7, 1),
        (11, 3, 9, 3),
    )
    assert scan.unknown_cells == ()


def test_live_board_recognises_levels_8_and_10_at_500x300() -> None:
    vision = TileVision(ASSET)
    scan = vision.scan_png(
        (FIXTURE_DIR / "2048-live-board-levels-8-10.png").read_bytes()
    )
    assert scan.board == (
        (2, 5, 8, 10),
        (4, 6, 2, 0),
        (3, 4, 2, 0),
        (0, 0, 1, 0),
    )
    assert scan.unknown_cells == ()


def test_live_board_distinguishes_kiwi_12_from_mango_8() -> None:
    vision = TileVision(ASSET)
    scan = vision.scan_png(
        (FIXTURE_DIR / "2048-live-board-level-12-vs-8.png").read_bytes()
    )
    assert scan.board == (
        (2, 1, 0, 0),
        (8, 3, 2, 1),
        (9, 2, 3, 2),
        (12, 8, 1, 3),
    )
    assert scan.unknown_cells == ()


def test_expected_high_tile_does_not_override_observed_sprite() -> None:
    vision = TileVision(ASSET)
    expected_rows = [list(row) for row in REFERENCE_BOARD]
    expected_rows[0][0] = 11
    expected = tuple(tuple(row) for row in expected_rows)
    scan = vision.scan_png(
        ASSET.read_bytes(),
        expected=expected,
    )
    assert scan.board[0][0] == REFERENCE_BOARD[0][0]


def test_expected_known_level_resolves_only_an_ambiguous_visual_tie() -> None:
    vision = TileVision(ASSET)
    image = decode_png(ASSET.read_bytes())
    grid = detect_grid(image)
    mango_feature = cell_feature(image, grid, 3, 2)
    mango_spatial = cell_spatial_feature(image, grid, 3, 2)
    vision.prototypes[9] = [tuple(value + 0.01 for value in mango_feature)]
    vision.spatial_prototypes[9] = [mango_spatial]
    expected_rows = [list(row) for row in REFERENCE_BOARD]
    expected_rows[3][2] = 9
    expected = tuple(tuple(row) for row in expected_rows)

    scan = vision.scan_png(ASSET.read_bytes(), expected=expected)

    assert scan.board[3][2] == 9


def test_expected_level_without_a_reference_is_never_synthesised() -> None:
    vision = TileVision(ASSET)
    expected_rows = [list(row) for row in REFERENCE_BOARD]
    expected_rows[3][2] = 13
    expected = tuple(tuple(row) for row in expected_rows)

    scan = vision.scan_png(ASSET.read_bytes(), expected=expected)

    assert 13 not in vision.prototypes
    assert scan.board[3][2] == 8


def test_confirmed_move_can_carry_a_level_above_available_prototypes() -> None:
    vision = TileVision(ASSET)
    expected_rows = [list(row) for row in REFERENCE_BOARD]
    expected_rows[3][2] = 13
    expected = tuple(tuple(row) for row in expected_rows)

    scan = vision.scan_png(
        ASSET.read_bytes(),
        expected=expected,
        trust_expected_levels=True,
    )

    assert 13 not in vision.prototypes
    assert scan.board[3][2] == 13


def test_spawn_successor_rejects_fictitious_high_tiles() -> None:
    expected = (
        (11, 0, 0, 0),
        (7, 4, 0, 0),
        (3, 2, 1, 0),
        (0, 0, 0, 0),
    )
    valid = (
        (11, 0, 0, 0),
        (7, 4, 1, 0),
        (3, 2, 1, 0),
        (0, 0, 0, 0),
    )
    fake = (
        (11, 0, 0, 0),
        (7, 15, 0, 0),
        (3, 2, 1, 0),
        (0, 0, 0, 0),
    )
    assert is_valid_spawn_successor(expected, valid)
    assert not is_valid_spawn_successor(expected, fake)


def test_grid_ignores_canvas_border_and_matches_square_span() -> None:
    # Regression for the 500x300 game: y=0 is a strong canvas border, while
    # 47..249 are the five real horizontal board dividers.
    candidates = (
        (0, 484),
        (39, 279),
        (47, 200),
        (97, 200),
        (148, 200),
        (196, 200),
        (203, 106),
        (249, 282),
        (261, 396),
    )
    lines, _confidence = _best_regular_five(
        candidates,
        extent=275,
        preferred_span=203,
    )
    assert lines == (47, 97, 148, 196, 249)


def test_grid_ignores_long_decorative_bar_near_canvas_top() -> None:
    candidates = (
        (6, 484),
        (45, 279),
        (53, 200),
        (103, 200),
        (154, 200),
        (202, 200),
        (209, 106),
        (255, 282),
        (267, 396),
    )
    lines, _confidence = _best_regular_five(
        candidates,
        extent=284,
        preferred_span=203,
    )
    assert lines == (53, 103, 154, 202, 255)


def test_smart_solver_returns_only_a_legal_move() -> None:
    solver = Smart2048Solver(time_budget_ms=500, max_depth=3)
    direction, _score, depth = solver.choose_move(REFERENCE_BOARD)
    assert direction in available_moves(REFERENCE_BOARD)
    assert depth >= 1


def test_bitboard_moves_match_reference_board_engine() -> None:
    boards = (
        REFERENCE_BOARD,
        (
            (1, 1, 1, 1),
            (3, 0, 3, 3),
            (7, 6, 5, 4),
            (0, 2, 2, 0),
        ),
    )
    for board in boards:
        bits = _board_to_bits(board)
        assert _transpose_bits(_transpose_bits(bits)) == bits
        for direction in ("left", "right", "up", "down"):
            expected = move_board(board, direction)
            moved, score = _move_bits(bits, direction)
            assert moved == _board_to_bits(expected.board)
            assert score == expected.score


def test_auto_player_plans_from_reference_capture() -> None:
    player = Auto2048Player(ASSET, time_budget_ms=500, max_depth=2)
    decision = player.plan(ASSET.read_bytes())
    assert decision.scan.board == REFERENCE_BOARD
    assert decision.direction in available_moves(REFERENCE_BOARD)
    for _ in range(3):
        pending = player.plan(ASSET.read_bytes())
        assert pending.scan.board == REFERENCE_BOARD
        assert pending.direction is None
        assert pending.waiting
        assert pending.depth == 0
    with pytest.raises(RuntimeError, match="chưa cập nhật"):
        player.plan(ASSET.read_bytes())
