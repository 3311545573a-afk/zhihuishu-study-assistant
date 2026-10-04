'use strict';

const $ = (id) => document.getElementById(id);

const DOT_CLASS = {
  '未运行': 'idle',
  '启动中': 'starting',
  '监控中': 'monitoring',
  '正在答题': 'answering',
  '等待你在浏览器处理': 'waiting',
  '已停止': 'stopped',
  '异常退出': 'error',
};

const MAX_LINES = 500;
// 后端 label 已经说清楚了这两种状态，detail 只是同义复述，顶栏不重复显示。
const REDUNDANT_DETAIL = new Set(['尚未启动助手', '助手正在运行']);
const SELECTOR_IDS = ['dialog', 'question', 'option', 'selected', 'submit',
  'continue', 'video', 'player', 'next', 'lesson'];

const logBox = $('log');
const logItems = [];
let lastSeq = 0;
let lastStatus = null;
let statusTimer = null;
let busy = false;

async function api(path, options) {
  let response;
  try {
    response = await fetch(path, { cache: 'no-store', ...(options || {}) });
  } catch (error) {
    return { ok: false, error: '无法连接控制台：' + error.message, httpStatus: 0 };
  }
  let data;
  try {
    data = await response.json();
  } catch (error) {
    data = { ok: false, error: '控制台返回了非 JSON 内容（HTTP ' + response.status + '）' };
  }
  data.httpStatus = response.status;
  return data;
}

function message(el, text, isError) {
  el.textContent = text || '';
  el.style.color = isError ? '#c53030' : '#2f855a';
}

function formatUptime(seconds) {
  const total = Math.max(0, Math.round(seconds || 0));
  if (total < 60) return total + ' 秒';
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return minutes + ' 分 ' + (total % 60) + ' 秒';
  return Math.floor(minutes / 60) + ' 小时 ' + (minutes % 60) + ' 分';
}

/* ---------- 状态 ---------- */

function renderStatus(status) {
  lastStatus = status;
  $('status-label').textContent = status.label || '未知';
  $('status-detail').textContent = REDUNDANT_DETAIL.has(status.detail) ? '' : (status.detail || '');
  $('status-dot').className = 'dot ' + (DOT_CLASS[status.label] || 'idle');
  const parts = [];
  if (status.pid) parts.push('PID ' + status.pid);
  if (status.running) parts.push('已运行 ' + formatUptime(status.uptime));
  // 只在后端确实给了布尔值时判断配置状态，避免局部状态对象造成“配置无效”假警报。
  if (typeof status.config_ok === 'boolean') {
    parts.push(status.config_ok ? '配置有效' : '配置无效');
    // 配置本身就不合法时不再对 AI 段下结论，否则会误报“AI 未填齐”。
    if (status.config_ok && typeof status.ai_ready === 'boolean') {
      parts.push(status.ai_ready ? 'AI 已填齐' : 'AI 未填齐');
    }
  }
  $('status-meta').textContent = parts.join(' · ');
  const warning = $('status-warning');
  if (status.config_stale) {
    warning.hidden = false;
    warning.textContent = '配置已在 ' + (status.config_changed_at || '') +
      ' 改动，助手是改动前启动的，点“重启助手”才生效';
  } else {
    warning.hidden = true;
    warning.textContent = '';
  }
  $('start').disabled = busy || status.running || status.config_ok === false;
  $('stop').disabled = busy || !status.running;
  $('restart').disabled = busy || !status.running;
  $('send-text').disabled = busy || !status.running || !$('stdin').value.trim();
  $('send-enter').disabled = busy || !status.running;
  $('send-quit').disabled = busy || !status.running;
  if (status.config_ok === false) {
    message($('action-message'), '当前 config.json 未通过校验：' + (status.config_message || ''), true);
  }
  renderVideo(status.video, status.video_pending);
}

