/* global fetch */

const state = {
  overview: null,
  events: [],
  detections: [],
  transitions: [],
  commands: [],
  history: {},
  chartPoints: { temperature: [], humidity: [], light: [], main_confidence: [], k230_confidence: [], vision_threshold: [] },
  socket: null,
  reconnectTimer: null,
  page: 'overview',
  needsResync: true,
  resyncPromise: null,
  historyRequestSequence: 0,
  historyRecords: [],
  config: null,
  streamId: null,
  streamSequence: 0,
  serverOffsetMs: 0,
  lastRealtimeAt: 0,
  videoChannels: [],
  videoErrors: {},
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const metricMeta = {
  temperature: { label: '温度', unit: '°C', color: '#b77515' },
  humidity: { label: '湿度', unit: '%', color: '#087f72' },
  light: { label: '光照', unit: 'lux', color: '#3166c4' },
};

// Legacy backend devices remain untouched; the console has one visual node.
function consoleDevices(items = []) {
  return items.filter((device) => device.device_id !== 'camera-main-001' && device.node_type !== 'Camera').map((device) => ({
    ...device,
    display_name: device.node_type === 'K230' ? 'K230 视觉采集节点' : device.node_type === 'Orange Pi 3B' ? 'Orange Pi 主控与 AI 推理' : device.display_name,
    capabilities: device.node_type === 'Orange Pi 3B'
      ? device.capabilities.filter((capability) => capability !== 'video-main')
      : device.capabilities,
  }));
}

function metricInfo(metric) {
  return metricMeta[metric] || { label: metric, unit: '', color: '#c292ff' };
}

function inCurrentScope(item) {
  return !state.overview || ((item.site_id || state.overview.site_id) === state.overview.site_id && item.source_mode === (state.overview.simulation_mode ? 'simulated' : 'live'));
}

function environmentByMetric(records = []) {
  return Object.fromEntries(records.filter((item) => item.device_id === 'esp32-s3-001').map((item) => [item.metric, item]));
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

// Presentation-only compatibility for legacy backend wording. Keep original
// records/evidence intact, and never associate inference boxes with other frames.
function consoleText(value) {
  return String(value ?? '')
    .replace(/\bMAIN 视角检测到/g, 'Orange Pi AI 推理检测到')
    .replace(/\bK230 视角检测到/g, 'K230 检测到')
    .replace(/环境或双路视觉数据缺失\/过期/g, '环境、视觉采集或 AI 推理数据缺失/过期');
}

function formatTime(value, withSeconds = true) {
  if (!value) return '--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '--';
  return date.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', ...(withSeconds ? { second: '2-digit' } : {}) });
}

function formatDate(value) {
  if (!value) return '--';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '--' : date.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' });
}

function ageSeconds(value) {
  if (!value) return Infinity;
  return Math.max(0, (Date.now() + state.serverOffsetMs - new Date(value).getTime()) / 1000);
}

function effectiveQuality(item) {
  if (!item) return 'missing';
  if (item.quality === 'invalid') return 'invalid';
  const staleAfter = Number(state.config?.stale_after_seconds || 5);
  return ageSeconds(item.event_time) > staleAfter ? 'stale' : item.quality;
}

function isUsable(item) {
  return item && inCurrentScope(item) && ['valid', 'simulated'].includes(effectiveQuality(item));
}

function qualityLabel(quality) {
  return ({ valid: '有效', simulated: '模拟', stale: '过期', invalid: '无效', estimated: '估算' })[quality] || '未知';
}

function qualityClass(quality) {
  return ['valid', 'simulated', 'stale', 'invalid', 'estimated'].includes(quality) ? quality : 'unknown';
}

function severityClass(severity) {
  return severity === 'critical' ? 'critical' : severity === 'warning' ? 'warning' : '';
}

function deviceStatusLabel(status) {
  return ({ online: '在线', delayed: '延迟', offline: '离线', not_connected: '未接入' })[status] || status || '未知';
}

function commandStatusLabel(status) {
  return ({ accepted: '已登记', sent: '已发送', acknowledged: '已确认', completed: '已完成', timeout: '超时', failed: '失败' })[status] || status || '未知';
}

function sourceModeLabel(mode) {
  return ({ live: '真实', simulated: '模拟', replay: '回放' })[mode] || mode || '未知';
}

function getControlToken() {
  try { return sessionStorage.getItem('sentinel-control-token') || ''; } catch (_) { return ''; }
}

function setControlToken(value) {
  try {
    if (value) sessionStorage.setItem('sentinel-control-token', value);
    else sessionStorage.removeItem('sentinel-control-token');
  } catch (_) { /* Session storage can be disabled by browser policy. */ }
  updateTokenStatus();
}

function controlHeaders() {
  const token = getControlToken();
  return token ? { 'X-Control-Token': token } : {};
}

function updateTokenStatus() {
  const status = $('#token-status');
  const input = $('#control-token');
  const configured = Boolean(getControlToken());
  if (status) status.textContent = configured ? '当前标签页已配置控制令牌' : '当前未配置浏览器控制令牌';
  if (input && !input.value && configured) input.placeholder = '当前标签页已配置；输入新值可替换';
}

function showToast(message, kind = 'info') {
  const stack = $('#toast-stack');
  if (!stack) return;
  const toast = document.createElement('div');
  toast.className = `toast ${kind}`;
  toast.textContent = consoleText(message);
  stack.appendChild(toast);
  setTimeout(() => toast.remove(), 4200);
}

function setConnection(connected) {
  const pill = $('#connection-pill');
  const edge = $('#edge-status');
  if (!pill) return;
  pill.classList.toggle('online', connected);
  pill.classList.toggle('offline', !connected);
  pill.querySelector('b').textContent = connected ? '实时已连接' : '连接中断';
  if (edge) edge.textContent = connected ? '实时服务正常' : '等待重连';
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${response.status}: ${detail.slice(0, 180)}`);
  }
  return response.json();
}

function updateClock() {
  const now = new Date();
  $('#clock-time').textContent = now.toLocaleTimeString('zh-CN', { hour12: false });
  $('#clock-date').textContent = now.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' });
}

function setModeBanner(overview) {
  const banner = $('#mode-banner');
  if (!banner) return;
  banner.innerHTML = overview?.simulation_mode
    ? '<strong>SIMULATION / 模拟数据模式</strong><span>当前数据用于平台联调，不代表真实现场状态；物理执行器不会被驱动。</span>'
    : '<strong>LIVE / 真实设备模式</strong><span>页面数据来自已接入节点；请结合质量和系统健康标记判断数据是否可用。</span>';
}

function updateSystemNotice(overview) {
  if (!overview) return;
  const notice = $('#system-notice');
  const message = $('#system-message');
  if (message) message.textContent = `${overview.system_mode} · ${consoleText(overview.system_message)}`;
  if (notice) notice.className = `system-notice ${String(overview.system_mode || '').toLowerCase()}`;
}

function updateState(overview) {
  if (!overview) return;
  const hero = $('#state-hero');
  const current = overview.current_state || 'NORMAL';
  hero.className = `panel state-hero ${current.toLowerCase()}`;
  $('#current-state').textContent = current;
  $('#state-reason').textContent = consoleText(overview.state_reason) || '暂无状态依据';
  $('#state-duration').textContent = overview.state_since ? `自 ${formatTime(overview.state_since)} 起` : '等待数据';
  const evidence = $('#state-evidence');
  const telemetry = environmentByMetric(overview.latest_telemetry || []);
  const detection = (overview.latest_detections || [])[0];
  const tags = [];
  if (telemetry.temperature) tags.push(`温度 ${Number(telemetry.temperature.value).toFixed(1)}${telemetry.temperature.unit}`);
  if (telemetry.humidity) tags.push(`湿度 ${Number(telemetry.humidity.value).toFixed(1)}${telemetry.humidity.unit}`);
  if (detection && detection.target !== 'none') tags.push(`${detection.target} ${Math.round(detection.confidence * 100)}%`);
  if (overview.state_trusted === false) tags.push('可信度不足 / 降级观察');
  if (overview.rules) tags.push(`规则 ${overview.rules.version} · 阈值 ${Number(overview.vision_threshold || 0).toFixed(2)}`);
  evidence.innerHTML = (tags.length ? tags : ['等待有效数据']).map((tag) => `<span>${escapeHtml(tag)}</span>`).join('');
  const evidenceJson = $('#state-evidence-json');
  if (evidenceJson) evidenceJson.textContent = JSON.stringify(overview.state_evidence || {}, null, 2);
  $$('#fsm-track [data-state]').forEach((node) => node.classList.toggle('active', node.dataset.state === current));
}

function updateSensors(overview) {
  const items = environmentByMetric(overview?.latest_telemetry || []);
  ['temperature', 'humidity', 'light'].forEach((metric) => {
    const item = items[metric];
    const value = $(`#value-${metric}`);
    const quality = $(`#quality-${metric}`);
    if (value) value.textContent = item ? Number(item.value).toFixed(metric === 'light' ? 0 : 1) : '--';
    if (quality) {
      const currentQuality = effectiveQuality(item);
      quality.className = `quality ${qualityClass(currentQuality)}`;
      quality.textContent = item ? qualityLabel(currentQuality) : '等待';
    }
    const trendQuality = $(`#trend-quality-${metric}`);
    if (trendQuality) trendQuality.textContent = item ? `${qualityLabel(effectiveQuality(item))} · ${formatTime(item.event_time)}` : '等待数据';
  });
  const detections = overview?.latest_detections || [];
  const usableDetections = detections.filter(isUsable);
  const risk = usableDetections.reduce((max, item) => Math.max(max, item.confidence || 0), 0);
  $('#value-confidence').textContent = usableDetections.length ? Math.round(risk * 100) : '--';
  const detectionQuality = usableDetections.length ? effectiveQuality(usableDetections[0]) : 'stale';
  $('#quality-confidence').className = `quality ${qualityClass(detectionQuality)}`;
  $('#quality-confidence').textContent = usableDetections.length ? qualityLabel(detectionQuality) : '无实时检测';
  $('#last-refresh').textContent = overview?.generated_at ? formatTime(overview.generated_at) : '--';
  $('#current-vision-threshold').textContent = `阈值 ${Number(overview?.vision_threshold || 0).toFixed(2)}${overview?.state_trusted ? '' : ' · 待复核'}`;
}

