# API 与数据契约示例

下面的示例默认服务运行在 `http://127.0.0.1:8000`。未配置令牌的默认模拟模式不需要 token；只要配置了令牌，模拟模式也会验证。live 模式必须配置令牌，设备输入需要 `X-Device-Token`，控制请求需要 `X-Control-Token`。

live 示例必须先关闭模拟模式，使用独立 live 数据库，并把站点、设备令牌和控制令牌替换成实际值。示例里的 `2026-10-09T00:00:00...Z` 仅用于说明字段格式：每次发送都要替换为采集时的当前 UTC 时间，否则会被判为过期或未来输入，不进入实时决策。`eventId` 每条事件必须唯一，`sequence` 在同一设备数据流与 `bootId` 内递增。

查询示例用 Python 标准库美化 JSON，不需要另行安装 `jq`；也可以直接移除管道查看原始响应。PowerShell 使用 `curl.exe`，避免旧版本 PowerShell 的 `curl` 别名行为。

## 1. 健康、配置和首屏快照

```bash
curl -s http://127.0.0.1:8000/api/v1/health | uv run python -m json.tool
curl -s http://127.0.0.1:8000/api/v1/config | uv run python -m json.tool
curl -s http://127.0.0.1:8000/api/v1/overview | uv run python -m json.tool
curl -s http://127.0.0.1:8000/api/v1/video/channels | uv run python -m json.tool
```

`overview` 是前端首屏的权威快照，包含当前 FSM、数据可信度、设备健康、最新遥测、最新检测、事件、状态流转、命令和视频通道。

## 2. 查询历史和审计

```bash
curl -s 'http://127.0.0.1:8000/api/v1/telemetry/history?metric=temperature&limit=120' | uv run python -m json.tool
curl -s 'http://127.0.0.1:8000/api/v1/telemetry/history?metric=humidity&device_id=esp32-s3-001&source_mode=simulated' | uv run python -m json.tool
curl -s 'http://127.0.0.1:8000/api/v1/detections?limit=50' | uv run python -m json.tool
curl -s 'http://127.0.0.1:8000/api/v1/events?limit=100' | uv run python -m json.tool
curl -s 'http://127.0.0.1:8000/api/v1/state/transitions?limit=100' | uv run python -m json.tool
curl -s 'http://127.0.0.1:8000/api/v1/commands?limit=100' | uv run python -m json.tool
```

历史结果同时包含 `event_time`、`ingest_time`、`quality`、`quality_reason`、`source_mode`、`sequence`、`boot_id` 和 `event_id`，方便区分采集延迟与展示延迟。

## 3. live 遥测接入

```bash
curl -i -X POST http://127.0.0.1:8000/api/v1/ingest/telemetry \
  -H 'Content-Type: application/json' \
  -H 'X-Device-Token: replace-with-device-token' \
  -d '{
    "eventId": "esp32-boot7-temperature-1001",
    "siteId": "demo-site",
    "deviceId": "esp32-s3-001",
    "nodeType": "ESP32-S3",
    "channel": "environment",
    "metric": "temperature",
    "value": 23.6,
    "unit": "°C",
    "eventTime": "2026-10-09T00:00:00.000Z",
    "sequence": 1001,
    "bootId": "boot-7",
    "quality": "valid",
    "sourceMode": "live"
  }'
```

服务端会以当前接收时间覆盖 `ingest_time`，并在服务端判定实际质量。重复 event ID、过期、未来时间、乱序或不符合设备能力的数据会返回记录或错误，但不会进入安全决策。

## 4. live 检测接入

```bash
curl -i -X POST http://127.0.0.1:8000/api/v1/ingest/detections \
  -H 'Content-Type: application/json' \
  -H 'X-Device-Token: replace-with-device-token' \
  -d '{
    "eventId": "det-k230-boot3-4001",
    "siteId": "demo-site",
    "deviceId": "k230-001",
    "cameraId": "k230-001",
    "sourceView": "k230",
    "target": "fire",
    "confidence": 0.87,
    "bbox": [0.32, 0.21, 0.22, 0.38],
    "frameId": "k230-boot3-frame-4001",
    "eventTime": "2026-10-09T00:00:00.100Z",
    "sequence": 4001,
    "bootId": "k230-boot-3",
    "quality": "valid",
    "sourceMode": "live",
    "modelVersion": "fire-v1-int8"
  }'
```

