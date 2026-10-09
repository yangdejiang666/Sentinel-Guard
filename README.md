# 智卫 Sentinel 本地安全监测平台

这是按照项目申报书《智卫 Sentinel：基于异构双核边缘协同的多模态安全监测系统》搭建的可运行平台骨架。当前优先完成了“数据接入 → 数据质量判断 → 多模态状态机 → 实时推送 → 前端呈现 → 命令审计”的端到端链路，为 ESP32-S3、K230、Orange Pi 3B、MF937 摄像头和执行器保留了明确的接口边界。

平台默认使用模拟模式启动。模拟链路用于开发和联调，不等同于真实硬件接入，也不能作为现场消防或工业安全结论。

## 当前已交付能力

- FastAPI 本地业务服务和 OpenAPI 文档。
- SQLite 持久化：设备、遥测、检测、事件、状态流转、命令、序号游标。
- 统一 Pydantic 数据契约，同时兼容 camelCase 输入和 snake_case 输出。
- 站点、`source_mode`、`boot_id`、`sequence`、事件时间和接收时间的可追溯隔离。
- `valid`、`simulated`、`stale`、`invalid`、`estimated` 数据质量标记。
- 重复事件、乱序序列、乱序事件时间、未来时间、非法值和不匹配设备能力的拒绝/审计。
- NORMAL / WATCH / ALERT / DANGER 四级状态机。
- 视觉火焰与有效环境热证据的联合决策；湿度和光照干扰证据会被记录。
- 风险恢复需要连续、完整、可信的数据，缺失数据不能被解释成“已恢复正常”。
- WebSocket 权威快照、单调 `stream_sequence`、缺口检测、慢客户端重新同步。
- 模拟数据 48 秒周期，覆盖 NORMAL、WATCH、ALERT、DANGER 和恢复观察阶段。
- DANGER 模拟生成 `alarm_on`、`fan_on` 审计命令，但明确记录“未驱动物理硬件”。
- 真实命令在没有执行器 `completed` 回执之前不会宣称硬件动作完成。
- 主视角和 K230 视角的 JPEG 预览帧接口、帧 ID 校验、缓存和检测框关联。
- MQTT 适配器和主题预留，默认不在模拟模式连接 broker。
- 前端六个页面：态势总览、实时监控、事件中心、设备与质量、历史数据、接入与接口。
- 前端按真实单位绘制温度、湿度、光照和 AI 置信度趋势；过期/无效/估算数据断线，不做伪造插值。
- 控制令牌只保存在当前浏览器标签页的 `sessionStorage`。

## 快速启动

### Linux / macOS / WSL

```bash
cd /mnt/d/Sentinel‑Guard
if [ ! -e .env ]; then cp .env.example .env; fi  # 可选；只在文件不存在时复制
uv sync
./scripts/start.sh
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。停止服务使用 `Ctrl+C`。

脚本会自动回到项目根目录，默认监听 `127.0.0.1:8000`，默认使用 `SIMULATION_MODE=true` 和 `data/sentinel.sqlite3`。可以通过环境变量覆盖：

```bash
SENTINEL_PORT=8012 SENTINEL_DATABASE=/tmp/sentinel-dev.sqlite3 ./scripts/start.sh
```

### Windows PowerShell

```powershell
Set-Location 'D:\Sentinel‑Guard'
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item .env.example .env } # 可选；不覆盖已有配置
.\scripts\start.ps1
```

Windows 脚本使用独立的 `.venv-win`，不会把 Windows Python 环境和 WSL/Linux 的 `.venv` 混用，也不会覆盖已有 `.env`。

### 不使用启动脚本

```bash
uv run uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

开发时可以自行增加 `--reload`；生产或现场部署不建议直接以开发热重载模式运行。

## 页面和接口

| 地址 | 用途 |
| --- | --- |
| `/` | 前端平台 |
| `/docs` | Swagger UI（默认可能依赖 CDN，离线环境不保证资源可用） |
| `/api/v1/health` | 服务、数据库和 MQTT 状态 |
| `/api/v1/overview` | 前端首屏权威快照 |
| `/api/v1/devices` | 设备清单与健康状态 |
| `/api/v1/telemetry/latest` | 当前作用域内最新遥测 |
| `/api/v1/telemetry/history` | 按指标、设备、来源和时间查询历史 |
| `/api/v1/detections` | AI 检测元数据 |
| `/api/v1/events` | 事件和告警审计 |
| `/api/v1/events/{event_id}/acknowledge` | 确认事件 |
| `/api/v1/state/transitions` | FSM 状态流转审计 |
| `/api/v1/commands` | 命令审计 |
| `/api/v1/config` | 规则、阈值、主题和安全开关 |
| `/api/v1/ingest/telemetry` | ESP32 或扩展传感器接入 |
| `/api/v1/ingest/detections` | K230 / Orange Pi 检测接入 |
| `/api/v1/ingest/heartbeat` | 节点心跳接入 |
| `/api/v1/ingest/video/{view}` | `main` / `k230` JPEG 帧接入 |
| `/api/v1/ingest/command-feedback` | 执行器回执接入 |
| `/api/v1/video/channels` | 视频通道状态 |
| `/api/v1/video/{view}/frame` | 最近有效 JPEG 帧 |
| `/ws` | 浏览器实时事件流和权威快照 |