function updateDeviceMiniList(devices = []) {
  devices = consoleDevices(devices);
  const online = devices.filter((device) => device.status === 'online').length;
  $('#health-score').textContent = `${online} / ${devices.length}`;
  const list = $('#device-mini-list');
  list.innerHTML = devices.map((device) => `<div class="device-mini">
    <div class="device-mini-name"><i class="${escapeHtml(device.status)}"></i><div><strong>${escapeHtml(device.display_name)}</strong><small>${escapeHtml(device.node_type)} · ${escapeHtml(device.health_message)}</small></div></div>
    <span class="device-status ${escapeHtml(device.status)}"><i class="${escapeHtml(device.status)}"></i>${escapeHtml(deviceStatusLabel(device.status))}</span>
  </div>`).join('') || '<div class="empty-state">暂无设备</div>';
}

function updateDetections(detections = state.detections) {
  renderFeedMetadata();
  renderDetectionTable(detections);
}

function channelIsFresh(channel) {
  return channel?.status === 'fresh' && ageSeconds(channel.event_time) <= Number(state.config?.stale_after_seconds || 5);
}

function renderFeedMetadata() {
  ['k230'].forEach((view) => {
    const detection = state.detections.find((item) => item.source_view === view && item.quality !== 'invalid' && item.quality !== 'estimated' && inCurrentScope(item));
    const channel = state.videoChannels.find((item) => item.source_view === view);
    $$(`.camera-feed[data-view="${view}"]`).forEach((feed) => {
      const stage = feed.querySelector('.feed-stage');
      const bbox = stage?.querySelector('canvas.bbox');
      const status = feed.querySelector('.feed-top b');
      const meta = feed.querySelector('.feed-meta');
      const label = stage?.querySelector('.feed-label strong');
      const image = stage?.querySelector('img.video-preview');
      if (!stage || !bbox || !status || !meta || !label) return;
      const stageWidth = stage.clientWidth; const stageHeight = stage.clientHeight; const dpr = window.devicePixelRatio || 1;
      bbox.width = Math.max(1, Math.round(stageWidth * dpr)); bbox.height = Math.max(1, Math.round(stageHeight * dpr));
      const context = bbox.getContext('2d'); context.scale(dpr, dpr); context.clearRect(0, 0, stageWidth, stageHeight);
      bbox.classList.remove('visible', 'simulated-metadata');
      const loadFailed = channel?.frame_id && state.videoErrors[view] === channel.frame_id;
      const hasFrame = channelIsFresh(channel) && image?.naturalWidth && feed.dataset.loadedFrameId === channel.frame_id;
      let copy = detection ? `${detection.target} · ${Math.round(detection.confidence * 100)}% · ${detection.model_version} · ${formatTime(detection.event_time)}` : '等待检测事件';
      status.textContent = loadFailed ? '预览加载失败' : hasFrame ? '预览已接入' : channelIsFresh(channel) ? '正在加载预览' : channel?.status === 'stale' ? '预览已过期' : '视频未接入';
      label.textContent = loadFailed ? '画面加载失败 · 不显示检测框' : channel?.status === 'stale' ? '视频已过期 · 等待新帧' : '视频源未接入';
      if (detection && !isUsable(detection)) {
        copy += ` · ${qualityLabel(effectiveQuality(detection))}，不用于实时画框`;
      } else if (detection && detection.target !== 'none') {
        const matched = hasFrame && detection.frame_id && detection.frame_id === channel.frame_id && detection.source_mode === channel.source_mode;
        const simulatedOnly = !loadFailed && !channelIsFresh(channel) && state.overview?.simulation_mode && detection.source_mode === 'simulated';
        if (matched || simulatedOnly) {
          const [x, y, width, height] = detection.bbox || [];
          if ([x, y, width, height].every(Number.isFinite) && width > 0 && height > 0) {
            const scale = matched ? Math.min(stageWidth / image.naturalWidth, stageHeight / image.naturalHeight) : 1;
            const renderedWidth = matched ? image.naturalWidth * scale : stageWidth;
            const renderedHeight = matched ? image.naturalHeight * scale : stageHeight;
            const boxX = (stageWidth - renderedWidth) / 2 + x * renderedWidth;
            const boxY = (stageHeight - renderedHeight) / 2 + y * renderedHeight;
            const boxWidth = width * renderedWidth; const boxHeight = height * renderedHeight;
            const boxColor = simulatedOnly ? '#f4bd64' : '#ff6477';
            context.strokeStyle = boxColor; context.lineWidth = 2; context.setLineDash(simulatedOnly ? [7, 4] : []); context.strokeRect(boxX, boxY, boxWidth, boxHeight); context.setLineDash([]);
            const boxLabel = `${simulatedOnly ? '模拟元数据 · ' : ''}${detection.target} ${Math.round(detection.confidence * 100)}%`;
            context.font = '12px Consolas, Microsoft YaHei, sans-serif';
            const labelWidth = Math.min(stageWidth - 8, context.measureText(boxLabel).width + 12);
            const labelY = Math.max(2, boxY - 22); const labelX = Math.max(2, Math.min(boxX, stageWidth - labelWidth - 2));
            context.fillStyle = simulatedOnly ? 'rgba(173,119,30,.94)' : 'rgba(199,50,69,.94)'; context.fillRect(labelX, labelY, labelWidth, 20);
            context.fillStyle = '#fff'; context.textBaseline = 'middle'; context.fillText(boxLabel, labelX + 6, labelY + 10, labelWidth - 10);
            bbox.classList.add('visible');
            bbox.classList.toggle('simulated-metadata', simulatedOnly);
          }
          status.textContent = simulatedOnly ? '模拟元数据框 · 无视频' : `${sourceModeLabel(detection.source_mode)}预览 · 已对帧`;
          if (simulatedOnly) label.textContent = '模拟元数据（无视频画面）';
          copy += simulatedOnly ? ' · 仅展示归一化框坐标，不代表画面检测' : ` · frame_id ${detection.frame_id}`;
        } else if (hasFrame) {
          copy += ' · 检测与当前帧不匹配，不叠加检测框';
        } else {
          copy += ' · 无匹配画面，不叠加真实检测框';
        }
      } else if (detection && isUsable(detection)) {
        copy += ' · 当前检测无目标';
      }
      meta.textContent = copy;
    });
  });
}

