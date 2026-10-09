# 事件中心与安全扩展接口

## 本次实现范围

平台只有 K230 一路摄像头，香橙派承担主控和 AI 推理，ESP32-S3 接入温湿度、光照等遥测。事件中心支持数据库全量关键词搜索、等级/确认状态/类型/处置阶段筛选、严重待确认优先排序和可展开证据，每页 20 条。摘要统计仍仅覆盖最近 100 条，不能代替全量历史统计。

人工确认表示已阅，不表示风险已解除。事件详情保留发生时数据质量、确认人和时间；实时事件到达时已展开的证据不会被收起。待确认请求期间按钮禁用，失败可重试。

本次实现的是工业安全场景的接入基础，不是已经认证的工业安全系统。异常识别、PPE 检测和禁区分析算法尚未部署；平台不生成虚假的算法结果，不承诺现场风险识别准确率。

## 接口

| 接口 | 用途 | 权限 |
| --- | --- | --- |
| `POST /api/v1/ingest/anomalies` | 香橙派/已登记分析节点上传异常识别结果 | `X-Device-Token` |
| `POST /api/v1/ingest/industrial-alerts` | 上传结构化工业安全告警 | `X-Device-Token` |
| `GET /api/v1/events/{event_id}` | 当前站点及运行来源内的事件详情 | 沿用平台只读访问策略 |
| `POST /api/v1/events/{event_id}/acknowledge` | 记录人工确认 | `X-Control-Token` |
| `GET /api/v1/events/search` | 全量历史筛选分页 | 沿用平台只读访问策略 |
| `POST /api/v1/events/{event_id}/actions` | 认领、记录、复核、关闭、重开 | `X-Control-Token` |

输入接口返回 202 和完整 EventRecord，写入 SQLite 并通过现有 WebSocket `event` 消息更新页面。它们不触发 FSM 重算或 GPIO 操作；现有遥测/检测接口和执行器安全链路保持原样。请求内容重复且 event_id 相同返回原事件，保留已有人工确认且不重复推送；同 ID 内容冲突拒绝。安全事件写入使用 SQLite 原子事务，避免并发重复写入或返回未落库的冲突内容。生产运行仍应采用单进程本地服务部署，实时广播与内存状态的多进程一致性另行设计。

### 输入契约（snake_case，亦接受 camelCase）

```json
{
  "event_id": "orange-anomaly-0001",
  "site_id": "demo-site",
  "source_device_id": "orange-pi-001",
  "category": "temperature",
  "severity": "warning",
  "title": "温度异常趋势待复核",
  "message": "边缘分析节点报告异常趋势，请核对环境遥测与现场情况",
  "occurred_at": "<当前带时区的 ISO-8601 时间>",
  "source_mode": "simulated",
  "quality": "simulated",
  "correlation_id": "corr-temperature-0001",
  "rule_id": "temperature-trend-v1",
  "model_version": "edge-anomaly-v1",
  "confidence": 0.86,
  "evidence": {
    "metric": "temperature",
    "telemetry_event_ids": ["真实上游事件 ID"],
    "camera_id": "k230-001",
    "frame_id": "仅有真实关联时填写"
  }
}
```

occurred_at 必须换成发送时的真实时间；固定旧时间会被拒绝。category 可选 fire、smoke、person、ppe_missing、restricted_area、temperature、humidity、light、equipment、other。severity 可选 info、warning、critical。模型版本和置信度可省略，其他顶层字段必填（evidence 默认空对象）。evidence 是扩展 JSON，建议传原始遥测/检测 ID、数值及单位、阈值、K230 帧 ID、采集时间，不应写入个人敏感信息、图像 Base64 或巨型数据。

只接受当前站点、已登记设备及当前运行来源。模拟环境只接受 simulated 来源/质量，真实环境只接受 live 来源和 valid 质量；回放、估算、无效、过期或超出时钟容差的数据均拒绝，不包装成实时安全告警。使用既有 stale_after_seconds 和 clock_tolerance_seconds 配置。客户端重试时保留相同 ID、时间和内容；超过实时接收窗口的新事件应走未来的历史导入接口，而非此实时入口。

结构字段验证失败返回 422；设备或数据边界校验失败也返回 422。令牌不匹配返回 401。详情/确认访问其他站点或其他运行来源的事件返回 404。

## 后续生产建设边界

当前操作人由请求声明，控制令牌是共享权限，不等同于企业个人身份。真实企业上线前需要独立实施身份认证/RBAC、操作员身份绑定、告警聚合/抑制策略、通知投递及回执、证据保留与防篡改、负载限制、TLS、备份和现场验收。未实现的通知或算法不在 UI 中宣称“已启用”。接入真实工业设备前必须核实适用的安全要求，并保持人工确认与物理动作分离。

兼容性说明：前后端、模拟器、视频通道及 FSM 已迁移至唯一 K230 采集源；旧摄像头节点不恢复到运行拓扑。不可变历史检测仍可读取 `main` 标记，但新输入拒绝它。主推理为香橙派，K230 板载结果只作辅助来源，不是第二独立视觉证据。正式历史记录不删除或改写。

## 事件处置契约

请求包含 `action`（claim/note/submit_review/close/reopen）、`actor`、非空 `note`、`evidence_ref`、`expected_revision` 和唯一 `request_id`。认领后进入 in_progress；责任人可补充记录或提交 pending_review；关闭只能从 pending_review 进入 closed，要求不同声明人和非空证据引用；重开必须说明原因并回到 open。历史记录保留声明人、说明、证据引用、时间、前后阶段和版本。证据引用只保存文本，不表示平台验证了证据真实性。

SQLite 事务内校验版本和幂等请求；同 ID 同内容重试返回现有记录，不重复写入或广播。冲突及非法流转返回 409，越站点/来源访问返回 404，令牌错误返回 401。所有处置与确认均不修改 FSM 或执行器状态，关闭工单不等于现场风险解除。声明复核人不同不是企业身份分离的安全保证，需后续身份提供方实现。

历史查询参数：`q`、`severity`、`acknowledgement`（all/pending/acknowledged）、`kind`（all/anomaly/industrial_alert/other）、`handling`、`sort`（recent/priority）、`page` 和 `page_size`（1–100）。返回 `items,total,page,page_size`，只查询当前站点和运行来源；优先级相同时按发生时间和数据库 ID 排序。单次查询统计和内容使用一致读事务；不同页请求不是跨请求冻结快照，实时新增/确认可能移动条目，可刷新重查。版本字段和操作历史是应用级审计，不是防篡改签名。