统一数据契约、MQTT 主题和 curl 示例见 [docs/api-examples.md](docs/api-examples.md)。系统架构和数据质量原则见 [docs/architecture.md](docs/architecture.md)。接板步骤见 [docs/hardware-integration.md](docs/hardware-integration.md)。

## 配置

复制 `.env.example` 为 `.env` 后修改。关键配置如下：

```env
SIMULATION_MODE=true
SENTINEL_SITE_ID=demo-site
SENTINEL_DATABASE=./data/sentinel.sqlite3
SENTINEL_STALE_AFTER_SECONDS=5
SENTINEL_HEARTBEAT_TIMEOUT_SECONDS=8
SENTINEL_CONTROL_TOKEN=
SENTINEL_DEVICE_TOKEN=
SENTINEL_ALLOW_ACTUATION=false
SENTINEL_AUTO_ACTUATION=false
```

真实模式必须同时提供 `SENTINEL_CONTROL_TOKEN` 和 `SENTINEL_DEVICE_TOKEN`。当前实现是共享令牌，不是逐设备证书认证；项目没有完整账户体系和 RBAC。不要把服务直接暴露到公网，也不要让 live、simulated、replay 数据共用同一个运行数据库。真实模式建议使用独立数据库，并先完成局域网、反向代理、访问控制和日志脱敏设计。

真实执行还需要 `SENTINEL_ALLOW_ACTUATION=true`，自动执行还需要 `SENTINEL_AUTO_ACTUATION=true`；这两个开关只是软件门槛，不代表 GPIO、继电器或风扇已经接好。

## 数据真实性边界

以下能力在当前代码中是接口和审计边界，不应被描述为已经完成的硬件能力：

- ESP32-S3 传感器固件尚未实现。
- K230 的 NNCase/KPU 模型编译和部署尚未实现。
- Orange Pi 的 RKNN-Toolkit2 模型部署和真实 NPU 推理尚未完成。
- MF937 的真实视频采集、编码和持续帧率尚未验收。
- GPIO adapter、继电器、电风扇、蜂鸣器和声光灯尚未完成现场联调。
- `local_fallback_ready=false` 表示本地 GPIO 执行器适配器没有注册；本地命令发布接口不等于 GPIO 已硬连接。
- 模拟命令不会驱动物理硬件；风扇输出仅为演示审计对象，不是认证消防处置设备。
- 原始申报书中的“毫秒级”“20+ FPS”“误报率降低”等内容是设计目标或申报书描述，不是本平台已经测得的结果。
- 接板后必须在目标分辨率、光线、水雾、反光、断网、掉电、重启和执行器故障条件下重新测量性能、帧率、延迟、误报率和漏报率。

## 数据库和数据安全

默认 `data/sentinel.sqlite3` 是本地持久化数据库，启动服务不会清空或覆盖已有数据。测试应使用临时数据库，例如 `/tmp/sentinel-e2e.sqlite3`，不要把测试写入默认数据库。数据库和 `.env` 已加入 `.gitignore`，备份或迁移前请先确认目标路径。

## 验证命令

```bash
uv run python -m compileall -q backend
node --check frontend/app.js
node --check playwright.config.js
node --check tests/e2e/dashboard.spec.js
SENTINEL_DATABASE=/tmp/sentinel-unit-bootstrap.sqlite3 uv run pytest -q -s
npm run test:e2e
```

当前后端单元/API 测试覆盖 48 项，浏览器端到端测试覆盖 11 项；本次验证结果见 [docs/validation-report.md](docs/validation-report.md)。测试使用独立临时数据库；上面的环境变量还用于隔离应用模块导入时创建的数据库，不使用已有业务库。PowerShell 运行测试时可先设置 `$env:SENTINEL_DATABASE` 为独立临时路径。

Playwright 测试需要 `npm ci`、已安装的 Chromium 和对应系统依赖；首次可执行 `npx playwright install chromium`，Linux 需要时执行 `npx playwright install-deps chromium`。如果浏览器启动时报告缺少 `libnspr4.so` 等系统库，说明测试环境缺少浏览器运行依赖，不是应用断言结果，应先安装浏览器依赖后重跑。

## 建议接入顺序

1. 先以 live 模式接入 ESP32-S3 的温度、湿度、光照和心跳。
2. 验证时间同步、`sequence`、`boot_id`、断线和恢复行为。
3. 接入 K230 检测元数据，不接执行器；验证 `source_view`、`frame_id` 和置信度质量。
4. 接入 MF937 或 K230 JPEG 预览，确认画面和检测结果同帧后再显示真实检测框。
5. 接入执行器反馈，只允许 acknowledged/completed/failed 形成可审计状态。
6. 完成硬件联锁、急停、保险、断电策略和现场验收后，才评估是否打开物理执行安全开关。