function renderDetectionTable(detections = []) {
  detections = detections.filter(inCurrentScope);
  const body = $('#detection-table');
  if (!body) return;
  body.innerHTML = detections.length ? detections.slice(0, 30).map((item) => `<tr>
    <td>${formatTime(item.event_time)}</td><td>${item.device_id === 'orange-pi-001' ? 'Orange Pi AI' : escapeHtml(item.device_id)}</td><td>${escapeHtml(item.target)}</td>
    <td>${Math.round(item.confidence * 100)}%</td><td>${escapeHtml(item.model_version)}</td><td><span class="quality ${qualityClass(effectiveQuality(item))}">${qualityLabel(effectiveQuality(item))}</span></td><td>${escapeHtml(item.correlation_id)}</td>
  </tr>`).join('') : '<tr><td colspan="7" class="table-empty">等待检测事件</td></tr>';
}

function eventItem(event) {
  const canAck = !event.acknowledged;
  return `<div class="timeline-item">
    <time class="timeline-time">${formatTime(event.occurred_at)}</time><i class="timeline-marker ${severityClass(event.severity)}"></i>
    <div><div class="timeline-title">${escapeHtml(consoleText(event.title))}</div><div class="timeline-message">${escapeHtml(consoleText(event.message))}</div><div class="timeline-meta"><span class="quality ${qualityClass(event.quality)}">${qualityLabel(event.quality)}</span><span class="quality ${event.source_mode === 'simulated' ? 'simulated' : 'valid'}">${event.source_mode === 'simulated' ? '模拟来源' : '实时来源'}</span></div></div>
    ${canAck ? `<button class="ack-button" data-ack-event="${escapeHtml(event.event_id)}">确认</button>` : '<span class="ack-button acknowledged">已确认</span>'}
  </div>`;
}

function renderEvents(events = state.events, target = '#overview-events') {
  const node = $(target);
  if (!node) return;
  node.innerHTML = events.length ? events.slice(0, target === '#overview-events' ? 8 : 100).map(eventItem).join('') : '<div class="empty-state">暂无事件</div>';
  $$("[data-ack-event]", node).forEach((button) => button.addEventListener('click', () => acknowledgeEvent(button.dataset.ackEvent)));
}

function updateEventSummary() {
  const unacked = state.events.filter((event) => !event.acknowledged).length;
  const critical = state.events.filter((event) => event.severity === 'critical').length;
  $('#event-count').textContent = unacked;
  $('#unacked-count').textContent = unacked;
  $('#critical-count').textContent = critical;
  $('#transition-count').textContent = state.transitions.length;
  $('#command-count').textContent = state.commands.length;
}

