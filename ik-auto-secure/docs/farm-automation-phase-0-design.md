# Farm automation — thiết kế Phase 0

Tài liệu yêu cầu gốc được lưu nguyên văn tại `docs/farm-automation-web-spec.md`.
Phase 0 khóa domain contract trước khi tạo web UI, broker hoặc adapter LDPlayer.

## Ranh giới triển khai

```text
Web control plane -> validated command -> Windows Agent -> bounded workflow -> DeviceAdapter
Windows Agent -> sequenced event/checkpoint -> control plane -> dashboard read model
```

Browser không được có API tap, swipe, ADB hoặc handle emulator. Agent chỉ nhận
ý định đã được validate và tự quyết định input từ frame mới nhất.

## Aggregate và bất biến

`FarmRun` là aggregate điều phối; `DeviceRun` là aggregate duy nhất được phép
giữ gameplay lease cho một thiết bị.

- Một `deviceId` chỉ có một lease active.
- Mỗi mutation cần `expectedVersion`; command cũ bị từ chối.
- Một `commandId` chỉ được apply một lần trên agent.
- `sequence` tăng đơn điệu theo `deviceRun`; control plane chỉ nhận snapshot
  có sequence lớn hơn snapshot đã lưu.
- Checkpoint chỉ khôi phục metadata. Sau restart, runtime luôn về `preflight`.
- Mỗi cycle hoàn tất tối đa một dispatch đã được xác minh.

## Contract tối thiểu

```ts
type DeviceRunState =
  | 'queued' | 'preflight' | 'ready' | 'running' | 'waiting'
  | 'recovering' | 'quarantined' | 'stopped';

type FarmCommand = {
  commandId: string;
  kind: 'farm.start' | 'farm.stop' | 'device.pause' | 'device.resume'
      | 'device.quarantine' | 'device.recover';
  agentId: string;
  deviceIds: string[];
  expectedVersion: number;
  deadlineAt: string;
  actorId: string;
  payload: Record<string, unknown>;
};

type DeviceSnapshot = {
  runId: string;
  deviceId: string;
  sequence: number;
  state: DeviceRunState;
  nextAttemptAt?: string;
  cycleCount: number;
  consecutiveTechnicalFailures: number;
  operation: string;
};
```

## Bounded cycle

```text
preflight
  -> detect fresh frame
  -> require known WorldMap and valid roster
  -> pick allowed ready team
  -> select resource/level with bounded fallback
  -> verify resource popup
  -> verify team selection
  -> guarded dispatch
  -> waiting(nextAttemptAt)
```

`business outcome` (no ready team/resource, storage full, resource expired) chỉ
đặt lịch fallback/retry. `technical failure` (ADB/capture/matcher timeout,
watchdog) mới đi vào recovery ladder và circuit breaker.

## Input guard bắt buộc

Mọi input production dùng cùng một primitive. Đây là logic đầu tiên cần hiện
thực hóa trong Agent trước bất kỳ workflow nào:

```text
guardedTap(templateId, expectedState):
  cancellation.throwIfCancelled()
  frame = captureFreshFrame(maxAge=500ms)
  assert detectState(frame) == expectedState
  match = vision.match(frame, templateId)
  assert match.confidence >= threshold && match.bounds.isValid
  adapter.tap(match.bounds.center)
  after = captureFreshFrame(maxAge=500ms)
  assert verifyExpectedPostAction(after)
```

Không có fallback tap theo tọa độ cố định; `Unknown`, stale frame, match yếu,
cancel, timeout hay post-action không xác minh đều kết thúc bước hiện tại mà
không gửi thêm input mù.

## State transition policy

| From | Event | To | Ghi checkpoint |
| --- | --- | --- | --- |
| `queued` | agent accepts start | `preflight` | Có |
| `preflight` | known state + roster | `ready` | Có |
| `ready` | gameplay lease granted | `running` | Có |
| `running` | verified dispatch/business outcome | `waiting` | Có |
| `waiting` | `nextAttemptAt` reached | `preflight` | Có |
| any active | technical failure | `recovering` | Có |
| `recovering` | policy exhausted | `quarantined` | Có |
| any | cancel acknowledged | `stopped` | Có |

## Thứ tự triển khai tiếp theo

1. ✅ Tạo shared contract package và state-transition tests.
2. ✅ Tạo Windows Agent local-only với `FakeDeviceAdapter`; hoàn tất lease,
   cancellation, checkpoint atomic và event sequence.
3. Cài `GuardedInput` + `VisionEngine` interface cùng regression fixtures.
4. Port one bounded cycle bằng fake adapter trước, sau đó mới LDPlayer/ADB.
5. Sau khi Agent contract ổn định, xây API/control plane, broker và dashboard.

## Acceptance criteria Phase 0

- Test chứng minh command redelivery không tạo hai device loop.
- Test chứng minh snapshot sequence cũ không ghi đè sequence mới.
- Test chứng minh business outcome không gọi recovery.
- Test chứng minh mọi tap bị từ chối nếu frame stale/state unknown/match yếu,
  và post-action không verify.
- Test chứng minh stop giải phóng lease, hủy wait và ghi checkpoint `stopped`.
