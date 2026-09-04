from __future__ import annotations

from ik_chrome_auto.farm_launch_policy import FarmLaunchPolicy

GIB = 1_073_741_824


def test_large_workstation_uses_conservative_three_profile_batches() -> None:
    policy = FarmLaunchPolicy.for_total_memory(96 * GIB)

    assert policy.batch_size == 3
    assert policy.profile_interval_seconds == 3.0
    assert policy.batch_pause_seconds == 15.0
    assert policy.resource_constrained_interval_seconds == 8.0
    assert policy.min_available_memory_bytes >= 16 * GIB


def test_mid_range_machine_keeps_the_original_stagger() -> None:
    policy = FarmLaunchPolicy.for_total_memory(32 * GIB)

    assert policy.batch_size == 3
    assert policy.profile_interval_seconds == 3.5
    assert policy.batch_pause_seconds == 18.0


def test_low_memory_machine_opens_one_profile_per_batch() -> None:
    policy = FarmLaunchPolicy.for_total_memory(16 * GIB)

    assert policy.batch_size == 1
    assert policy.profile_interval_seconds == 5.0
    assert policy.batch_pause_seconds == 12.0
    assert policy.resource_constrained_interval_seconds == 8.0


def test_resource_guard_blocks_low_memory_and_high_cpu() -> None:
    policy = FarmLaunchPolicy.for_total_memory(96 * GIB)

    assert policy.resource_block_reason(
        available_memory_bytes=10 * GIB,
        memory_load_percent=70,
        profile_cpu_percent=10,
    ).startswith("RAM trống")
    assert policy.resource_block_reason(
        available_memory_bytes=30 * GIB,
        memory_load_percent=60,
        profile_cpu_percent=85,
    ).startswith("CPU Chrome")
    assert policy.resource_block_reason(
        available_memory_bytes=30 * GIB,
        memory_load_percent=60,
        profile_cpu_percent=40,
        gpu_utilization_percent=90,
    ).startswith("GPU đang ở")
    assert policy.resource_block_reason(
        available_memory_bytes=30 * GIB,
        memory_load_percent=60,
        profile_cpu_percent=40,
        gpu_utilization_percent=40,
    ) is None


def test_timeout_budget_includes_all_batch_pauses() -> None:
    policy = FarmLaunchPolicy.for_total_memory(96 * GIB)

    timeout = policy.estimated_timeout_seconds(50, 90)

    assert timeout >= 90 + (49 * policy.resource_constrained_interval_seconds) + 180