function renderAllEvents() {
  const filter = $('#event-filter')?.value || 'all';
  const filtered = filter === 'all' ? state.events : state.events.filter((event) => event.severity === filter);
  renderEvents(filtered, '#all-events');
}

function pushPoint(metric, record) {
  if (record.device_id !== 'esp32-s3-001' || !inCurrentScope(record)) return;
  appendChartPoint(metric, { id: record.event_id, time: record.event_time, value: Number(record.value), quality: record.quality });
}

function appendChartPoint(metric, point) {
  if (!state.chartPoints[metric]) state.chartPoints[metric] = [];
  const points = state.chartPoints[metric];
  if (!point.time || !Number.isFinite(new Date(point.time).getTime())) return;
  if (points.some((item) => point.id ? item.id === point.id : item.time === point.time)) return;
  points.push(point);
  points.sort((a, b) => new Date(a.time) - new Date(b.time));
  if (points.length > 240) points.splice(0, points.length - 240);
}

function pushDetectionPoint(item) {
  if (!inCurrentScope(item)) return;
  appendChartPoint(`${item.source_view}_confidence`, { id: item.event_id, time: item.event_time, value: Number(item.confidence), quality: item.quality });
}

function chartPointIsValid(point) {
  return Number.isFinite(point.value) && ['valid', 'simulated'].includes(point.quality);
}

function drawChart(canvas, series, options = {}) {
  if (!canvas) return;
  const parent = canvas.parentElement;
  const width = Math.floor(parent.clientWidth);
  const height = Math.floor(parent.clientHeight);
  if (width < 20 || height < 20) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = width * dpr; canvas.height = height * dpr;
  const ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr); ctx.clearRect(0, 0, width, height);
  const pad = { left: 43, right: 12, top: 12, bottom: 25 };
  const now = Date.now() + state.serverOffsetMs;
  const filteredSeries = series.map((item) => ({ ...item, points: item.points.filter((point) => !options.live || new Date(point.time).getTime() >= now - 120000) }));
  const allValues = filteredSeries.flatMap((item) => item.points.filter(chartPointIsValid).map((point) => point.value));
  if (!allValues.length) { if (options.empty) options.empty.classList.remove('hidden'); return; }
  if (options.empty) options.empty.classList.add('hidden');
  const valueMin = Math.min(...allValues); const valueMax = Math.max(...allValues);
  const valuePad = Math.max((valueMax - valueMin) * 0.1, Math.abs(valueMax) * 0.03, 0.5);
  const low = Number.isFinite(options.low) ? options.low : valueMin - valuePad;
  const high = Number.isFinite(options.high) ? options.high : valueMax + valuePad;
  const range = high - low || 1;
  const times = filteredSeries.flatMap((item) => item.points.map((point) => new Date(point.time).getTime())).filter(Number.isFinite);
  const minTime = options.live ? now - 120000 : Math.min(...times);
  const maxTime = options.live ? Math.max(now, ...times) : Math.max(minTime + 1000, ...times);
  const timeSpan = Math.max(1, maxTime - minTime);
  ctx.strokeStyle = '#e7ebf0'; ctx.lineWidth = 1;
  for (let i = 0; i < 4; i += 1) { const y = pad.top + (height - pad.top - pad.bottom) * i / 3; ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke(); }
  ctx.font = '11px Consolas, monospace'; ctx.fillStyle = '#6b7585'; ctx.textAlign = 'right';
  for (let i = 0; i < 4; i += 1) { const value = high - range * i / 3; const y = pad.top + (height - pad.top - pad.bottom) * i / 3 + 3; ctx.fillText(value.toFixed(value > 100 ? 0 : 1), pad.left - 6, y); }
  filteredSeries.forEach((item) => {
    const points = item.points; if (!points.length) return;
    ctx.strokeStyle = item.color; ctx.lineWidth = 1.8; ctx.setLineDash(item.dashed ? [5, 4] : []); ctx.beginPath(); let started = false; let previous = null;
    points.forEach((point) => {
      if (!chartPointIsValid(point)) { started = false; previous = null; return; }
      if (previous && (new Date(point.time) - new Date(previous.time)) / 1000 > Number(state.config?.stale_after_seconds || 5)) started = false;
      const x = pad.left + ((new Date(point.time).getTime() - minTime) / timeSpan) * (width - pad.left - pad.right);
      const y = pad.top + (high - point.value) / range * (height - pad.top - pad.bottom);
      if (!started) { ctx.moveTo(x, y); started = true; } else if (item.step) { const previousY = pad.top + (high - previous.value) / range * (height - pad.top - pad.bottom); ctx.lineTo(x, previousY); ctx.lineTo(x, y); } else ctx.lineTo(x, y);
      previous = point;
    }); ctx.stroke();
    ctx.setLineDash([]);
    const last = [...points].reverse().find(chartPointIsValid);
    if (last) { const x = pad.left + ((new Date(last.time).getTime() - minTime) / timeSpan) * (width - pad.left - pad.right); const y = pad.top + (high - last.value) / range * (height - pad.top - pad.bottom); ctx.fillStyle = item.color; ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill(); }
  });
  ctx.fillStyle = '#6b7585'; ctx.textAlign = 'left'; ctx.fillText(formatTime(minTime, false), pad.left, height - 6);
  ctx.textAlign = 'right'; ctx.fillText(formatTime(maxTime, false), width - pad.right, height - 6);
}

function redrawLiveChart() {
  ['temperature', 'humidity', 'light'].forEach((metric) => drawChart($(`#live-${metric}-chart`), [{ points: state.chartPoints[metric], color: metricMeta[metric].color }], { empty: $(`#${metric}-chart-empty`), live: true }));
  drawChart($('#live-confidence-chart'), [
    { points: state.chartPoints.main_confidence, color: '#3166c4' },
    { points: state.chartPoints.k230_confidence, color: '#087f72' },
    { points: state.chartPoints.vision_threshold, color: '#b77515', dashed: true, step: true },
  ], { empty: $('#confidence-chart-empty'), low: 0, high: 1, live: true });
}

function redrawHistoryChart(metric = $('#history-metric')?.value || 'temperature') {
  const seriesByDevice = new Map();
  state.historyRecords.forEach((record) => {
    if (!seriesByDevice.has(record.device_id)) seriesByDevice.set(record.device_id, []);
    seriesByDevice.get(record.device_id).push({ time: record.event_time, value: Number(record.value), quality: record.quality });
  });
  const colors = [metricInfo(metric).color, '#c292ff', '#68d6a3', '#ff985e'];
  drawChart($('#history-chart'), [...seriesByDevice.values()].map((points, index) => ({ points, color: colors[index % colors.length] })), { empty: $('#history-empty') });
  const units = [...new Set(state.historyRecords.map((record) => record.unit))].join(' / ') || metricInfo(metric).unit;
  $('#history-title').textContent = `${metricInfo(metric).label}历史${units ? ` / ${units}` : ''}`;
}