async function pollStatus() {
  const data = await api('/api/status');
  if (data.ok) {
    renderStatus(data);
  } else {
    $('status-label').textContent = '控制台未响应';
    $('status-dot').className = 'dot error';
    $('status-detail').textContent = data.error || '';
  }
  clearTimeout(statusTimer);
  if (!document.hidden) statusTimer = setTimeout(pollStatus, 2000);
}

/* ---------- 控制 ---------- */

async function control(path, body, label) {
  busy = true;
  renderStatus(lastStatus || {});
  const data = await api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  busy = false;
  message($('action-message'), data.message || data.error || '', !data.ok);
  if (data.status) renderStatus(data.status);
  await pollStatus();
  return data;
}

$('start').addEventListener('click', () => control('/api/start', {}, '启动'));
$('stop').addEventListener('click', () => control('/api/stop', {}, '停止'));
$('restart').addEventListener('click', () => control('/api/restart', {}, '重启'));
$('send-text').addEventListener('click', () => {
  const text = $('stdin').value;
  if (!text.trim()) return;
  control('/api/stdin', { text });
  $('stdin').value = '';
});
$('send-enter').addEventListener('click', () => control('/api/stdin', { text: '' }, '回车'));
$('send-quit').addEventListener('click', () => control('/api/stdin', { text: 'q' }, '退出'));
$('stdin').addEventListener('input', () => {
  $('send-text').disabled = !(lastStatus && lastStatus.running) || !$('stdin').value.trim();
});
$('stdin').addEventListener('keydown', (event) => {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  // 框里没内容时等同于「发送回车（继续）」，和按钮行为一致。
  control('/api/stdin', { text: $('stdin').value });
  $('stdin').value = '';
});

/* ---------- 日志 ---------- */

function matchesFilter(item) {
  const needle = $('filter').value.trim().toLowerCase();
  return !needle || item.text.toLowerCase().includes(needle);
}

function renderLine(item) {
  const line = document.createElement('span');
  line.className = 'line src-' + (item.source || 'log');
  if (/错误|失败|异常|Error|Traceback/.test(item.text)) line.classList.add('error');
  const time = document.createElement('span');
  time.className = 'time';
  time.textContent = (item.time || '') + ' ';
  const source = document.createElement('span');
  source.className = 'src';
  source.textContent = item.source === 'app' ? '[程序] ' : (item.source === 'console' ? '[控制台] ' : '');
  const text = document.createElement('span');
  text.className = 'text';
  text.textContent = item.text;
  line.append(time, source, text);
  return line;
}

function refreshLogView() {
  logBox.textContent = '';
  // 只有日志本身为空才收高；筛选到 0 条时保持原高度，避免输入筛选词时框高跳动。
  logBox.classList.toggle('is-empty', logItems.length === 0);
  const visible = logItems.filter(matchesFilter);
  if (!visible.length) {
    const empty = document.createElement('span');
    empty.className = 'empty';
    empty.textContent = logItems.length ? '没有匹配的行。' : '暂无日志。启动助手后这里会实时滚动显示。';
    logBox.append(empty);
    return;
  }
  const fragment = document.createDocumentFragment();
  visible.forEach((item) => fragment.append(renderLine(item)));
  logBox.append(fragment);
  if ($('autoscroll').checked) logBox.scrollTop = logBox.scrollHeight;
}

function appendLog(item) {
  if (typeof item.seq === 'number') {
    if (item.seq <= lastSeq) return;   // SSE 重连后服务器会重发历史，这里去重
    lastSeq = item.seq;
  }
  logItems.push(item);
  while (logItems.length > MAX_LINES) logItems.shift();
  // appendLog 不走 refreshLogView，这里必须自己把空态收高还原，否则有日志了框还只有 120px。
  logBox.classList.remove('is-empty');
  if (matchesFilter(item)) {
    if (logBox.querySelector('.empty')) logBox.textContent = '';  // 去掉“暂无日志”占位
    logBox.append(renderLine(item));
  }
  while (logBox.childElementCount > MAX_LINES) logBox.firstElementChild.remove();
  $('log-count').textContent = logItems.length + ' 行';
  if ($('autoscroll').checked) logBox.scrollTop = logBox.scrollHeight;
}

