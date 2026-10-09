# 智卫 Sentinel 平台验证报告

## 1. 报告范围

- 验证环境：本次交付的 WSL/Linux 工作区，服务时区与页面时间按 UTC / Asia/Shanghai 明确转换。
- 验证对象：当前目录中的 FastAPI 后端、SQLite 数据层、WebSocket 实时通道、六页面前端、模拟数据链路、接口契约和安全执行边界。
- 验证原则：所有测试和模拟周期均使用独立临时数据库，不写入 `data/sentinel.sqlite3`。
- 本报告只证明软件平台在当前开发环境中的测试结果，不代表 ESP32-S3、K230、Orange Pi 3B、MF937、GPIO、继电器或风扇已经完成实物验收。

## 2. 静态检查

执行：

```bash
node --check frontend/app.js
node --check playwright.config.js
node --check tests/e2e/dashboard.spec.js
bash -n scripts/start.sh
uv run python -m compileall -q backend
```

结果：全部退出码为 0，没有 JavaScript、Bash 或 Python 语法错误。

## 3. 后端自动化测试

执行时通过 `SENTINEL_DATABASE` 指向 `mktemp` 创建的临时目录，避免应用模块导入时接触现有业务数据库：

```bash
sentinel_validation_dir="$(mktemp -d /tmp/sentinel-final-check-XXXXXX)"
SENTINEL_DATABASE="${sentinel_validation_dir}/bootstrap.sqlite3" uv run pytest -q -s
```

结果：

```text
48 passed, 1 warning in 5.38s
```

唯一 warning 是当前测试依赖中的 Starlette/httpx 弃用提醒，不是业务断言失败。测试覆盖的主要边界包括：

- 遥测、检测、心跳、视频帧、命令和执行器回执接口。
- camelCase / snake_case 契约、单位和值域校验、时区、未来时间、过期时间和有限数值。
- 事件 ID 去重、序列乱序、事件时间乱序和 Boot ID 隔离。
- 站点与 `live` / `simulated` 来源模式隔离。
- NORMAL / WATCH / ALERT / DANGER 状态机、多模态联合判断、缺失数据保持风险和恢复观察。
- 命令幂等、状态流转、超时、晚到回执和真实执行安全锁。
- JPEG 类型、大小、签名、帧 ID、时间新鲜度和视频通道能力。
- WebSocket 队列、流序号与慢客户端重新同步。

## 4. 浏览器端到端测试

执行：

```bash
npm run test:e2e
```

Playwright 使用独立随机临时数据库和 `127.0.0.1:8010` 测试服务，不复用预览服务或业务库。

结果：

```text
11 passed (24.7s)
```

已在 Chromium 中验证：

1. 首屏显示新鲜模拟遥测、真实原始单位和四组独立曲线。
2. 态势总览、实时监控、事件中心、设备与质量、历史数据、接入与接口六页均可访问。
3. 未接摄像头时明确显示“视频未接入”，不会伪装成真实画面。
4. 无视频的模拟检测框明确标注为元数据展示，不代表画面检测。
5. 控制令牌只在当前标签页的 `sessionStorage` 中保存，可清除，不写入 `localStorage`。
6. 模拟命令返回可审计的 completed 回执，并明确写明“未驱动物理硬件”。
7. 事件确认后，刷新与审计视图保持一致。
8. 历史数据可按原始值、单位、设备、来源和时间查询。
9. `resync_required` 与实时序号缺口会触发 REST 权威同步。
10. 连接断开后数据会老化为 stale，不会一直显示为新鲜。
11. 390 × 844 手机视口下六个页面均无主页面横向溢出。

每个用例都会收集页面 JavaScript 异常、浏览器 console error、资源加载错误和 CSP 错误；本次全部为空。

## 5. 完整模拟周期验证

另行启动使用 `/tmp/sentinel-final-cycle-v2.sqlite3` 的临时服务，将模拟间隔设为 1 秒，连续采样 54 秒。模拟规则周期为 48 秒，因此采样窗口覆盖了一个以上完整周期。

观察到的业务状态集合：

```text
NORMAL, WATCH, ALERT, DANGER
```

观察到的完整方向：

```text
NORMAL → WATCH → ALERT → DANGER → NORMAL
```

验证结果：

- 所有数据来源均为 `simulated`，没有与 live 数据混用。
- 每次进入 DANGER 都生成 `alarm_on` 和 `fan_on` 两条模拟命令。
- 两条命令均为 `completed`，但执行说明明确为“仅完成模拟命令回执；未驱动物理硬件”。
- 临时实例截至查询时累计 3 次 DANGER，因而共有 6 条模拟命令；这个数量是实例累计值，核心规则是一轮 DANGER 对应一对命令。
- `simulator_error` 事件数量为 0。
- 健康接口返回服务与数据库正常，MQTT 为 `disabled-in-simulation`。
- 两路视频通道均为 `missing`，没有把模拟检测元数据描述为已经接入摄像头。

## 6. 现有数据库保护

验证前后均对现有数据库计算 SHA-256：

```text
87a5e1af9b3ad1c2fc11ad085fcfbd21abd050922787ab91960cd2cd7b5a8792  data/sentinel.sqlite3
```

指纹一致，说明本轮静态检查、测试、完整周期验证和预览服务没有修改该数据库。

## 7. 尚未完成的实物验收

以下项目不能由当前软件测试替代：

- ESP32-S3 传感器驱动、FreeRTOS 固件和现场时钟同步。
- K230 的 NNCase/KPU 模型编译、量化、推理与实际帧率。
- Orange Pi 的 RKNN-Toolkit2 模型部署、NPU 推理和 MF937 采集。
- MQTT Broker 的真实网络、TLS、掉线、抖动、拥塞和 retained-message 联调。
- GPIO adapter、光耦继电器、声光器件、风扇、急停、保险和断电安全态。
- 真实环境中的延迟、FPS、误报率、漏报率、水雾、反光、暗光和 EMC 测试。
- `scripts/start.ps1` 已完成静态编写和环境隔离设计，但本次 WSL/Linux 验证环境没有执行 Windows PowerShell 实机启动。

在上述项目完成前，平台中的毫秒级、20+ FPS、工业级可靠性和消防处置能力只能作为设计目标，不能作为已经实测的结论。