function updateHistoryTable(records) {
  const body = $('#history-table');
  if (!body) return;
  body.innerHTML = records.length ? records.slice().reverse().map((item) => `<tr><td>${formatDate(item.event_time)} ${formatTime(item.event_time)}<small class="table-note">接收 ${formatTime(item.ingest_time)}</small></td><td>${escapeHtml(item.device_id)}</td><td>${Number(item.value).toFixed(item.metric === 'light' ? 0 : 2)} ${escapeHtml(item.unit)}</td><td><span class="quality ${qualityClass(item.quality)}">${qualityLabel(item.quality)}</span><small class="table-note">${escapeHtml(item.quality_reason || '')}</small></td><td>${escapeHtml(sourceModeLabel(item.source_mode))}</td><td>${item.sequence}<small class="table-note">${escapeHtml(item.boot_id)}</small></td><td>${escapeHtml(item.event_id)}</td></tr>`).join('') : '<tr><td colspan="7" class="table-empty">暂无历史数据</td></tr>';
}

function updateHistorySelectors() {
  const metricSelect = $('#history-metric');
  const deviceSelect = $('#history-device');
  const selectedMetric = metricSelect.value; const selectedDevice = deviceSelect.value;
  const metrics = new Set([...Object.keys(metricMeta), ...(state.overview?.latest_telemetry || []).map((item) => item.metric)]);
  const nonMetricCapabilities = new Set(['fsm', 'api', 'gpio', 'heartbeat', 'alarm', 'fan', 'video-decode', 'video-main', 'video-k230', 'vision-detection', 'execution-feedback']);
  consoleDevices(state.overview?.devices).forEach((device) => device.capabilities.forEach((capability) => { if (/^[a-z][a-z0-9_]*$/.test(capability) && !nonMetricCapabilities.has(capability)) metrics.add(capability); }));
  metricSelect.innerHTML = [...metrics].map((metric) => `<option value="${escapeHtml(metric)}">${escapeHtml(metricInfo(metric).label)}</option>`).join('');
  if (metrics.has(selectedMetric)) metricSelect.value = selectedMetric;
  deviceSelect.innerHTML = '<option value="">全部设备</option>' + consoleDevices(state.overview?.devices).map((device) => `<option value="${escapeHtml(device.device_id)}">${escapeHtml(device.display_name)}</option>`).join('');
  if ([...deviceSelect.options].some((option) => option.value === selectedDevice)) deviceSelect.value = selectedDevice;
}

function renderTransitions() {
  const body = $('#transition-table');
  if (!body) return;
  body.innerHTML = state.transitions.length ? state.transitions.slice(0, 100).map((item) => `<tr><td>${formatTime(item.occurred_at)}</td><td>${escapeHtml(item.from_state)} → ${escapeHtml(item.to_state)}</td><td>${Number(item.vision_threshold).toFixed(2)}</td><td>${escapeHtml(item.rule_version)}<small class="table-note">${escapeHtml(sourceModeLabel(item.source_mode))}</small></td><td class="message-cell">${escapeHtml(consoleText(item.reason))}<details class="audit-details"><summary>完整证据</summary><pre>${escapeHtml(JSON.stringify(item.evidence || {}, null, 2))}</pre></details></td><td>${escapeHtml(item.correlation_id)}</td></tr>`).join('') : '<tr><td colspan="6" class="table-empty">暂无状态流转</td></tr>';
}

function renderDeviceCards(devices = []) {
  devices = consoleDevices(devices);
  const target = $('#device-card-grid');
  target.innerHTML = devices.map((device) => `<article class="panel device-card"><div class="device-card-top"><div><h3>${escapeHtml(device.display_name)}</h3><span class="device-type">${escapeHtml(device.device_id)} · ${escapeHtml(device.node_type)}</span></div><span class="status-badge ${escapeHtml(device.status)}">${escapeHtml(deviceStatusLabel(device.status))}</span></div><div class="device-capabilities">${device.capabilities.map((capability) => `<span>${escapeHtml(capability)}</span>`).join('')}</div><div class="device-details"><div><small>协议</small><strong>${escapeHtml(device.protocol)}</strong></div><div><small>数据模式</small><strong>${escapeHtml(sourceModeLabel(device.source_mode))}</strong></div><div><small>最后心跳</small><strong>${formatTime(device.last_heartbeat)}</strong></div><div><small>状态说明</small><strong>${escapeHtml(device.health_message)}</strong></div></div></article>`).join('');
}

function renderLatestTelemetry(records = []) {
  const body = $('#latest-telemetry-table');
  if (!body) return;
  const sorted = [...records].sort((a, b) => String(a.metric).localeCompare(String(b.metric), 'zh-CN'));
  body.innerHTML = sorted.length ? sorted.map((item) => {
    const currentQuality = effectiveQuality(item);
    const latency = ageSeconds(item.event_time);
    return `<tr><td>${escapeHtml(item.metric)}</td><td>${Number(item.value).toLocaleString('zh-CN', { maximumFractionDigits: 3 })} ${escapeHtml(item.unit)}</td><td>${escapeHtml(item.device_id)}</td><td>${formatTime(item.event_time)}</td><td>${Number.isFinite(latency) ? `${latency.toFixed(latency < 10 ? 1 : 0)}s` : '--'}</td><td><span class="quality ${qualityClass(currentQuality)}">${qualityLabel(currentQuality)}</span></td><td>${escapeHtml(item.sequence)} / ${escapeHtml(item.boot_id)}</td></tr>`;
  }).join('') : '<tr><td colspan="7" class="table-empty">等待遥测数据</td></tr>';
}

function renderCommands(commands = state.commands) {
  const body = $('#command-table');
  if (!body) return;
  body.innerHTML = commands.length ? commands.slice(0, 100).map((item) => `<tr><td>${formatTime(item.requested_at)}</td><td>${escapeHtml(item.command_type)}</td><td><span class="command-status ${escapeHtml(item.status)}">${escapeHtml(commandStatusLabel(item.status))}</span></td><td>${escapeHtml(sourceModeLabel(item.source_mode))}</td><td>${escapeHtml(item.requested_by)}</td><td class="message-cell">${escapeHtml(item.execution_message || item.reason || '--')}</td><td>${escapeHtml(item.command_id)}</td></tr>`).join('') : '<tr><td colspan="7" class="table-empty">暂无命令</td></tr>';
}

