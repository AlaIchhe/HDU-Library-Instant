import argparse
import json
import os
import re
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from instant_book import (
    DEFAULT_BOOK_DAYS,
    DEFAULT_CONFIG,
    DEFAULT_MAX_TRIALS,
    DEFAULT_RETRY_DELAY,
    load_config,
    normalize_execute_at,
    parse_plan,
    run_booking,
)

# 自动化模块（编译失败时优雅降级）
try:
    from cookie_browser import find_hdu_cookie, load_cookies_into_session
    HAS_COOKIE_BROWSER = True
except ImportError:
    HAS_COOKIE_BROWSER = False

    def find_hdu_cookie(preferred_browser="auto"):  # noqa: ARG001
        return None

    def load_cookies_into_session(session, preferred_browser="auto"):  # noqa: ARG001
        return False

try:
    from safe_cookie import safe_find_cookie as _safe_find_hdu_cookie
    from safe_cookie import save_cookie_file
except ImportError:
    def _safe_find_hdu_cookie(preferred_browser="auto"):  # noqa: ARG001
        return None, ["cookie 模块不可用"]

    def save_cookie_file(cookie_str):  # noqa: ARG001
        return False

LIBRARY_URL = "https://hdu.huitu.zhishulib.com"


try:
    from scheduler import Scheduler
    HAS_SCHEDULER = True
except ImportError:
    HAS_SCHEDULER = False