## 5. 心跳接入

```bash
curl -i -X POST http://127.0.0.1:8000/api/v1/ingest/heartbeat \
  -H 'Content-Type: application/json' \
  -H 'X-Device-Token: replace-with-device-token' \
  -d '{
    "siteId": "demo-site",
    "deviceId": "esp32-s3-001",
    "eventTime": "2026-10-09T00:00:00.000Z",
    "bootId": "boot-7",
    "sourceMode": "live"
  }'
```

## 6. JPEG 视频帧

```bash
curl -i -X POST 'http://127.0.0.1:8000/api/v1/ingest/video/main?device_id=camera-main-001&frame_id=mf937-frame-100&event_time=2026-10-09T00%3A00%3A00.100Z&source_mode=live' \
  -H 'Content-Type: image/jpeg' \
  -H 'X-Device-Token: replace-with-device-token' \
  --data-binary @frame.jpg

curl -s 'http://127.0.0.1:8000/api/v1/video/main/frame?frame_id=mf937-frame-100' -o frame-response.jpg
```

服务端会限制 JPEG 大小并检查签名；视频通道的 `frame_id` 必须和检测记录一致，前端才会绘制真实框。

## 7. 控制命令和事件确认

```bash
curl -i -X POST http://127.0.0.1:8000/api/v1/commands \
  -H 'Content-Type: application/json' \
  -H 'X-Control-Token: replace-with-control-token' \
  -d '{
    "commandType": "fan_on",
    "reason": "operator test",
    "requestedBy": "operator",
    "requestId": "operator-test-001"
  }'

curl -i -X POST http://127.0.0.1:8000/api/v1/events/event-abc/acknowledge \
  -H 'Content-Type: application/json' \
  -H 'X-Control-Token: replace-with-control-token' \
  -d '{"acknowledgedBy":"operator"}'
```

模拟模式下命令会生成 completed 的模拟审计记录，但执行说明明确写为“未驱动物理硬件”。live 模式下，控制命令只是登记/发布，必须等待执行器回执。

## 8. 执行器回执

```bash
curl -i -X POST http://127.0.0.1:8000/api/v1/ingest/command-feedback \
  -H 'Content-Type: application/json' \
  -H 'X-Device-Token: replace-with-device-token' \
  -d '{
    "siteId": "demo-site",
    "commandId": "cmd-123",
    "deviceId": "actuator-001",
    "status": "completed",
    "eventTime": "2026-10-09T00:00:01.200Z",
    "executionMessage": "isolated relay channel 1 energized",
    "sourceMode": "live"
  }'
```

允许回执状态为 `acknowledged`、`completed`、`failed`。超时后晚到的完成回执不会把已过期命令重新宣称为完成。

## 9. MQTT 报文示例

主题：

```text
sentinel/demo-site/telemetry/esp32-s3-001
```

payload 使用与 REST 相同的 camelCase 或 snake_case 契约；topic 里的设备 ID 和 payload 中的 `deviceId` 必须一致。不要设置 retained flag 来建立实时安全状态。

## 10. WebSocket 消息示例

```javascript
const socket = new WebSocket('ws://127.0.0.1:8000/ws');
socket.onmessage = (event) => {
  const message = JSON.parse(event.data);
  console.log(message.type, message.stream_id, message.stream_sequence, message.data);
};
```

第一条消息是 `snapshot`。后续可能是 `telemetry`、`detection`、`event`、`state_transition`、`command`、`command_feedback`、`health`、`video`、`ping`。客户端发现 `stream_sequence` 缺口或收到 `resync_required` 时，应重新请求权威 REST 快照。