function renderConfig(overview = state.overview) {
  const target = $('#config-grid');
  if (!target || !overview) return;
  const cfg = state.config || {};
  const values = [
    ['站点', overview.site_id],
    ['规则版本', overview.rules?.version || cfg.rules?.version || '--'],
    ['MQTT', overview.mqtt_status || '--'],
    ['当前视觉阈值', Number(overview.vision_threshold || 0).toFixed(2)],
    ['物理执行', cfg.allow_actuation ? (cfg.auto_actuation ? '允许 / 自动允许' : '允许 / 自动禁止') : '安全锁关闭'],
    ['本地 Fallback', overview.local_fallback_ready ? (overview.fallback_active ? '已就绪 / 已启用' : '已就绪') : (overview.fallback_active ? '已降级 / 未就绪' : '未接入')],
  ];
  target.innerHTML = values.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join('');
  const safety = $('#command-safety-copy');
  if (safety) safety.textContent = overview.simulation_mode
    ? '模拟模式只写入命令与审计记录，不会驱动物理 GPIO。'
    : `真实模式：控制令牌${cfg.control_token_required ? '必需' : '未启用'}；物理执行安全锁${cfg.allow_actuation ? '已开启' : '已关闭'}；只有收到硬件 completed 回执才视为完成。`;
}

function renderMonitorFeeds() {
  const target = $('#monitor-feeds'); if (!target) return;
  target.innerHTML = `<div class="camera-feed" data-view="k230"><div class="feed-top"><span>K230 · 唯一视觉采集通道</span><b id="monitor-k230-status">视频未接入</b></div><div class="feed-stage k230"><div class="feed-label"><span>K230 CAMERA</span><strong>等待视频源</strong></div><canvas class="bbox" id="monitor-bbox-k230" aria-hidden="true"></canvas></div><div class="feed-meta" id="monitor-meta-k230">等待 K230 视频帧与关联检测事件</div></div>`;
}

function renderAudit() {
  renderEvents(); renderAllEvents(); renderCommands(); renderTransitions(); updateEventSummary();
}

function sortAndLimit(items, timeField, limit = 100) {
  return items.sort((a, b) => new Date(b[timeField]) - new Date(a[timeField])).slice(0, limit);
}

async function resyncData(reason = '') {
  if (state.resyncPromise) return state.resyncPromise;
  state.needsResync = true;
  state.resyncPromise = (async () => {
    const [events, detections, transitions, commands, config, telemetry] = await Promise.all([
      api('/api/v1/events?limit=100'), api('/api/v1/detections?limit=100'), api('/api/v1/state/transitions?limit=100'), api('/api/v1/commands?limit=100'), api('/api/v1/config'), api('/api/v1/telemetry/history?limit=400'),
    ]);
    state.config = config;
    const overview = await api('/api/v1/overview');
    if (state.overview && (state.overview.site_id !== overview.site_id || state.overview.simulation_mode !== overview.simulation_mode)) {
      state.events = []; state.detections = []; state.transitions = []; state.commands = [];
      Object.keys(state.chartPoints).forEach((key) => { state.chartPoints[key] = []; });
    }
    state.events = sortAndLimit(mergeById(state.events, events, 'event_id'), 'occurred_at');
    state.detections = sortAndLimit(mergeById(state.detections, detections, 'event_id'), 'event_time', 100);
    state.transitions = sortAndLimit(mergeById(state.transitions, transitions, 'correlation_id'), 'occurred_at');
    state.commands = sortAndLimit(mergeById(state.commands, commands, 'command_id'), 'requested_at');
    if (!state.streamId || state.streamId !== overview.stream_id || overview.stream_sequence >= state.streamSequence) {
      state.streamId = overview.stream_id; state.streamSequence = overview.stream_sequence;
      applyOverview(overview);
    } else {
      renderAudit(); updateDetections();
    }
    telemetry.forEach((record) => pushPoint(record.metric, record));
    detections.forEach(pushDetectionPoint);
    transitions.forEach((item) => appendChartPoint('vision_threshold', { id: `transition:${item.correlation_id}`, time: item.occurred_at, value: Number(item.vision_threshold), quality: item.source_mode === 'simulated' ? 'simulated' : 'valid' }));
    redrawLiveChart(); updateTokenStatus();
    $('#heartbeat-threshold').textContent = config.heartbeat_timeout_seconds;
    state.needsResync = false;
    if (reason) showToast('实时数据与审计记录已重新同步');
  })().catch((error) => {
    state.needsResync = true;
    showToast(`数据同步失败：${error.message}`, 'error');
  }).finally(() => { state.resyncPromise = null; });
  return state.resyncPromise;
}

async function loadInitialData() {
  await resyncData();
  await loadHistory($('#history-metric')?.value || 'temperature');
}

async function loadHistory(metric) {
  const sequence = ++state.historyRequestSequence;
  const params = new URLSearchParams({ metric, limit: '120' });
  if ($('#history-device').value) params.set('device_id', $('#history-device').value);
  if ($('#history-source').value) params.set('source_mode', $('#history-source').value);
  try {
    const records = await api(`/api/v1/telemetry/history?${params}`);
    if (sequence !== state.historyRequestSequence) return;
    state.historyRecords = records;
    updateHistoryTable(records); redrawHistoryChart(metric);
  } catch (error) { showToast(`历史数据加载失败：${error.message}`, 'error'); }
}