function setLogState(text, warn) {
  const el = $('log-state');
  el.textContent = text;
  el.style.color = warn ? '#9c4221' : '';
}

function connectLog() {
  const source = new EventSource('/api/logs/stream');
  source.onopen = () => setLogState('已连接', false);
  source.onmessage = (event) => {
    try {
      appendLog(JSON.parse(event.data));
    } catch (error) {
      /* 忽略单条解析失败 */
    }
  };
  source.onerror = () => setLogState('连接断开，正在重连…', true);
}

$('filter').addEventListener('input', refreshLogView);
$('clear-view').addEventListener('click', () => {
  logItems.length = 0;
  $('log-count').textContent = '0 行';
  refreshLogView();
});

/* ---------- 视频状态与手动控制 ---------- */

function formatRate(rate) {
  return (Math.round(Number(rate) * 100) / 100) + 'x';
}

function formatClock(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  return Math.floor(total / 60) + ':' + String(total % 60).padStart(2, '0');
}

let lastVideo = null;  // 最后一次非空状态：状态超龄（长 AI 调用）时仍要知道按钮该显示"开始"还是"暂停"
let lastPid = null;    // 助手进程号：换进程就是新的一轮，旧缓存不能再拿来当现状

function renderVideo(video, pending) {
  const info = $('video-info');
  const pauseButton = $('video-pause');
  const running = !!(lastStatus && lastStatus.running);
  const pid = (lastStatus && lastStatus.pid) || null;
  if (pid !== lastPid) {
    // 控制台的「重启助手」在一次请求里完成，页面可能看不到 running=false；
    // 用进程号换没换过来判断"这是新的一轮"，旧缓存必须作废。
    lastPid = pid;
    lastVideo = null;
  }
  if (video) lastVideo = video;
  if (!running) lastVideo = null;
  // 助手没在跑就不该再说"等待助手响应"：那条命令可能永远等不到应用
  $('video-pending').hidden = !(pending && running);
  // 助手没在跑时，留下的状态文件不算"现状"：设计里说好被强杀后不显示过期倍速。
  const fresh = running ? video : null;
  const known = fresh || lastVideo;  // 只用来决定控件，不参与文字：陈旧的数据不该冒充当前值
  if (fresh) {
    const parts = [];
    if (typeof fresh.rate === 'number') parts.push(formatRate(fresh.rate));
    if (typeof fresh.paused === 'boolean') {
      // manual_paused 是"指令"，paused 才是"现状"：刚点暂停的那一轮状态写在动作之前，
      // 这时如实显示"播放中"，下一轮就对了。
      parts.push(fresh.manual_paused && fresh.paused ? '已暂停（手动）'
        : (fresh.paused ? '已暂停' : '播放中'));
    }
    if (typeof fresh.time === 'number' && typeof fresh.duration === 'number' && fresh.duration > 0) {
      parts.push(formatClock(fresh.time) + ' / ' + formatClock(fresh.duration));
    }
    if (fresh.lesson) parts.push(fresh.lesson);
    if (fresh.requested_rate && typeof fresh.rate === 'number'
        && Math.abs(fresh.requested_rate - fresh.rate) > 0.01) {
      parts.push('请求 ' + formatRate(fresh.requested_rate) + ' / 实际 ' + formatRate(fresh.rate));
    }
    info.textContent = parts.length ? parts.join(' · ') : '无数据';
    info.title = fresh.lesson || '';
  } else {
    info.textContent = running ? '无数据（等助手回报）' : '无数据';
    info.title = '';
  }
  // 按钮文案必须用"最后一次已知状态"：否则暂停中遇到长 AI 调用、状态一超龄，
  // 「开始视频」会退回「暂停视频」，用户既恢复不了也停不下来。
  pauseButton.textContent = known && known.manual_paused ? '开始视频' : '暂停视频';
  pauseButton.disabled = !running || busy;
}