HOST = "127.0.0.1"
PORT = 8765
JOBS = {}
JOBS_LOCK = threading.Lock()
SCHEDULER = None  # Scheduler 实例，main() 中初始化


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HDU 图书馆即时预约</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f2;
      --panel: #ffffff;
      --ink: #202124;
      --muted: #667085;
      --line: #d9dfd0;
      --accent: #216869;
      --accent-strong: #174f50;
      --danger: #b42318;
      --ok: #157347;
      --shadow: 0 12px 36px rgba(21, 42, 30, 0.10);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    .app {
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr;
    }

    header {
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.88);
      backdrop-filter: blur(14px);
    }

    .bar {
      max-width: 1180px;
      margin: 0 auto;
      padding: 18px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }

    h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 720;
      letter-spacing: 0;
    }

    .status {
      min-width: 116px;
      padding: 7px 12px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: #fdfefa;
      color: var(--muted);
      font-size: 13px;
      text-align: center;
    }

    .status.running {
      color: var(--accent-strong);
      border-color: rgba(33, 104, 105, 0.28);
      background: #edf7f4;
    }

    .status.error {
      color: var(--danger);
      border-color: rgba(180, 35, 24, 0.25);
      background: #fff3f0;
    }

    main {
      width: 100%;
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px;
      display: grid;
      grid-template-columns: minmax(360px, 440px) minmax(0, 1fr);
      gap: 18px;
      align-items: stretch;
    }

    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }

    .form {
      padding: 20px;
    }

    .form h2,
    .logs h2 {
      margin: 0 0 16px;
      font-size: 15px;
      font-weight: 700;
      letter-spacing: 0;
    }

    .grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
    }

    label {
      display: grid;
      gap: 7px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.25;
    }

    input {
      width: 100%;
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 0 10px;
      color: var(--ink);
      background: #fff;
      font: inherit;
      outline: none;
    }

    input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(33, 104, 105, 0.14);
    }

    .wide { grid-column: 1 / -1; }

    .date-row {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
    }

    .date-row input,
    .dry-row input {
      position: absolute;
      opacity: 0;
      pointer-events: none;
    }

    .date-row span,
    .dry-row span {
      display: flex;
      height: 38px;
      align-items: center;
      justify-content: center;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fff;
      color: var(--ink);
      cursor: pointer;
      user-select: none;
      transition: border-color 120ms ease, background 120ms ease, color 120ms ease;
    }

    .date-row input:checked + span,
    .dry-row input:checked + span {
      border-color: var(--accent);
      background: #edf7f4;
      color: var(--accent-strong);
      font-weight: 700;
    }

    .dry-row {
      display: grid;
      grid-template-columns: 1fr;
    }

    .actions {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-top: 18px;
    }

    button {
      height: 40px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      font-weight: 680;
      cursor: pointer;
    }

    button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
    }

    button:hover:not(:disabled) {
      border-color: var(--accent);
    }

    button.primary:hover:not(:disabled) {
      background: var(--accent-strong);
    }

    button:disabled {
      cursor: wait;
      opacity: 0.62;
    }

    .logs {
      min-height: 520px;
      display: grid;
      grid-template-rows: auto 1fr;
      padding: 20px;
    }

    pre {
      margin: 0;
      min-height: 0;
      overflow: auto;
      border: 1px solid #cfd8c7;
      border-radius: 8px;
      padding: 14px;
      background: #101714;
      color: #d9f4dc;
      font: 13px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      white-space: pre-wrap;
      word-break: break-word;
    }

    .notice {
      margin-top: 12px;
      min-height: 20px;
      color: var(--muted);
      font-size: 13px;
    }

    .notice.error { color: var(--danger); }
    .notice.ok { color: var(--ok); }

    /* ---- 自动化状态栏 ---- */
    .status-bar {
      grid-column: 1 / -1;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px 16px;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 8px 16px;
      align-items: center;
    }

    .status-bar .status-item {
      font-size: 13px;
      color: var(--muted);
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .status-bar .status-icon { font-size: 16px; }

    .status-bar .status-actions {
      display: flex;
      gap: 6px;
      justify-content: flex-end;
    }

    .status-bar .status-actions button {
      height: 30px;
      padding: 0 12px;
      font-size: 12px;
      font-weight: 600;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      cursor: pointer;
      transition: border-color 120ms ease, background 120ms ease;
    }

    .status-bar .status-actions button:hover:not(:disabled) {
      border-color: var(--accent);
    }

    .status-bar .status-actions button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
    }

    .status-bar .status-actions button.danger {
      border-color: rgba(180, 35, 24, 0.3);
      color: var(--danger);
    }

    .status-bar .status-actions button.danger:hover:not(:disabled) {
      background: #fff3f0;
    }

    .status-bar .ok { color: var(--ok); }
    .status-bar .warn { color: #b54708; }
    .status-bar .err { color: var(--danger); }

    @media (max-width: 840px) {
      main {
        grid-template-columns: 1fr;
      }

      .grid {
        grid-template-columns: 1fr;
      }

      .status-bar .status-actions {
        justify-content: flex-start;
        margin-top: 4px;
      }
    }
  </style>
</head>
<body data-version="3">
  <div class="app">
    <header>
      <div class="bar">
        <h1>HDU 图书馆即时预约</h1>
        <div id="status" class="status">就绪</div>
      </div>
    </header>
    <main>
      <section class="form">
        <div class="status-bar" id="statusBar">
          <div class="status-item">
            <span class="status-icon">🔗</span>
            <span id="cookieLabel">Cookie: 检测中…</span>
          </div>
          <div class="status-item">
            <span class="status-icon" id="schedIcon">⏰</span>
            <span id="schedLabel">自动预约: 未启用</span>
          </div>
          <div class="status-item" id="nextRunRow" hidden>
            <span>📅 下次执行:</span>
            <span id="nextRunTime">-</span>
          </div>
          <div class="status-item" id="lastResultRow" hidden>
            <span>📋 上次结果:</span>
            <span id="lastResult">-</span>
          </div>
          <div class="status-actions">
            <button id="loginBtn">登录图书馆</button>
            <button id="cookieBtn">获取cookie</button>
            <button id="schedStartBtn" class="primary">启用自动</button>
            <button id="schedStopBtn" class="danger" disabled>禁用自动</button>
          </div>
        </div>
        <h2 style="margin-top: 16px;">预约参数</h2>
        <div class="grid">
          <label>楼层
            <select id="floorId">
              <option value="1559">杭韵数阁（六楼）</option>
              <option value="1558">宋韵云图（四楼）</option>
              <option value="1557">格物E堂（二楼东）</option>
              <option value="1554">数智渊阁（二楼）</option>
              <option value="1543">芯灵驿站（十二楼）</option>
              <option value="1524">比特庭园（二楼西）</option>
            </select>
          </label>
          <label>座位号
            <input id="seatNum" type="number" min="1" step="1">
          </label>
          <label>开始时间
            <select id="startHour"></select>
          </label>
          <label>时长
            <select id="durationHours">
              <option value="1">1 小时</option>
              <option value="2">2 小时</option>
              <option value="3">3 小时</option>
              <option value="4">4 小时</option>
              <option value="5">5 小时</option>
              <option value="6">6 小时</option>
              <option value="7">7 小时</option>
              <option value="8">8 小时</option>
              <option value="9">9 小时</option>
              <option value="10">10 小时</option>
              <option value="11">11 小时</option>
              <option value="12">12 小时</option>
              <option value="13">13 小时</option>
              <option value="14">14 小时</option>
            </select>
          </label>
          <label>执行时间
            <input id="executeAt" type="text" inputmode="numeric" autocomplete="off" placeholder="20:00:00">
          </label>
          <label>重试次数
            <input id="maxTrials" type="number" min="1" max="20" step="1">
          </label>
          <label>重试间隔秒
            <input id="retryDelay" type="number" min="0.2" max="10" step="0.1">
          </label>
          <label>预约日期
            <div class="date-row" id="dayGroup">
              <label><input type="radio" name="days" value="0"><span>今天</span></label>
              <label><input type="radio" name="days" value="1"><span>明天</span></label>
              <label><input type="radio" name="days" value="2"><span>后天</span></label>
            </div>
          </label>
          <label class="wide">提交模式
            <div class="dry-row">
              <label><input id="dryRun" type="checkbox"><span>只测试，不提交预约</span></label>
            </div>
          </label>
        </div>
        <div class="actions">
          <button id="loadBtn" type="button">载入配置</button>
          <button id="saveBtn" type="button">保存计划</button>
          <button id="runBtn" class="primary" type="button">开始执行</button>
          <button id="cancelBtn" type="button" disabled>取消任务</button>
          <button id="clearBtn" type="button">清空日志</button>
        </div>
        <div id="notice" class="notice"></div>
      </section>
      <section class="logs">
        <h2>执行日志</h2>
        <pre id="logBox"></pre>
      </section>
    </main>
  </div>

  <script>
    const $ = (id) => document.getElementById(id);
    const fields = {
      floorId: $("floorId"),
      seatNum: $("seatNum"),
      startHour: $("startHour"),
      durationHours: $("durationHours"),
      executeAt: $("executeAt"),
      maxTrials: $("maxTrials"),
      retryDelay: $("retryDelay"),
      dryRun: $("dryRun"),
      logBox: $("logBox"),
      notice: $("notice"),
      status: $("status"),
      loadBtn: $("loadBtn"),
      saveBtn: $("saveBtn"),
      runBtn: $("runBtn"),
      cancelBtn: $("cancelBtn"),
      clearBtn: $("clearBtn"),
    };

    // 初始化开始时间下拉框（00:00 - 23:00）
    (function initStartHourSelect() {
      for (let h = 0; h < 24; h++) {
        const label = String(h).padStart(2, '0') + ':00';
        const option = document.createElement('option');
        option.value = h;
        option.textContent = label;
        fields.startHour.appendChild(option);
      }
    })();

    let pollTimer = null;
    let schedPollTimer = null;
    let currentJobId = "";

    /* ---- 自动化状态栏 ---- */
    const schedFields = {
      cookieLabel: $("cookieLabel"),
      schedIcon: $("schedIcon"),
      schedLabel: $("schedLabel"),
      nextRunRow: $("nextRunRow"),
      nextRunTime: $("nextRunTime"),
      lastResultRow: $("lastResultRow"),
      lastResult: $("lastResult"),
      schedStartBtn: $("schedStartBtn"),
      schedStopBtn: $("schedStopBtn"),
    };

    async function checkCookieStatus() {
      try {
        const data = await requestJson("/api/cookie-status");
        // 显示探测日志
        if (data.logs && data.logs.length) {
          appendLog(data.logs);
        }
        if (data.available) {
          if (data.source === "browser") {
            schedFields.cookieLabel.textContent = "Cookie: ✅ 浏览器自动读取";
            schedFields.cookieLabel.className = "ok";
          } else {
            schedFields.cookieLabel.textContent = "Cookie: ⚠️ 使用配置文件";
            schedFields.cookieLabel.className = "warn";
          }
        } else {
          schedFields.cookieLabel.textContent = "Cookie: ❌ 不可用";
          schedFields.cookieLabel.className = "err";
        }
      } catch (e) {
        schedFields.cookieLabel.textContent = "Cookie: ❓ 检测失败";
        schedFields.cookieLabel.className = "err";
      }
    }

    async function pollScheduler() {
      try {
        const data = await requestJson("/api/scheduler");
        if (data.error) return;
        schedFields.schedStartBtn.disabled = data.enabled;
        schedFields.schedStopBtn.disabled = !data.enabled;
        if (data.enabled) {
          schedFields.schedIcon.textContent = "⏰";
          schedFields.schedLabel.textContent = "自动预约: 🔛 已开启";
          schedFields.schedLabel.className = "ok";
        } else {
          schedFields.schedIcon.textContent = "⏰";
          schedFields.schedLabel.textContent = "自动预约: 未启用";
          schedFields.schedLabel.className = "";
        }
        if (data.next_run) {
          schedFields.nextRunRow.hidden = false;
          const target = new Date(data.next_run_ts * 1000);
          const now = new Date();
          const diff = Math.max(0, Math.floor((target - now) / 1000));
          const h = Math.floor(diff / 3600);
          const m = Math.floor((diff % 3600) / 60);
          schedFields.nextRunTime.textContent =
            data.next_run + (diff > 0 ? ` (约 ${h}h ${m}m 后)` : " (即将执行)");
        } else {
          schedFields.nextRunRow.hidden = true;
        }
        if (data.last_time) {
          schedFields.lastResultRow.hidden = false;
          schedFields.lastResult.textContent = `[${data.last_time}] ${data.last_result}`;
        } else if (data.last_result) {
          schedFields.lastResultRow.hidden = false;
          schedFields.lastResult.textContent = data.last_result;
        } else {
          schedFields.lastResultRow.hidden = true;
        }
      } catch (e) {
        // 调度器不可用时静默
      }
    }

    function startSchedPolling() {
      pollScheduler();
      schedPollTimer = setInterval(pollScheduler, 2000);
    }

    async function startScheduler() {
      try {
        const data = await requestJson("/api/scheduler/start", { method: "POST" });
        setNotice(data.message || "自动预约已启用", "ok");
        pollScheduler();
      } catch (e) {
        setNotice(e.message, "error");
      }
    }

    async function stopScheduler() {
      try {
        const data = await requestJson("/api/scheduler/stop", { method: "POST" });
        setNotice(data.message || "自动预约已禁用", "ok");
        pollScheduler();
      } catch (e) {
        setNotice(e.message, "error");
      }
    }

    schedFields.schedStartBtn.addEventListener("click", startScheduler);
    schedFields.schedStopBtn.addEventListener("click", stopScheduler);

    // "获取cookie" 按钮：手动触发浏览器 cookie 读取，日志显示在右侧
    const cookieBtn = $("cookieBtn");
    cookieBtn.addEventListener("click", async () => {
      cookieBtn.disabled = true;
      cookieBtn.textContent = "检测中…";
      schedFields.cookieLabel.textContent = "Cookie: 检测中…";
      schedFields.cookieLabel.className = "";
      appendLog(["========== 获取 Cookie =========="]);
      try {
        await checkCookieStatus();
      } finally {
        cookieBtn.disabled = false;
        cookieBtn.textContent = "获取cookie";
        appendLog(["========== 检测完成 =========="]);
      }
    });

    // "登录图书馆"：用专用 profile 打开可见窗口，用户登录后关闭窗口即可
    const loginBtn = $("loginBtn");
    loginBtn.addEventListener("click", async () => {
      loginBtn.disabled = true;
      loginBtn.textContent = "打开中…";
      appendLog(["========== 打开登录窗口 =========="]);
      try {
        const data = await requestJson("/api/browser-login", { method: "POST" });
        if (data.logs && data.logs.length) appendLog(data.logs);
        appendLog([
          "请在弹出的浏览器窗口中登录图书馆，登录完成后关闭该窗口，",
          "然后点击「获取cookie」确认读取成功。",
        ]);
        setNotice("登录窗口已打开，登录后关闭窗口再点获取cookie", "ok");
      } catch (e) {
        setNotice(e.message, "error");
        appendLog(["打开登录窗口失败：" + e.message]);
      } finally {
        loginBtn.disabled = false;
        loginBtn.textContent = "登录图书馆";
      }
    });
    /* ---- 自动化状态栏 END ---- */

    function selectedDays() {
      const item = document.querySelector("input[name='days']:checked");
      return item ? Number(item.value) : 1;
    }

    function setDays(days) {
      const item = document.querySelector(`input[name='days'][value="${days}"]`);
      (item || document.querySelector("input[name='days'][value='1']")).checked = true;
    }

    function payload() {
      return {
        floor_id: fields.floorId.value,
        seat_num: fields.seatNum.value.trim(),
        start_hour: fields.startHour.value,
        duration_hours: fields.durationHours.value,
        execute_at: fields.executeAt.value.trim(),
        max_trials: fields.maxTrials.value.trim(),
        retry_delay: fields.retryDelay.value.trim(),
        days: selectedDays(),
        dry_run: fields.dryRun.checked,
      };
    }

    function setStatus(text, cls = "") {
      fields.status.textContent = text;
      fields.status.className = `status ${cls}`;
    }

    function setNotice(text, cls = "") {
      fields.notice.textContent = text;
      fields.notice.className = `notice ${cls}`;
    }

    function appendLog(lines) {
      if (!Array.isArray(lines)) lines = [String(lines)];
      if (!lines.length) return;
      fields.logBox.textContent += lines.join("\n") + "\n";
      fields.logBox.scrollTop = fields.logBox.scrollHeight;
    }

    function setBusy(busy) {
      fields.loadBtn.disabled = busy;
      fields.saveBtn.disabled = busy;
      fields.runBtn.disabled = busy;
      fields.cancelBtn.disabled = !busy;
      setStatus(busy ? "执行中" : "就绪", busy ? "running" : "");
    }

    async function requestJson(url, options = {}) {
      const response = await fetch(url, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "请求失败");
      return data;
    }

    async function loadConfig() {
      try {
        setNotice("");
        const data = await requestJson("/api/config");
        fields.floorId.value = data.floor_id;
        fields.seatNum.value = data.seat_num;
        fields.startHour.value = data.start_hour;
        fields.durationHours.value = data.duration_hours;
        fields.executeAt.value = data.execute_at || "";
        fields.maxTrials.value = data.max_trials;
        fields.retryDelay.value = data.retry_delay;
        fields.dryRun.checked = Boolean(data.dry_run);
        setDays(data.days);
        setNotice("配置已载入", "ok");
      } catch (error) {
        setNotice(error.message, "error");
      }
    }

    async function savePlan() {
      try {
        setNotice("");
        const data = await requestJson("/api/save", {
          method: "POST",
          body: JSON.stringify(payload()),
        });
        setNotice(data.message, "ok");
      } catch (error) {
        setNotice(error.message, "error");
      }
    }

    async function runBooking() {
      if (!fields.dryRun.checked) {
        const prefix = fields.executeAt.value.trim() ? "将创建定时任务，到点后" : "继续后";
        const ok = confirm(`当前不是 dry-run，${prefix}会向预约接口提交请求。`);
        if (!ok) return;
      }
      try {
        setNotice("");
        fields.logBox.textContent = "";
        setBusy(true);
        appendLog("开始执行预约流程...");
        const data = await requestJson("/api/run", {
          method: "POST",
          body: JSON.stringify(payload()),
        });
        currentJobId = data.job_id;
        pollJob(data.job_id);
      } catch (error) {
        setBusy(false);
        setStatus("失败", "error");
        setNotice(error.message, "error");
      }
    }

    async function cancelJob() {
      if (!currentJobId) return;
      try {
        const data = await requestJson("/api/cancel", {
          method: "POST",
          body: JSON.stringify({ job_id: currentJobId }),
        });
        setNotice(data.message, "ok");
      } catch (error) {
        setNotice(error.message, "error");
      }
    }

    async function pollJob(jobId) {
      try {
        const data = await requestJson(`/api/job?id=${encodeURIComponent(jobId)}`);
        fields.logBox.textContent = data.logs.join("\n") + (data.logs.length ? "\n" : "");
        fields.logBox.scrollTop = fields.logBox.scrollHeight;
        if (data.status === "running") {
          pollTimer = setTimeout(() => pollJob(jobId), 350);
          return;
        }
        setBusy(false);
        currentJobId = "";
        if (data.status === "error") {
          setStatus("失败", "error");
          setNotice(data.error || "执行失败", "error");
        } else if (data.status === "cancelled") {
          setStatus("已取消", "");
          setNotice("任务已取消", "ok");
        } else {
          setStatus("完成", "");
          setNotice("执行完成", "ok");
        }
      } catch (error) {
        setBusy(false);
        currentJobId = "";
        setStatus("失败", "error");
        setNotice(error.message, "error");
      }
    }

    fields.loadBtn.addEventListener("click", loadConfig);
    fields.saveBtn.addEventListener("click", savePlan);
    fields.runBtn.addEventListener("click", runBooking);
    fields.cancelBtn.addEventListener("click", cancelJob);
    fields.clearBtn.addEventListener("click", () => { fields.logBox.textContent = ""; });
    document.querySelectorAll("input[type='number']").forEach((input) => {
      input.addEventListener("focus", () => input.select());
      input.addEventListener("wheel", (event) => event.preventDefault(), { passive: false });
    });

    setDays(0);
    loadConfig();
    checkCookieStatus();
    startSchedPolling();
  </script>
</body>
</html>
"""


def booking_form_from_config():
    """从默认配置文件读取预约参数（供前端加载）。"""
    config = load_config(DEFAULT_CONFIG)
    booking = config.get("booking") or {}
    plan = parse_plan(str(booking.get("plan") or ""))
    return {
        "floor_id": plan["floor_id"],
        "seat_num": plan["seat_num"],
        "start_hour": plan["start_hour"],
        "duration_hours": plan["duration_hours"],
        "execute_at": normalize_execute_at(booking.get("execute_at")),
        "max_trials": normalize_max_trials(booking.get("max_trials", DEFAULT_MAX_TRIALS)),
        "retry_delay": normalize_retry_delay(booking.get("retry_delay", DEFAULT_RETRY_DELAY)),
        "days": normalize_days(int(booking.get("book_days", DEFAULT_BOOK_DAYS))),
        "dry_run": bool(booking.get("dry_run")),
    }


def normalize_days(days):
    return days if days in (0, 1, 2) else 1


def normalize_max_trials(value):
    try:
        return max(1, min(int(value), 20))
    except Exception:
        return DEFAULT_MAX_TRIALS


def normalize_retry_delay(value):
    try:
        return max(0.2, min(float(value), 10.0))
    except Exception:
        return DEFAULT_RETRY_DELAY


def plan_from_payload(payload):
    # room_type 固定为 1（自习室），UI 中已移除该字段
    required = ("floor_id", "seat_num", "start_hour", "duration_hours")
    values = {}
    for key in required:
        value = str(payload.get(key, "")).strip()
        if not value:
            raise ValueError(f"{key} 不能为空")
        values[key] = value

    plan_text = (
        f"1:{values['floor_id']}:{values['seat_num']}:"
        f"{values['start_hour']}:{values['duration_hours']}"
    )
    plan = parse_plan(plan_text)
    if not 0 <= plan["start_hour"] <= 23:
        raise ValueError("开始小时必须在 0 到 23 之间")
    if plan["duration_hours"] <= 0:
        raise ValueError("时长必须大于 0")
    return plan_text


def write_booking_values(path, plan_text, days, dry_run, execute_at, max_trials, retry_delay):
    values = {
        "plan": plan_text,
        "execute_at": f"'{normalize_execute_at(execute_at)}'",
        "max_trials": str(normalize_max_trials(max_trials)),
        "retry_delay": f"{normalize_retry_delay(retry_delay):g}",
        "dry_run": "true" if dry_run else "false",
    }
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    booking_start = find_booking_section(lines)

    if booking_start is None:
        prefix = "" if not text or text.endswith("\n") else "\n"
        block = (
            f"{prefix}booking:\n"
            f"  plan: {values['plan']}\n"
            f"  execute_at: {values['execute_at']}\n"
            f"  max_trials: {values['max_trials']}\n"
            f"  retry_delay: {values['retry_delay']}\n"
            f"  dry_run: {values['dry_run']}\n"
        )
        path.write_text(text + block, encoding="utf-8")
        return

    booking_end = find_section_end(lines, booking_start)
    seen = set()
    for index in range(booking_start + 1, booking_end):
        match = re.match(r"^(\s+)(plan|book_days|execute_at|max_trials|retry_delay|dry_run)\s*:", lines[index])
        if not match:
            continue
        indent, key = match.groups()
        if key == "book_days":
            lines[index] = ""
            continue
        seen.add(key)
        newline = "\n" if lines[index].endswith("\n") else ""
        lines[index] = f"{indent}{key}: {values[key]}{inline_comment(lines[index])}{newline}"

    missing = [key for key in ("plan", "execute_at", "max_trials", "retry_delay", "dry_run") if key not in seen]
    if missing:
        lines[booking_end:booking_end] = [f"  {key}: {values[key]}\n" for key in missing]
    path.write_text("".join(lines), encoding="utf-8")


def find_booking_section(lines):
    for index, line in enumerate(lines):
        if re.match(r"^booking\s*:", line):
            return index
    return None


def find_section_end(lines, section_start):
    for index in range(section_start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.startswith((" ", "\t", "#")) and ":" in line:
            return index
    return len(lines)


def inline_comment(line):
    body = line.rstrip("\r\n")
    if "#" not in body:
        return ""
    before, after = body.split("#", 1)
    return f" #{after}" if before.strip() else ""


def start_booking_job(payload):
    config = load_config(DEFAULT_CONFIG)
    plan_text = plan_from_payload(payload)
    days = normalize_days(int(payload.get("days", 1)))
    dry_run = bool(payload.get("dry_run"))
    execute_at = normalize_execute_at(payload.get("execute_at"))
    max_trials = normalize_max_trials(payload.get("max_trials", DEFAULT_MAX_TRIALS))
    retry_delay = normalize_retry_delay(payload.get("retry_delay", DEFAULT_RETRY_DELAY))

    # 自动从浏览器加载 cookie（如果配置允许）
    auto_cookie = (config.get("automation") or {}).get("auto_cookie", True)
    preferred_browser = (config.get("automation") or {}).get("preferred_browser", "auto")
    browser_cookie = None
    if auto_cookie and HAS_COOKIE_BROWSER:
        browser_cookie, _ = _safe_find_hdu_cookie(preferred_browser)

    job_id = str(uuid.uuid4())
    cancel_event = threading.Event()
    job = {
        "id": job_id,
        "status": "running",
        "logs": [],
        "error": "",
        "started_at": time.time(),
        "finished_at": None,
        "cancel_event": cancel_event,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job

    def append(message):
        with JOBS_LOCK:
            stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            job["logs"].append(f"[{stamp}] {message}")

    def worker():
        try:
            if browser_cookie:
                append(f"已从浏览器读取到 cookie ({len(browser_cookie)} 字符)")

            run_booking(
                config_path=config_path,
                plan_text=plan_text,
                days=days,
                dry_run_override=dry_run,
                execute_at=execute_at,
                max_trials=max_trials,
                retry_delay=retry_delay,
                logger=append,
                should_cancel=cancel_event.is_set,
                browser_cookie=browser_cookie,
            )
        except Exception as exc:
            with JOBS_LOCK:
                if cancel_event.is_set():
                    job["status"] = "cancelled"
                    job["error"] = ""
                    job["logs"].append("任务已取消")
                else:
                    job["status"] = "error"
                    job["error"] = str(exc)
                    job["logs"].append(f"失败：{exc}")
                job["finished_at"] = time.time()
        else:
            with JOBS_LOCK:
                job["status"] = "done"
                job["logs"].append("执行完成")
                job["finished_at"] = time.time()

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def cancel_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise KeyError("任务不存在")
        if job["status"] != "running":
            return {"message": "任务已经结束"}
        job["cancel_event"].set()
        job["logs"].append("收到取消请求，正在停止任务...")
        return {"message": "已请求取消任务"}


def job_snapshot(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise KeyError("任务不存在")
        return {
            "id": job["id"],
            "status": job["status"],
            "logs": list(job["logs"]),
            "error": job["error"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
        }


class WebHandler(BaseHTTPRequestHandler):
    server_version = "HDULibraryInstant/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            import time as _time
            query = parse_qs(parsed.query)
            if "v" not in query:
                # 强制重定向到带版本号的 URL，彻底绕过浏览器缓存
                self.send_response(302)
                self.send_header("Location", f"/?v=3&t={int(_time.time())}")
                self.end_headers()
                return
            self.send_bytes(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/config":
            # 兼容旧版带 ?path= 参数的请求（忽略参数，始终返回默认配置）
            self.handle_json(booking_form_from_config)
            return
        if parsed.path == "/api/cookie-status":
            self.handle_json(self.check_cookie_status)
            return
        if parsed.path == "/api/scheduler":
            self.handle_json(self.get_scheduler_state)
            return
        if parsed.path == "/api/job":
            query = parse_qs(parsed.query)
            job_id = query.get("id", [""])[0]
            self.handle_json(lambda: job_snapshot(job_id))
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/save":
            self.handle_json(self.save_plan)
            return
        if parsed.path == "/api/run":
            self.handle_json(self.run_booking)
            return
        if parsed.path == "/api/scheduler/start":
            self.handle_json(self.start_scheduler)
            return
        if parsed.path == "/api/scheduler/stop":
            self.handle_json(self.stop_scheduler)
            return
        if parsed.path == "/api/cancel":
            self.handle_json(self.cancel_booking)
            return
        if parsed.path == "/api/browser-login":
            self.handle_json(self.browser_login)
            return
        self.send_error(404)

    def browser_login(self):
        """用专用 profile 打开一个可见浏览器窗口供用户登录图书馆。"""
        try:
            from cdp_cookie import open_login_window
        except ImportError:
            raise RuntimeError("浏览器登录功能不可用（缺少 cdp_cookie 模块）")
        config = load_config(DEFAULT_CONFIG)
        preferred = (config.get("automation") or {}).get("preferred_browser", "auto")
        logs: list[str] = []
        ok = open_login_window(LIBRARY_URL, preferred, logger=logs.append)
        if not ok:
            return {"ok": False, "logs": logs, "error": "未能打开登录窗口"}
        return {"ok": True, "logs": logs}

    def save_plan(self):
        payload = self.read_json()
        path = DEFAULT_CONFIG
        load_config(path)
        plan_text = plan_from_payload(payload)
        write_booking_values(
            path,
            plan_text,
            payload.get("days", 1),
            bool(payload.get("dry_run")),
            payload.get("execute_at"),
            payload.get("max_trials", DEFAULT_MAX_TRIALS),
            payload.get("retry_delay", DEFAULT_RETRY_DELAY),
        )
        return {"message": "计划已保存"}

    def run_booking(self):
        payload = self.read_json()
        job_id = start_booking_job(payload)
        return {"job_id": job_id}

    def cancel_booking(self):
        payload = self.read_json()
        return cancel_job(str(payload.get("job_id") or ""))

    def check_cookie_status(self):
        """检查浏览器 cookie 是否可用，同时返回探测日志。"""
        try:
            config = load_config(DEFAULT_CONFIG)
        except Exception:
            return {"available": False, "source": "error", "detail": "配置读取失败", "logs": []}

        auto_cookie = (config.get("automation") or {}).get("auto_cookie", True)
        preferred = (config.get("automation") or {}).get("preferred_browser", "auto")

        logs: list[str] = []
        if auto_cookie and HAS_COOKIE_BROWSER:
            cookie, logs = _safe_find_hdu_cookie(preferred)
            if cookie:
                # 读取成功即缓存，供调度器/手动运行在读取失败时兜底
                if save_cookie_file(cookie):
                    logs.append("已缓存 cookie 到 browser_cookie.json")
                return {"available": True, "source": "browser", "logs": logs}

        # 回退到配置文件
        auth = config.get("auth") or {}
        if auth.get("cookie"):
            logs.append("浏览器未获取到 cookie，使用配置文件中的 cookie")
            return {"available": True, "source": "config", "logs": logs}
        cookie_file = auth.get("cookie_file")
        if cookie_file and (Path(__file__).with_name(str(cookie_file)).exists()
                            or Path(str(cookie_file)).exists()):
            logs.append("浏览器未获取到 cookie，使用已缓存的 cookie 文件")
            return {"available": True, "source": "config", "logs": logs}

        logs.append("浏览器和配置文件中均未找到 cookie")
        return {"available": False, "source": "none", "logs": logs}

    def get_scheduler_state(self):
        if SCHEDULER is None:
            return {"enabled": False, "status": "unavailable", "error": "调度器未初始化"}
        return SCHEDULER.get_state()

    def start_scheduler(self):
        if SCHEDULER is None:
            raise RuntimeError("调度器不可用")
        SCHEDULER.start()
        return {"message": "自动预约已启用"}

    def stop_scheduler(self):
        if SCHEDULER is None:
            raise RuntimeError("调度器不可用")
        SCHEDULER.stop()
        return {"message": "自动预约已禁用"}

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(raw or "{}")

    def handle_json(self, callback):
        try:
            data = callback()
            self.send_json(data)
        except Exception as exc:
            try:
                self.send_json({"error": str(exc)}, status=400)
            except Exception:
                pass  # 客户端可能已断开，忽略写入错误

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8")

    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_text, *args):
        return


def make_server(host, port):
    return ThreadingHTTPServer((host, port), WebHandler)


def parse_args():
    parser = argparse.ArgumentParser(description="HDU 图书馆即时预约网页控制台")
    parser.add_argument("--host", default=HOST, help="监听地址")
    parser.add_argument("--port", type=int, default=PORT, help="起始端口")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    return parser.parse_args()


def main():
    global SCHEDULER

    args = parse_args()

    # 初始化调度器（如果模块可用）
    if HAS_SCHEDULER:
        config = load_config(DEFAULT_CONFIG)
        auto_daily = (config.get("automation") or {}).get("auto_daily", True)
        SCHEDULER = Scheduler()
        if auto_daily:
            SCHEDULER.start()
            print("自动预约调度器已启动")
    else:
        print("（调度器模块不可用，跳过自动预约）")

    last_error = None
    default_port = args.port
    for port in range(args.port, args.port + 20):
        try:
            server = make_server(args.host, port)
        except OSError as exc:
            last_error = exc
            continue
        url = f"http://{args.host}:{port}"

        # 端口被占用时给出清晰提示
        if port != default_port:
            print(f"端口 {default_port} 已被占用，已自动切换到 {port}")

        # 写入端口文件，方便 start_web.bat 等脚本读取
        try:
            Path(__file__).with_name(".port").write_text(str(port))
        except Exception:
            pass

        print(f"网页控制台已启动：{url}")
        print("按 Ctrl+C 退出")
        if args.open:
            threading.Timer(0.5, webbrowser.open, args=(url,)).start()
        else:
            print(f"浏览器访问 {url} 打开管理界面")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n已退出")
        finally:
            server.server_close()
            if SCHEDULER:
                SCHEDULER.stop()
            # 清理端口文件
            try:
                Path(__file__).with_name(".port").unlink(missing_ok=True)
            except Exception:
                pass
        return
    print(f"错误：端口 {default_port}-{default_port + 19} 全部被占用。")
    print("请关闭占用端口的程序后重试，或手动指定 --port 参数。")
    raise RuntimeError(f"没有可用端口：{last_error}")


if __name__ == "__main__":
    main()