function applyOverview(overview) {
  if (!overview) return;
  state.overview = overview; state.videoChannels = overview.video_channels || state.videoChannels;
  if (overview.generated_at) state.serverOffsetMs = new Date(overview.generated_at).getTime() - Date.now();
  if (overview.active_events) state.events = sortAndLimit(mergeById(state.events, overview.active_events, 'event_id'), 'occurred_at');
  if (overview.recent_transitions) state.transitions = sortAndLimit(mergeById(state.transitions, overview.recent_transitions, 'correlation_id'), 'occurred_at');
  if (overview.latest_commands) state.commands = sortAndLimit(mergeById(state.commands, overview.latest_commands, 'command_id'), 'requested_at');
  setModeBanner(overview); updateSystemNotice(overview); updateState(overview); updateSensors(overview); updateDeviceMiniList(overview.devices || []); renderDeviceCards(overview.devices || []); renderLatestTelemetry(overview.latest_telemetry || []); renderConfig(overview);
  if (overview.latest_detections) { state.detections = sortAndLimit(mergeById(state.detections, overview.latest_detections, 'event_id'), 'event_time', 100); overview.latest_detections.forEach(pushDetectionPoint); }
  (overview.latest_telemetry || []).forEach((record) => pushPoint(record.metric, record));
  appendChartPoint('vision_threshold', { id: `snapshot:${overview.stream_id}:${overview.stream_sequence}`, time: overview.generated_at, value: Number(overview.vision_threshold), quality: overview.state_trusted ? (overview.simulation_mode ? 'simulated' : 'valid') : 'estimated' });
  renderAudit(); updateHistorySelectors(); updateDetections(); redrawLiveChart();
  updateVideoChannels(state.videoChannels);
}

function updateVideoChannels(channels = []) {
  state.videoChannels = channels;
  ['k230'].forEach((view) => {
    const channel = channels.find((item) => item.source_view === view);
    $$(`.camera-feed[data-view="${view}"]`).forEach((feed) => {
      const stage = feed.querySelector('.feed-stage');
      if (!stage) return;
      let image = stage.querySelector('img.video-preview');
      if (channel?.frame_url && channelIsFresh(channel) && state.videoErrors[view] !== channel.frame_id) {
        const url = new URL(channel.frame_url, window.location.origin);
        if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/v1/video/')) return;
        if (!image || image.dataset.frameId !== channel.frame_id) {
          image?.remove(); delete feed.dataset.loadedFrameId; stage.classList.remove('has-video');
          image = document.createElement('img'); image.className = 'video-preview'; image.alt = `${sourceModeLabel(channel.source_mode)} ${view} 视频预览`; image.dataset.frameId = channel.frame_id;
          image.addEventListener('load', () => {
            if (image.dataset.frameId !== state.videoChannels.find((item) => item.source_view === view)?.frame_id || !image.isConnected) return;
            if (!image.naturalWidth) return;
            feed.dataset.loadedFrameId = channel.frame_id; stage.classList.add('has-video'); delete state.videoErrors[view]; renderFeedMetadata();
          });
          image.addEventListener('error', () => {
            if (!image.isConnected) return;
            state.videoErrors[view] = channel.frame_id;
            $$(`.camera-feed[data-view="${view}"]`).forEach((node) => { node.querySelector('img.video-preview')?.remove(); delete node.dataset.loadedFrameId; node.querySelector('.feed-stage').classList.remove('has-video'); });
            renderFeedMetadata();
          });
          stage.prepend(image); image.src = url.href;
        }
      } else {
        image?.remove(); delete feed.dataset.loadedFrameId; stage.classList.remove('has-video');
      }
    });
  });
  const badge = $('#monitor-mode-badge');
  if (badge) {
    const fresh = channels.some((item) => item.source_view === 'k230' && channelIsFresh(item) && state.videoErrors.k230 !== item.frame_id);
    badge.textContent = fresh ? `K230 · ${state.overview?.simulation_mode ? '模拟' : '真实'}预览帧` : (state.overview?.simulation_mode ? '视频未接入 · AI 元数据为模拟' : 'K230 视频预览尚未接入');
  }
  renderFeedMetadata();
}

function mergeById(items, additions, key) {
  const map = new Map(items.map((item) => [item[key], item]));
  additions.forEach((item) => {
    const previous = map.get(item[key]);
    if (previous?.acknowledged && item.acknowledged === false) return;
    if (key === 'command_id' && previous) {
      const previousAt = previous.status_history?.at(-1)?.at || previous.requested_at;
      const nextAt = item.status_history?.at(-1)?.at || item.requested_at;
      if (new Date(previousAt) > new Date(nextAt)) return;
    }
    map.set(item[key], item);
  });
  return [...map.values()];
}

function handleRealtime(message) {
  if (!message) return;
  state.lastRealtimeAt = Date.now();
  if (message.server_time) state.serverOffsetMs = new Date(message.server_time).getTime() - Date.now();
  if (message.type === 'ping') return;
  if (message.type === 'resync_required') {
    state.needsResync = true;
    resyncData('slow-client');
    return;
  }
  if (message.stream_id && state.streamId && message.stream_id !== state.streamId) { state.streamId = message.stream_id; state.streamSequence = 0; state.needsResync = true; }
  if (message.stream_id) state.streamId = message.stream_id;
  if (message.stream_sequence && message.stream_sequence < state.streamSequence) return;
  if (message.type !== 'snapshot' && message.stream_sequence && message.stream_sequence === state.streamSequence) return;
  if (message.stream_sequence && state.streamSequence && message.stream_sequence > state.streamSequence + 1) { state.needsResync = true; }
  if (message.stream_sequence) state.streamSequence = Math.max(state.streamSequence, message.stream_sequence);
  if (state.needsResync) resyncData('realtime');
  if (message.type === 'snapshot') { applyOverview(message.data); setConnection(true); return; }
  if (message.data?.site_id && !inCurrentScope(message.data)) return;
  if (message.type === 'telemetry') {
    const record = message.data; pushPoint(record.metric, record);
    if (isUsable(record) && state.overview) {
      const current = (state.overview.latest_telemetry || []).filter((item) => !(item.metric === record.metric && item.device_id === record.device_id)); current.push(record); state.overview.latest_telemetry = current;
      updateSensors(state.overview); renderLatestTelemetry(current); updateHistorySelectors();
    }
    redrawLiveChart(); return;
  }
  if (message.type === 'quality_issue') { showToast('收到重复或无效输入，已排除出实时决策', 'error'); return; }
  if (message.type === 'detection') { pushDetectionPoint(message.data); state.detections = sortAndLimit(mergeById(state.detections, [message.data], 'event_id'), 'event_time', 100); updateDetections(); redrawLiveChart(); return; }
  if (message.type === 'event') { state.events = mergeById(state.events, [message.data], 'event_id').sort((a, b) => new Date(b.occurred_at) - new Date(a.occurred_at)); renderEvents(); renderAllEvents(); updateEventSummary(); if (message.data.severity === 'critical') showToast(message.data.title, 'critical'); return; }
  if (message.type === 'event_acknowledged') { state.events = state.events.map((event) => event.event_id === message.data.event_id ? message.data : event); renderEvents(); renderAllEvents(); updateEventSummary(); return; }
  if (message.type === 'state_transition') { state.transitions = sortAndLimit(mergeById(state.transitions, [message.data], 'correlation_id'), 'occurred_at'); if (state.overview) { Object.assign(state.overview, { current_state: message.data.to_state, state_since: message.data.occurred_at, state_reason: message.data.reason, state_evidence: message.data.evidence, vision_threshold: message.data.vision_threshold }); updateState(state.overview); renderConfig(); } appendChartPoint('vision_threshold', { time: message.data.occurred_at, value: Number(message.data.vision_threshold), quality: message.data.source_mode === 'simulated' ? 'simulated' : 'valid' }); renderTransitions(); updateEventSummary(); redrawLiveChart(); return; }
  if (message.type === 'command' || message.type === 'command_feedback') { state.commands = [message.data, ...state.commands.filter((item) => item.command_id !== message.data.command_id)].slice(0, 100); updateEventSummary(); renderCommands(); $('#last-command').textContent = `${message.data.command_type} · ${commandStatusLabel(message.data.status)} · ${message.data.execution_message || ''}`; return; }
  if (message.type === 'video') { updateVideoChannels(message.data); return; }
  if (message.type === 'health' && state.overview) { state.overview.devices = message.data; updateDeviceMiniList(message.data); renderDeviceCards(message.data); }
  if (message.type === 'device_registered') { state.needsResync = true; resyncData(); }
}