async function sendVideoCommand(action, value) {
  if (busy) return;
  busy = true;
  $('video-pause').disabled = true;
  try {
    const data = await api('/api/video', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: action, value: value === undefined ? null : value }),
    });
    if (!data.ok) {
      message($('action-message'), data.error || data.message || '操作失败', true);
    } else {
      // 成功也回显一句：只靠按钮文案和"等待助手响应…"不够明确，用户需要知道命令发出去了。
      message($('action-message'), data.message || '已发送', false);
      renderVideo(data.video, data.video_pending);
    }
  } finally {
    busy = false;
    await pollStatus();
  }
}

function videoPaused() {
  // 口径与 renderVideo 一致：助手没在跑时就没有"当前状态"，不能凭过期缓存决定动作
  const running = !!(lastStatus && lastStatus.running);
  const known = running ? ((lastStatus && lastStatus.video) || lastVideo) : null;
  return !!(known && known.manual_paused);
}

$('video-pause').addEventListener('click', () => {
  sendVideoCommand(videoPaused() ? 'resume' : 'pause');
});

/* ---------- 诊断文件清理（只在点按钮并确认后执行） ---------- */

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return value + ' B';
  if (value < 1048576) return (value / 1024).toFixed(1) + ' KB';
  if (value < 1073741824) return (value / 1048576).toFixed(1) + ' MB';
  return (value / 1073741824).toFixed(2) + ' GB';
}

/* 目录名形如 20261003-101010-000003，这里只取“月-日 时:分”给人看，原文放到 title。 */
function formatStamp(name) {
  const text = String(name || '');
  if (text.length < 13) return text;
  return text.slice(4, 6) + '-' + text.slice(6, 8) + ' ' + text.slice(9, 11) + ':' + text.slice(11, 13);
}

function renderDiagnostics(data) {
  const usage = data.usage || {};
  $('diag-summary').textContent = usage.count
    ? `${usage.count} 个文件夹 / ${formatBytes(usage.bytes)}（最新 ${formatStamp(usage.newest)}，` +
      `最早 ${formatStamp(usage.oldest)}）`
    : '暂无';
  // 原始目录名不截断，悬停可看全。
  $('diag-summary').title = usage.count
    ? '最新 ' + usage.newest + '，最早 ' + usage.oldest
    : '';
  const retention = data.retention || {};
  $('diag-policy').textContent =
    `保留 ${retention.keep_days} 天 / ${retention.keep_count} 个 / ${retention.keep_mb} MB`;
}

async function loadDiagnostics() {
  const data = await api('/api/diagnostics');
  if (!data.usage) {
    $('diag-summary').textContent = '读取失败';
    $('diag-summary').title = '';
    return data;
  }
  renderDiagnostics(data);
  return data;
}

$('diag-check').addEventListener('click', async () => {
  $('diag-confirm').hidden = true;
  const data = await loadDiagnostics();
  if (!data.usage) {
    message($('diag-message'), data.error || '读取诊断目录失败', true);
    return;
  }
  if (!data.deleted_count) {
    message($('diag-message'),
      `没有需要清理的，当前 ${data.usage.count} 个 / ${formatBytes(data.usage.bytes)}。`, false);
    return;
  }
  $('diag-confirm-text').textContent =
    `将删除 ${data.deleted_count} 个文件夹，释放 ${formatBytes(data.freed_bytes)}；` +
    `保留 ${data.kept_count} 个 / ${formatBytes(data.kept_bytes)}。`;
  $('diag-confirm').hidden = false;
  message($('diag-message'), '', false);
});

$('diag-confirm-no').addEventListener('click', () => {
  $('diag-confirm').hidden = true;
  message($('diag-message'), '已取消，没有删除任何文件。', false);
});

