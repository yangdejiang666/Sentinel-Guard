# 硬件与 AI 板卡接入手册

## 1. 接入前提

平台把“报文已经到达网关”和“硬件已经完成动作”严格区分。接入设备前准备：

- 为每个站点建立唯一 `SENTINEL_SITE_ID`。
- live 模式配置不可预测的 `SENTINEL_DEVICE_TOKEN` 和 `SENTINEL_CONTROL_TOKEN`。
- 为每个设备维护稳定的 `device_id`，重启时只更新 `boot_id`，不要更换设备 ID。
- 所有设备时钟使用 UTC，并确保误差在 `SENTINEL_CLOCK_TOLERANCE_SECONDS` 内。
- 每个数据流维护单调递增的 `sequence`；重启换新的 `boot_id`。
- 先使用独立 live 数据库，不要把 live 和 simulated 数据写进同一个运行库。

下文的 `lab-a`、令牌、事件 ID、Boot ID 和时间戳是格式示例。实际接入时站点必须与服务配置一致，事件 ID 必须唯一，时间戳必须使用当前真实采集时刻；不要原样发送固定日期示例，否则会触发过期、未来或重复校验。

## 2. REST 接入

所有设备 HTTP 请求使用：

```http
X-Device-Token: <SENTINEL_DEVICE_TOKEN>
Content-Type: application/json
```

### 2.1 ESP32-S3 遥测

发送到 `POST /api/v1/ingest/telemetry`。每个指标是一条记录，推荐 1 秒或更长周期；设备必须保留序列和 Boot ID。

```json
{
  "eventId": "esp32-boot7-temperature-1001",
  "siteId": "lab-a",
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
}
```

固定指标：

| metric | unit | 约束 |
| --- | --- | --- |
| `temperature` | `°C` | -40 到 125 |
| `humidity` | `%` | 0 到 100 |
| `light` | `lux` | 0 到 200000 |

扩展指标可以直接进入通用遥测表，但必须使用小写下划线名称、单位和有限数值。前端设备质量页会自动展示新指标，安全 FSM 不会自动使用未经规则登记的指标。

### 2.2 K230 / Orange Pi 检测

发送到 `POST /api/v1/ingest/detections`。K230 使用 `deviceId=k230-001`、`sourceView=k230`、`cameraId=k230-001`；Orange Pi 主视角使用 `deviceId=orange-pi-001`、`sourceView=main`、`cameraId=camera-main-001`。

```json
{
  "eventId": "det-k230-boot3-4001",
  "siteId": "lab-a",
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
}
```

`bbox` 是归一化坐标，不是像素。没有对应视频帧时，前端只能展示检测元数据；不会把框伪装成真实画面框。

### 2.3 心跳

发送到 `POST /api/v1/ingest/heartbeat`：

```json
{
  "siteId": "lab-a",
  "deviceId": "esp32-s3-001",
  "eventTime": "2026-10-09T00:00:00.000Z",
  "bootId": "boot-7",
  "sourceMode": "live"
}
```

心跳不能用重复或倒退的时间刷新在线状态；过期心跳最多标记为延迟，不能伪装成新鲜数据。

### 2.4 视频帧

发送到 `POST /api/v1/ingest/video/main` 或 `POST /api/v1/ingest/video/k230`，查询参数包括 `device_id`、`frame_id`、`event_time` 和 `source_mode=live`，请求体为 `image/jpeg`。单帧上限 2 MiB，服务端检查 JPEG 起止签名、来源设备、时间新鲜度和通道能力。帧只在内存中缓存最近窗口，不写入 SQLite。

检测中的 `frame_id` 必须与视频帧完全一致，才能在前端显示真实检测框。

## 3. MQTT 接入

配置 `SENTINEL_MQTT_HOST` 后，live 模式会启动 MQTT 适配器。主题固定为：

```text
sentinel/{site}/telemetry/{device}
sentinel/{site}/detections/{device}
sentinel/{site}/heartbeat/{device}
sentinel/{site}/actuator/feedback/{device}
sentinel/{site}/actuator/command/{device}
```

输入主题中的站点和设备必须与 payload 的 `siteId` / `deviceId` 一致。当前适配器订阅前四类主题，命令主题由网关发布。

MQTT 约束：

- payload 必须是 JSON object，最大 64 KiB。
- topic 设备 ID 和 payload 设备 ID 不一致时拒绝。
- retained message 不得建立实时安全状态；接收 retained 输入会拒绝并审计。
- 必须携带 `eventTime`、`sequence`、`bootId`（心跳至少携带 `eventTime` 和 `bootId`）。
- 乱序、重复、未来时间、过期数据只能进入质量/审计路径，不能替换有效最新值。
- MQTT 断开会显示 degraded/fallback 状态，不能把断网解释成环境正常。

## 4. 执行器命令和回执

网关命令发布到：

```text
sentinel/{site}/actuator/command/actuator-001
```

执行器必须把回执发送到：

```text
sentinel/{site}/actuator/feedback/actuator-001
```

回执示例：

```json
{
  "siteId": "lab-a",
  "commandId": "cmd-123",
  "deviceId": "actuator-001",
  "status": "completed",
  "eventTime": "2026-10-09T00:00:01.200Z",
  "executionMessage": "isolated relay channel 1 energized",
  "sourceMode": "live"
}
```

允许状态为 `acknowledged`、`completed`、`failed`。执行器必须在 `SENTINEL_COMMAND_TIMEOUT_SECONDS` 内反馈；超时后网关会记录 timeout，并明确说明不能假设硬件已经动作。`completed` 前端和审计才会显示“已完成”。

## 5. Fallback 和本地执行

当前 `fallback_active` 只表示 MQTT/节点健康发生降级；`local_fallback_ready` 只有在真正注册本地命令发布适配器时才为 true。当前仓库没有 GPIO adapter，默认因此为 false。不要因为 API 中存在 `local_command_publisher` 接口，就把本地 GPIO 描述成已经硬连接。

申报书中的“断网单兵闭环”需要在 Orange Pi 上完成独立的本地视觉、GPIO、安全联锁和掉电策略验收后，才能称为已实现。当前平台只保留接口和状态展示。

## 6. 接板验收清单

- [ ] 设备 ID、站点 ID、令牌和 TLS/局域网策略已确认。
- [ ] UTC 时间同步、`sequence`、`boot_id` 已验证。
- [ ] 断网、重复、乱序、重启、过期数据测试通过。
- [ ] K230/Orange Pi 检测与实际视频帧 ID 对齐。
- [ ] 模型版本、输入分辨率和实际 FPS 已记录，不使用申报书目标值代替实测。
- [ ] 执行器 acknowledged/completed/failed/timeout 全链路通过。
- [ ] 继电器隔离、续流、急停、保险、断电安全态已由硬件负责人验收。
- [ ] 误报、漏报、延迟、温升速率和现场环境样本已形成测试报告。
- [ ] 只有完成上述验收后，才评估 `SENTINEL_ALLOW_ACTUATION` 和 `SENTINEL_AUTO_ACTUATION`。