function connectWebSocket() {
  if (state.socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(state.socket.readyState)) return;
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
  state.socket = new WebSocket(`${protocol}://${window.location.host}/ws`);
  state.socket.addEventListener('open', () => { setConnection(true); if (state.reconnectTimer) clearTimeout(state.reconnectTimer); });
  state.socket.addEventListener('message', (event) => { try { handleRealtime(JSON.parse(event.data)); } catch (error) { console.warn('Invalid realtime message', error); } });
  state.socket.addEventListener('close', () => { state.needsResync = true; setConnection(false); state.reconnectTimer = setTimeout(connectWebSocket, 1800); });
  state.socket.addEventListener('error', () => { setConnection(false); });
}

async function acknowledgeEvent(eventId) {
  try { const event = await api(`/api/v1/events/${encodeURIComponent(eventId)}/acknowledge`, { method: 'POST', headers: controlHeaders() }); state.events = sortAndLimit(mergeById(state.events, [event], 'event_id'), 'occurred_at'); renderAudit(); } catch (error) { showToast(`确认失败：${error.message}`, 'error'); }
}

async function sendCommand(commandType) {
  try {
    const command = await api('/api/v1/commands', { method: 'POST', headers: controlHeaders(), body: JSON.stringify({ command_type: commandType, reason: state.overview?.simulation_mode ? '前端模拟测试（不驱动物理硬件）' : '操作员请求，需执行器安全开关与硬件回执', requested_by: 'dashboard-operator', request_id: `ui-${commandType}-${Date.now()}` }) });
    state.commands = sortAndLimit(mergeById(state.commands, [command], 'command_id'), 'requested_at'); updateEventSummary(); renderCommands(); $('#last-command').textContent = `${command.command_type} · ${commandStatusLabel(command.status)} · ${command.execution_message}`;
    showToast(state.overview?.simulation_mode ? '模拟命令已记录，未驱动物理硬件' : `执行请求状态：${commandStatusLabel(command.status)}`, command.status === 'failed' ? 'error' : 'info');
  } catch (error) { showToast(`命令失败：${error.message}`, 'error'); }
}

function navigate(page) {
  state.page = page;
  $$('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.page === page));
  $$('.page').forEach((item) => item.classList.toggle('active', item.id === `page-${page}`));
  const title = { overview: '态势总览', monitor: '实时监控', events: '事件中心', devices: '设备与质量', history: '历史数据', integrations: '接入与接口' }[page] || '态势总览';
  $('#page-title').textContent = title;
  $('.sidebar')?.classList.remove('open');
  redrawLiveChart(); renderFeedMetadata();
  if (page === 'history') loadHistory($('#history-metric')?.value || 'temperature');
  if (page === 'events') renderAudit();
}

function bindEvents() {
  $$('.nav-item').forEach((button) => button.addEventListener('click', () => navigate(button.dataset.page)));
  $$('[data-page-jump]').forEach((button) => button.addEventListener('click', () => navigate(button.dataset.pageJump)));
  $('#mobile-menu')?.addEventListener('click', () => $('.sidebar')?.classList.toggle('open'));
  $('#event-filter')?.addEventListener('change', renderAllEvents);
  $('#refresh-events')?.addEventListener('click', async () => { try { state.events = await api('/api/v1/events?limit=100'); renderEvents(); renderAllEvents(); updateEventSummary(); } catch (error) { showToast(error.message, 'error'); } });
  $('#refresh-history')?.addEventListener('click', () => loadHistory($('#history-metric').value));
  $('#history-metric')?.addEventListener('change', () => loadHistory($('#history-metric').value));
  $('#history-device')?.addEventListener('change', () => loadHistory($('#history-metric').value));
  $('#history-source')?.addEventListener('change', () => loadHistory($('#history-metric').value));
  $$('.command-buttons button').forEach((button) => button.addEventListener('click', () => sendCommand(button.dataset.command)));
  $('#save-control-token')?.addEventListener('click', () => { const input = $('#control-token'); setControlToken(input?.value.trim() || ''); if (input) input.value = ''; showToast(getControlToken() ? '控制令牌已应用于当前标签页' : '未保存空令牌'); });
  $('#clear-control-token')?.addEventListener('click', () => { setControlToken(''); const input = $('#control-token'); if (input) input.value = ''; showToast('当前标签页的控制令牌已清除'); });
  window.addEventListener('resize', () => { redrawLiveChart(); redrawHistoryChart($('#history-metric')?.value || 'temperature'); renderFeedMetadata(); });
}

document.addEventListener('DOMContentLoaded', () => {
  renderMonitorFeeds(); updateClock(); updateTokenStatus(); bindEvents();
  setInterval(updateClock, 1000);
  setInterval(() => { if (state.overview) { updateSensors(state.overview); updateDetections(); updateVideoChannels(state.videoChannels); renderLatestTelemetry(state.overview.latest_telemetry || []); redrawLiveChart(); if (state.lastRealtimeAt && Date.now() - state.lastRealtimeAt > 15000) setConnection(false); } }, 1000);
  loadInitialData(); connectWebSocket();
});

