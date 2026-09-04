"""Adaptive startup policy for opening many browser farm profiles safely."""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil

_GIB = 1_073_741_824


@dataclass(frozen=True, slots=True)
class FarmLaunchPolicy:
    batch_size: int
    profile_interval_seconds: float
    batch_pause_seconds: float
    min_available_memory_bytes: int
    max_memory_load_percent: float = 82.0
    max_profile_cpu_percent: float = 80.0
    max_gpu_utilization_percent: float = 80.0
    resource_pause_timeout_seconds: float = 180.0
    resource_constrained_interval_seconds: float = 8.0

    @classmethod
    def for_total_memory(cls, total_memory_bytes: int) -> "FarmLaunchPolicy":
        """Choose a fast stagger while retaining the live resource guard."""
        total = max(1, int(total_memory_bytes))
        if total >= 64 * _GIB:
            return cls(
                # Real startup logs show that a profile normally reaches READY
                # in roughly two to three seconds.  A one-second stagger lets
                # Chrome overlap process creation without creating one giant
                # WebGL burst.
                batch_size=6,
                profile_interval_seconds=1.0,
                batch_pause_seconds=5.0,
                min_available_memory_bytes=max(16 * _GIB, int(total * 0.18)),
                max_memory_load_percent=80.0,
                max_profile_cpu_percent=78.0,
                max_gpu_utilization_percent=84.0,
                resource_constrained_interval_seconds=4.0,
            )
        if total >= 32 * _GIB:
            return cls(
                batch_size=4,
                profile_interval_seconds=1.25,
                batch_pause_seconds=6.0,
                min_available_memory_bytes=max(6 * _GIB, int(total * 0.20)),
                max_memory_load_percent=80.0,
                max_profile_cpu_percent=76.0,
                max_gpu_utilization_percent=82.0,
                resource_constrained_interval_seconds=4.0,
            )
        return cls(
            batch_size=2,
            profile_interval_seconds=2.0,
            batch_pause_seconds=8.0,
            min_available_memory_bytes=max(4 * _GIB, int(total * 0.25)),
            max_memory_load_percent=78.0,
            max_profile_cpu_percent=72.0,
            max_gpu_utilization_percent=76.0,
            resource_constrained_interval_seconds=5.0,
        )

    def estimated_timeout_seconds(self, profile_count: int, startup_timeout_seconds: float) -> float:
        count = max(0, int(profile_count))
        batches = ceil(count / self.batch_size) if count else 0
        launch_time = max(0, count - 1) * self.profile_interval_seconds
        pauses = max(0, batches - 1) * self.batch_pause_seconds
        normal_duration = launch_time + pauses
        # Budget for the slower resource-constrained path so a healthy but
        # temporarily busy machine does not time out prematurely.
        constrained_duration = max(0, count - 1) * self.resource_constrained_interval_seconds
        return max(30.0, startup_timeout_seconds) + max(
            normal_duration,
            constrained_duration,
        ) + self.resource_pause_timeout_seconds

    def resource_block_reason(
        self,
        *,
        available_memory_bytes: int,
        memory_load_percent: float,
        profile_cpu_percent: float,
        gpu_utilization_percent: float | None = None,
    ) -> str | None:
        if available_memory_bytes < self.min_available_memory_bytes:
            return f"RAM trống chỉ còn {available_memory_bytes / _GIB:.1f} GB"
        if memory_load_percent >= self.max_memory_load_percent:
            return f"RAM đã dùng {memory_load_percent:.0f}%"
        if profile_cpu_percent >= self.max_profile_cpu_percent:
            return f"CPU Chrome đang ở {profile_cpu_percent:.0f}%"
        if (
            gpu_utilization_percent is not None
            and gpu_utilization_percent >= self.max_gpu_utilization_percent
        ):
            return f"GPU đang ở {gpu_utilization_percent:.0f}%"
        return None
