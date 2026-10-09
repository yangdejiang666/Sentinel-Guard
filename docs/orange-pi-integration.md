# 香橙派 (Orange Pi 3B / RK3588) 边缘 AI 推理通信对接与端到端验证手册

本手册专门面向香橙派端软件开发、AI 模型部署（RKNN）以及嵌入式通信对接人员。文档基于智卫 Sentinel 平台的整体架构（参见 [docs/architecture.md](file:///d:/Sentinel‑Guard/docs/architecture.md) 与 [docs/hardware-integration.md](file:///d:/Sentinel‑Guard/docs/hardware-integration.md)），详细说明香橙派与 K230 视觉前端的协同机制、香橙派向监测平台传输数据的通信协议、数据契约、字段约束、链路打通步骤与端到端联调验证方法。

---

## 1. 硬件职责边界与架构定位

### 1.1 明确硬件职责分工
在智卫 Sentinel 系统的异构协同架构中，各硬件节点有严格的能力边界：
- **ESP32-S3 端（传感器与硬件控制中心）**：
  - **真正的物理 GPIO 与现场传感器均接在 ESP32 端**（包括温湿度传感器、光照传感器、继电器、排烟风扇、声光报警器等）。
  - 负责环境遥测数据的采集上报，以及物理动作的执行与回执反馈。
- **K230 端（前端视觉采集节点）**：
  - 系统中唯一的物理摄像头传感器接入端，负责原始视频流捕获。
  - 将获取到的图像/视频数据帧输送给香橙派。
- **香橙派（Orange Pi 3B / RK3588，设备代号 `orange-pi-001`）**：
  - **定位为纯粹的高算力 AI 推理与数据处理核心，不执行任何物理动作，不接物理执行器 GPIO**。
  - 接收来自 K230 的视频数据帧，调用板载 NPU（RKNN 引擎）执行高精度 AI 检测（火情、烟雾、人员）。
  - 将 AI 检测元数据（坐标框、置信度、目标类别）与关键视频帧上传至智卫 Sentinel 平台。
  - 向平台发送自身节点心跳，维持健康活跃状态。

### 1.2 整体数据拓扑与流向
```text
┌───────────────────────┐
│  K230 摄像头采集端    │
│  (唯一物理视觉源)     │
└──────────┬────────────┘
           │ 视频数据帧 (局域网/串行/网络流)
           ▼
┌──────────────────────────────────────────────┐
│  香橙派 (orange-pi-001) - 纯 AI 推理与运算   │
│  ├─ 接收 K230 视频数据帧                      │
│  ├─ RKNN NPU 视觉目标检测 (fire/smoke/person) │
│  ├─ 节点保活心跳 (Heartbeat)                  │
│  └─ 视频关键帧中继 (JPEG Frame)              │
│  (注: 不承担任何 GPIO 控制与物理执行动作)     │
└──────────────────────┬───────────────────────┘
                       │ 协议: HTTP REST (主) / MQTT (备)
                       │ 认证: X-Device-Token
                       ▼
┌──────────────────────────────────────────────┐
│  智卫 Sentinel 平台服务端 (FastAPI)           │
│  ├─ 数据质检 (质量标签、时钟容差、序列游标)   │
│  ├─ 多模态 FSM 状态机 (NORMAL/WATCH/ALERT/DANGER)
│  └─ SQLite 数据持久化与审计                  │
└──────────────────────┬───────────────────────┘
                       │ 协议: WebSocket (/ws)
                       ▼
┌──────────────────────────────────────────────┐
│  前端控制台面板 (Web Dashboard)               │
│  ├─ 态势总览 (实时风险等级与环境/视觉证据)    │
│  ├─ 实时监控 (视频预览与 AI 真实检测框贴合)   │
│  └─ 设备与质量 (在线/延迟状态与数据完整性)    │
└──────────────────────────────────────────────┘
```

---

## 2. 通信协议与支持方式

平台针对香橙派的数据传输设计了标准协议通路：

| 协议类型 | 香橙派传输的数据类型 | 认证机制 | 协议特征与边界 |
| :--- | :--- | :--- | :--- |
| **HTTP REST**<br>*(推荐首选)* | • 节点心跳 (`/heartbeat`)<br>• AI 检测元数据 (`/detections`)<br>• JPEG 视频关键帧 (`/video/k230`) | 请求头：<br>`X-Device-Token: <token>` | • **零外部依赖**：直接调用 FastAPI 原生路由，无需额外 Broker。<br>• **天然支持大文件**：单帧支持最高 2 MiB 的二进制 JPEG 上传。<br>• **排错直观**：支持 curl/Python 快速重现和脚本单步验证。 |
| **MQTT**<br>*(局域网扩展)* | • 节点心跳<br>• AI 检测元数据 | 连接凭据：<br>用户名 / 密码 (可选) | • 需局域网部署 Mosquitto 或 EMQX Broker。<br>• **严禁走 MQTT 发送视频帧**（单条报文严格限制 64 KiB 以内）。<br>• 实时安全状态禁止 Retained 消息。 |

> **建议对接策略**：
> 第一阶段**全力使用 HTTP REST 跑通端到端链路**（开发最快、排错最直接）；后续如需多节点低带宽广播与双向解耦，再切入 MQTT 遥测扩展。

---

## 3. 详细字段与数据契约说明

数据契约兼容 `camelCase`（驼峰命名）与 `snake_case`（下划线命名）。所有时间戳必须为 **ISO-8601 UTC 格式**（以 `Z` 结尾），例如 `2026-10-09T04:30:00.000Z`。

### 3.1 节点心跳 (Heartbeat)
香橙派定时（建议每 2~3 秒）向平台发送自身心跳。若超过 `SENTINEL_HEARTBEAT_TIMEOUT_SECONDS`（默认 8 秒）未收到有效心跳，平台会将香橙派置为 `delayed` 或 `offline`。

- **请求路径**：`POST /api/v1/ingest/heartbeat`
- **请求头**：
  - `Content-Type: application/json`
  - `X-Device-Token: <SENTINEL_DEVICE_TOKEN>`
- **请求体字段**：

| 字段名 (Camel / Snake) | 类型 | 必填 | 约束与取值示例 | 业务含义说明 |
| :--- | :--- | :---: | :--- | :--- |
| `siteId` / `site_id` | string | 是 | 必须与服务端配置完全一致，如 `"demo-site"` | 部署站点标识，跨站点数据直接拒绝 |
| `deviceId` / `device_id` | string | 是 | 固定为 `"orange-pi-001"` | 设备唯一标识 |
| `bootId` / `boot_id` | string | 是 | 每次开机/重启生成唯一 ID，如 `"opi-boot-01"` | 启动会话标识，用于序列号游标重置 |
| `eventTime` / `event_time` | string | 是 | 实时 UTC 时间，如 `"2026-10-09T04:30:00.000Z"` | 采集时刻，偏差超过 5 秒将拒绝 |
| `sourceMode` / `source_mode` | string | 否 | `"live"`（默认）或 `"simulated"` | 数据来源模式，真机联调必须为 `"live"` |

- **报文示例**：
```json
{
  "siteId": "demo-site",
  "deviceId": "orange-pi-001",
  "bootId": "opi-boot-20261009-001",
  "eventTime": "2026-10-09T04:30:00.000Z",
  "sourceMode": "live"
}
```

---

### 3.2 主 AI 检测元数据 (Vision Detection)
香橙派 NPU（RKNN 引擎）推理 K230 输入帧后，将检测元数据实时推送至平台。

- **请求路径**：`POST /api/v1/ingest/detections`
- **请求头**：
  - `Content-Type: application/json`
  - `X-Device-Token: <SENTINEL_DEVICE_TOKEN>`
- **请求体字段**：

| 字段名 (Camel / Snake) | 类型 | 必填 | 约束与取值示例 | 业务含义说明 |
| :--- | :--- | :---: | :--- | :--- |
| `eventId` / `event_id` | string | 是 | 唯一字符串，如 `"det-opi-seq1001"` | 本次检测事件全局唯一 ID，防重复攻击 |
| `siteId` / `site_id` | string | 是 | 必须与平台一致，如 `"demo-site"` | 站点标识 |
| `deviceId` / `device_id` | string | 是 | 固定为 `"orange-pi-001"` | 执行主推理的设备 ID（香橙派） |
| `cameraId` / `camera_id` | string | 是 | 固定为 `"k230-001"` | 画面源头摄像头 ID（K230） |
| `sourceView` / `source_view` | string | 是 | 固定为 `"k230"` | 视角通道名称，当前系统仅接受 `"k230"` |
| `target` | string | 是 | `"fire"` \| `"smoke"` \| `"person"` \| `"none"` | 检测到的目标分类 |
| `confidence` | float | 是 | `0.0` ~ `1.0`，例如 `0.88` | 模型置信度得分 |
| `bbox` | list[float] | 是 | 4 个浮点数：`[x, y, width, height]` | **归一化矩形框**（范围 0.0~1.0），非绝对像素值 |
| `frameId` / `frame_id` | string | 否 | 如 `"k230-frame-5001"` | **关联的视频帧 ID**，与上传的视频帧匹配方可在前端贴合画框 |
| `eventTime` / `event_time` | string | 是 | 当前 UTC 时间 | 算法实际产生检测的时刻 |
| `sequence` | integer | 是 | 从 0 开始单调递增整数，如 `1001` | 同一 bootId 内严格递增的序号，防乱序重放 |
| `bootId` / `boot_id` | string | 是 | 与心跳的 bootId 保持一致 | 启动会话 ID |
| `quality` | string | 否 | 固定传 `"valid"` | 数据质量初始标记，平台会做二次质检 |
| `sourceMode` / `source_mode` | string | 否 | 固定传 `"live"` | 来源模式 |
| `modelVersion` / `model_version` | string | 是 | 如 `"fire-yolo-rknn-v1.0"` | 算法模型与权重版本 |

- **报文示例**：
```json
{
  "eventId": "det-opi-998811",
  "siteId": "demo-site",
  "deviceId": "orange-pi-001",
  "cameraId": "k230-001",
  "sourceView": "k230",
  "target": "fire",
  "confidence": 0.89,
  "bbox": [0.32, 0.25, 0.22, 0.40],
  "frameId": "k230-frame-5001",
  "eventTime": "2026-10-09T04:30:00.120Z",
  "sequence": 5001,
  "bootId": "opi-boot-20261009-001",
  "quality": "valid",
  "sourceMode": "live",
  "modelVersion": "fire-rknn-v1.0"
}
```

---

### 3.3 JPEG 视频帧上传 (Video Ingest)
K230 将视频流帧传给香橙派后，香橙派负责将关键帧同步中继到平台，以便前端实时预览画面并叠加热区与检测框：

- **请求路径**：`POST /api/v1/ingest/video/k230`
- **URL 查询参数 (Query Parameters)**：
  - `device_id`：`k230-001`（原始图像源自 K230 摄像头）
  - `frame_id`：当前帧唯一 ID，如 `k230-frame-5001`（**必须与 AI 检测元数据中的 `frameId` 完全对齐**）
  - `event_time`：当前 UTC 时间字符串
  - `source_mode`：`live`
- **请求头**：
  - `Content-Type: image/jpeg`
  - `X-Device-Token: <SENTINEL_DEVICE_TOKEN>`
- **请求体 (Body)**：原始二进制 JPEG 数据（单帧体积不得超过 2 MiB，文件头必须以 `0xFF 0xD8` 起始并以 `0xFF 0xD9` 结尾）。

---

## 4. 平台核心安全与质检刚性规则

为确保对接代码不被平台服务端拒绝，对接人员**必须严格遵守**以下规则：

1. **时钟必须严格同步（误差 < 5 秒）**：
   - 平台的 `SENTINEL_CLOCK_TOLERANCE_SECONDS` 默认为 5 秒。
   - 严禁在脚本中写死固定的假时间！香橙派操作系统必须开启 NTP 时钟同步，发送实时 UTC 时间。超前 5 秒判定为“未来时间（future）”，落后判定为“过期（stale）”，均会被判定为无效数据。
2. **序号单调递增（Monotonic Sequence）**：
   - 在同一个 `bootId` 内，每一次发送检测数据 `sequence` 必须大于前一次。
   - 若序号不递增或倒退，服务端判定为 `out_of_order_sequence` 并记录审计，拒绝输入。
   - 香橙派设备重启或算法进程重启后，**必须生成全新的 `bootId`**，此时 `sequence` 可以从 0 或 1 重新起算。
3. **坐标归一化边界（Normalized Bounding Box）**：
   - `bbox` 格式固定为 `[x, y, width, height]`。
   - 数值范围必须在 `0.0 ~ 1.0` 之间，且 `x + width <= 1.0`，`y + height <= 1.0`。若传了绝对像素坐标（如 640, 480），接口会直接报 HTTP 422 校验失败。
4. **视频帧与检测框关联机制**：
   - 平台只有在当前缓存中找到了相同 `frame_id` 的视频帧时，前端监控画面才会绘制真实 AI 框；
   - 若只有检测数据而没有对应帧，前端仅在“AI 元数据卡片”中显示文本，不会在画面上绘制虚假模拟框。

---

## 5. 链路打通与端到端验证实施流程

对接人员请按照以下五步进行逐级联调：

```text
[第 1 步: 网络与服务探活] ➔ [第 2 步: 心跳在线点亮] ➔ [第 3 步: 推理数据上报] ➔ [第 4 步: 视频画框贴合] ➔ [第 5 步: 多模态联动]
```

### 第 1 步：网络连通性验证
在香橙派终端中测试能否访问部署平台的主机端口（假设平台主机 IP 为 `192.168.1.100`，端口为 `8000`）：

```bash
# 1. 探活 ping
ping 192.168.1.100

# 2. 调用健康检查接口
curl -s http://192.168.1.100:8000/api/v1/health
```
**期望返回**：返回 HTTP 200，且 `database: "connected"`。

---

### 第 2 步：节点心跳打通与设备点亮
在香橙派上执行心跳请求（模拟或真实脚本）：

```bash
curl -i -X POST http://192.168.1.100:8000/api/v1/ingest/heartbeat \
  -H "Content-Type: application/json" \
  -H "X-Device-Token: your-device-token-here" \
  -d '{
    "siteId": "demo-site",
    "deviceId": "orange-pi-001",
    "bootId": "opi-test-boot-01",
    "eventTime": "'$(date -u +"%Y-%m-%dT%H:%M:%SZ")'",
    "sourceMode": "live"
  }'
```
**平台验证现象**：
1. 接口返回 HTTP 202 `{"accepted": true, ...}`。
2. 打开平台前端网页 [http://192.168.1.100:8000](http://192.168.1.100:8000)，切换到 **“设备与质量”** 页面，找到 `orange-pi-001`，其状态应实时变更为 **绿色 `online`**。

---

### 第 3 步：AI 检测元数据上报验证
在香橙派上发送一次模拟检测（如发现火焰）：

```bash
curl -i -X POST http://192.168.1.100:8000/api/v1/ingest/detections \
  -H "Content-Type: application/json" \
  -H "X-Device-Token: your-device-token-here" \
  -d '{
    "eventId": "test-fire-'$(date +%s)'",
    "siteId": "demo-site",
    "deviceId": "orange-pi-001",
    "cameraId": "k230-001",
    "sourceView": "k230",
    "target": "fire",
    "confidence": 0.92,
    "bbox": [0.3, 0.3, 0.2, 0.3],
    "frameId": "frame-001",
    "eventTime": "'$(date -u +"%Y-%m-%dT%H:%M:%SZ")'",
    "sequence": 1,
    "bootId": "opi-test-boot-01",
    "quality": "valid",
    "sourceMode": "live",
    "modelVersion": "yolov8-rknn-v1.0"
  }'
```
**平台验证现象**：
1. 接口返回 HTTP 202。
2. 浏览器打开 **“态势总览”** 或 **“事件中心”** 页面，无需刷新，通过 WebSocket 实时弹出一行黄色 Warning 告警：“**Orange Pi AI 推理检测到 fire · 置信度 92%**”。

---

### 第 4 步：视频画面与检测框贴合联调
若香橙派从 K230 获取到视频帧后中继上传：
1. 先向平台上传一张 `frame_id=frame-001` 的测试图片 `test.jpg`：
```bash
curl -i -X POST "http://192.168.1.100:8000/api/v1/ingest/video/k230?device_id=k230-001&frame_id=frame-001&event_time=$(date -u +"%Y-%m-%dT%H:%M:%SZ")&source_mode=live" \
  -H "Content-Type: image/jpeg" \
  -H "X-Device-Token: your-device-token-here" \
  --data-binary @test.jpg
```
2. 紧接着发送上述带有 `frameId: "frame-001"` 的检测请求。
**平台验证现象**：
打开前端 **“实时监控”** 页面，视频画面更新为刚才上传的图片，画面上精确叠加出绿色/红色 AI 目标检测方框。

---

### 第 5 步：多模态联动与状态机跳转
当平台同时收到来自 ESP32-S3 的环境升温遥测（例如温度 > 50°C）以及香橙派上报的高置信度火情（`confidence > 0.8`）时：
**平台验证现象**：
顶部总览看板状态栏自动从 `NORMAL` 跃迁为 `DANGER`，并触发审计日志记录。

---

## 6. 香橙派端完整对接参考实现 (Python)

对接人员可在香橙派上直接使用以下脚本作为守护进程运行模板：

```python
#!/usr/bin/env python3
"""
香橙派边缘通信对接客户端 (Sentinel Orange Pi Client)
运行环境: Python 3.8+ (依赖 requests)
安装依赖: pip3 install requests
职责说明: 专职处理 K230 输入帧 -> RKNN NPU 模型推理 -> 数据打包上报平台
        (不进行任何 GPIO 控制和物理动作执行)
"""

import datetime
import logging
import time
import uuid
import requests

# =========================
# 基础配置项
# =========================
SERVER_HOST = "http://192.168.1.100:8000"  # 替换为平台主机局域网 IP
DEVICE_TOKEN = "your-device-token-here"    # 替换为 .env 中配置的 SENTINEL_DEVICE_TOKEN
SITE_ID = "demo-site"
DEVICE_ID = "orange-pi-001"                # 香橙派主推理设备
CAMERA_ID = "k230-001"                    # K230 摄像头源
MODEL_VERSION = "yolov8-rknn-v1.0"

# 全局运行标识与序号游标
BOOT_ID = f"opi-{uuid.uuid4().hex[:8]}"
sequence_counter = 0

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
headers = {
    "Content-Type": "application/json",
    "X-Device-Token": DEVICE_TOKEN,
}

def get_current_utc_iso() -> str:
    """获取当前严格的 ISO-8601 UTC 时间字符串"""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def send_heartbeat() -> bool:
    """上报香橙派节点心跳"""
    url = f"{SERVER_HOST}/api/v1/ingest/heartbeat"
    payload = {
        "siteId": SITE_ID,
        "deviceId": DEVICE_ID,
        "bootId": BOOT_ID,
        "eventTime": get_current_utc_iso(),
        "sourceMode": "live",
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=2.5)
        if resp.status_code == 202:
            logging.info("心跳发送成功")
            return True
        else:
            logging.warning(f"心跳被拒绝: HTTP {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        logging.error(f"心跳网络连接失败: {e}")
        return False

def send_detection(target: str, confidence: float, bbox: list, frame_id: str = None) -> bool:
    """
    上报香橙派 NPU 推理得到的 AI 检测结果
    :param target: "fire" | "smoke" | "person" | "none"
    :param confidence: 0.0 ~ 1.0
    :param bbox: [x, y, w, h] 归一化比例 (0.0 ~ 1.0)
    :param frame_id: 关联的 K230 视频帧 ID
    """
    global sequence_counter
    sequence_counter += 1
    
    url = f"{SERVER_HOST}/api/v1/ingest/detections"
    payload = {
        "eventId": f"det-opi-{uuid.uuid4().hex[:10]}",
        "siteId": SITE_ID,
        "deviceId": DEVICE_ID,
        "cameraId": CAMERA_ID,
        "sourceView": "k230",
        "target": target,
        "confidence": round(float(confidence), 3),
        "bbox": bbox,
        "frameId": frame_id,
        "eventTime": get_current_utc_iso(),
        "sequence": sequence_counter,
        "bootId": BOOT_ID,
        "quality": "valid",
        "sourceMode": "live",
        "modelVersion": MODEL_VERSION,
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=2.5)
        if resp.status_code == 202:
            logging.info(f"检测上报成功 [seq={sequence_counter}]: {target} ({confidence:.1%})")
            return True
        else:
            logging.warning(f"检测上报被拒绝: HTTP {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        logging.error(f"检测数据发送失败: {e}")
        return False

def send_video_frame(frame_bytes: bytes, frame_id: str) -> bool:
    """上报来自 K230 的 JPEG 视频关键帧"""
    url = f"{SERVER_HOST}/api/v1/ingest/video/k230"
    params = {
        "device_id": CAMERA_ID,
        "frame_id": frame_id,
        "event_time": get_current_utc_iso(),
        "source_mode": "live",
    }
    img_headers = {
        "Content-Type": "image/jpeg",
        "X-Device-Token": DEVICE_TOKEN,
    }
    try:
        resp = requests.post(url, params=params, data=frame_bytes, headers=img_headers, timeout=3.0)
        return resp.status_code == 202
    except Exception as e:
        logging.error(f"视频帧发送失败: {e}")
        return False

# =========================
# 主处理流程示范
# =========================
if __name__ == "__main__":
    logging.info(f"启动香橙派通信客户端... 设备 ID={DEVICE_ID}, 启动会话={BOOT_ID}")
    last_hb = 0.0

    while True:
        now = time.time()
        # 1. 周期性上报心跳 (每 3 秒一次)
        if now - last_hb >= 3.0:
            send_heartbeat()
            last_hb = now

        # 2. 模拟处理来自 K230 的视频流帧:
        # frame_bytes, frame_id = get_frame_from_k230()
        #
        # 3. 调用 RKNN 引擎进行 NPU 推理:
        # detections = rknn_model_inference(frame_bytes)
        #
        # 4. 上报视频帧与检测结果:
        # send_video_frame(frame_bytes, frame_id)
        # for det in detections:
        #     send_detection(target=det['label'], confidence=det['score'], bbox=det['bbox'], frame_id=frame_id)

        time.sleep(0.5)
```

---

## 7. 常见报错排查指南 (Troubleshooting)

| 状态码 / 报错信息 | 典型根因 | 解决方案 |
| :--- | :--- | :--- |
| **HTTP 401 / 403 Forbidden** | 请求头缺失或错误的 `X-Device-Token` | 检查香橙派代码中的 Token 是否与服务器 `.env` 中的 `SENTINEL_DEVICE_TOKEN` 严格一致。 |
| **HTTP 415 Unsupported Media** | 上传视频帧时 `Content-Type` 不为 `image/jpeg` | 必须为 `Content-Type: image/jpeg`，不能是 `multipart/form-data` 或 `image/png`。 |
| **HTTP 422 `event_time is in the future`** | 香橙派系统时间超前平台服务器超过 5 秒 | 在香橙派上执行 `sudo ntpdate pool.ntp.org` 或配置 `systemd-timesyncd` 进行网络授时。 |
| **HTTP 422 `out_of_order_sequence`** | 序号未递增或出现了重复发送 | 保证每次检测调用的 `sequence` 单调自增（+1）；若程序重新启动，必须更换 `boot_id`。 |
| **HTTP 422 `bbox must be normalized`** | 传了绝对像素值或坐标超出 1.0 | 检查模型后处理代码，将 `[x, y, w, h]` 除以输入分辨率归一化到 `0.0~1.0`。 |
| **HTTP 422 `invalid JPEG signature`** | 视频数据损坏或未完成编码 | 确认发送的数据流以 `0xFF 0xD8` 开头，以 `0xFF 0xD9` 结尾。 |
| **心跳正常但画面无框** | 检测的 `frameId` 与视频帧 `frame_id` 不一致 | 确保视频上报与检测上报传入完全相同的字符串 ID。 |