$('diag-confirm-yes').addEventListener('click', async () => {
  const button = $('diag-confirm-yes');
  button.disabled = true;
  const data = await api('/api/diagnostics/cleanup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  button.disabled = false;
  $('diag-confirm').hidden = true;
  if (data.ok) {
    message($('diag-message'),
      `已删除 ${data.deleted_count} 个文件夹，释放 ${formatBytes(data.freed_bytes)}；` +
      `保留 ${data.kept_count} 个 / ${formatBytes(data.kept_bytes)}。`, false);
  } else {
    message($('diag-message'), (data.errors || []).join('；') || '清理失败', true);
  }
  await loadDiagnostics();
});

/* ---------- 配置 ---------- */

let configLoaded = false;

function fillConfig(data) {
  const config = data.config || {};
  const ai = config.ai || {};
  const selectors = config.selectors || {};
  const diagnostics = config.diagnostics || {};
  $('course_url').value = config.course_url || '';
  $('browser_channel').value = config.browser_channel || 'auto';
  $('poll_seconds').value = config.poll_seconds;
  $('ai_base_url').value = ai.base_url || '';
  $('ai_model').value = ai.model || '';
  $('ai_api_key').value = '';
  $('ai_api_key').placeholder = data.api_key_set
    ? '留空表示不修改（当前 ' + data.api_key_mask + '）'
    : '尚未配置，请填写密钥';
  $('ai_min_confidence').value = ai.min_confidence;
  $('ai_timeout_seconds').value = ai.timeout_seconds;
  $('diag_keep_days').value = diagnostics.keep_days != null ? diagnostics.keep_days : 7;
  $('diag_keep_count').value = diagnostics.keep_count != null ? diagnostics.keep_count : 50;
  $('diag_keep_mb').value = diagnostics.keep_mb != null ? diagnostics.keep_mb : 200;
  SELECTOR_IDS.forEach((name) => {
    $('sel_' + name).value = selectors[name] || '';
  });
  configLoaded = true;
}

async function loadConfig(force) {
  if (configLoaded && !force) return;
  const data = await api('/api/config');
  if (!data.ok) {
    message($('config-message'), data.error || '读取配置失败', true);
    return;
  }
  fillConfig(data);
  message($('config-message'), '已读取当前 config.json。', false);
}

$('config-toggle').addEventListener('click', () => {
  const form = $('config-form');
  form.hidden = !form.hidden;
  $('config-toggle').textContent = '配置 config.json ' + (form.hidden ? '▾' : '▴');
  if (!form.hidden) loadConfig(false);
});

$('config-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const selectors = {};
  SELECTOR_IDS.forEach((name) => {
    selectors[name] = $('sel_' + name).value;
  });
  const payload = {
    course_url: $('course_url').value.trim(),
    browser_channel: $('browser_channel').value,
    poll_seconds: Number($('poll_seconds').value),
    ai: {
      base_url: $('ai_base_url').value.trim(),
      model: $('ai_model').value.trim(),
      api_key: $('ai_api_key').value.trim(),
      min_confidence: Number($('ai_min_confidence').value),
      timeout_seconds: Number($('ai_timeout_seconds').value),
    },
    selectors: selectors,
    diagnostics: {
      keep_days: Number($('diag_keep_days').value),
      keep_count: Number($('diag_keep_count').value),
      keep_mb: Number($('diag_keep_mb').value),
    },
  };
  const saveButton = $('save-config');
  saveButton.disabled = true;
  const data = await api('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  saveButton.disabled = false;
  message($('config-message'), data.message || data.error || '', !data.ok);
  if (data.ok) {
    $('ai_api_key').value = '';
    await loadConfig(true);
    if (lastStatus && lastStatus.running) {
      message($('config-message'), (data.message || '配置已保存') +
        '；助手正在运行，点“重启助手”后生效。', false);
    }
    await pollStatus();
  }
});

/* ---------- 启动 ---------- */

refreshLogView();
loadConfig(false);
loadDiagnostics();
pollStatus();
connectLog();
