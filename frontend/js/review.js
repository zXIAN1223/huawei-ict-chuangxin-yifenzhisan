/**
 * review.js — 复习中心与复习提醒（T9）。
 *
 * 职责：
 *  - 复习中心：渲染今日待复习（GET /api/review/today）与全部复习项（GET /api/review/items?status=all），
 *    支持「完成复习」（POST /api/review/items/{kp_id}/complete {grade}）、「删除」（软删除）、「恢复」。
 *  - 每日预算：PUT /api/review/budget。
 *  - 复习提醒：GET/PUT /api/review/reminder；到达 reminder_time 且今日有待复习项时，
 *    触发浏览器 Notification（需授权）并播放 Web Audio 提示音。
 *
 * 契约对齐 C6-UI@1 / plan 2.2.6、C2-REVIEW@1 / plan 2.2.2。
 * 依赖同任务的 app.js 提供的 `window.ALA`（工具与共享仪表盘数据）。
 */
(function (global) {
  "use strict";

  var API = global.API;
  var ALA = global.ALA;
  if (!API || !ALA) {
    console.error("[review.js] 需要同时加载 api.js 与 app.js");
    return;
  }

  var esc = ALA.esc;
  var toNumber = ALA.toNumber;

  /** grade 选项（0..5，语义与记忆曲线 apply_grade 对齐）。 */
  var GRADES = [
    { value: 0, label: "完全忘记" },
    { value: 1, label: "很模糊" },
    { value: 2, label: "模糊" },
    { value: 3, label: "勉强记起" },
    { value: 4, label: "良好" },
    { value: 5, label: "完美" },
  ];

  var LAST_FIRED_KEY = "ala_reminder_last_fired";
  var REMINDER_CHECK_MS = 30000;

  var currentTab = "today";
  var todayPlan = null;
  var reminder = { enabled: true, time: "20:00" };
  var reminderLoaded = false;
  var reminderTimer = null;
  var audioContext = null;

  function $(id) {
    return document.getElementById(id);
  }

  // ---------------------------------------------------------------------------
  // 渲染：复习项
  // ---------------------------------------------------------------------------

  function gradeButtons() {
    return GRADES.map(function (grade) {
      return (
        '<button type="button" class="grade-btn" data-grade="' + grade.value + '" ' +
        'aria-label="评分 ' + grade.value + ' ' + esc(grade.label) + '">' +
        '<span class="grade-num">' + grade.value + "</span>" +
        '<span class="grade-text">' + esc(grade.label) + "</span>" +
        "</button>"
      );
    }).join("");
  }

  function itemActions(status) {
    if (status === "removed") {
      return '<button type="button" class="btn btn-sm btn-soft" data-action="restore">恢复复习</button>';
    }
    return (
      '<button type="button" class="btn btn-sm btn-primary" data-action="complete">完成复习</button>' +
      '<button type="button" class="btn btn-sm btn-ghost" data-action="delete">删除</button>'
    );
  }

  function renderItem(item, status) {
    var kpId = item.kp_id;
    var masteryPct = Math.round(toNumber(item.mastery) * 100);
    var chips = [];
    // overdue_days 仅由今日计划项（DailyPlanItem）提供；复习项视图无此字段。
    if (typeof item.overdue_days === "number") {
      chips.push('<span class="chip chip-due">' + esc(ALA.overdueLabel(item.overdue_days)) + "</span>");
    }
    chips.push('<span class="chip">⏱ ' + toNumber(item.est_minutes) + " 分钟</span>");
    chips.push('<span class="chip">' + esc(ALA.chapterLabel(item.chapter_id)) + "</span>");
    if (typeof item.level === "number") {
      chips.push('<span class="chip">Lv ' + toNumber(item.level) + "</span>");
    }
    if (typeof item.priority_score === "number") {
      chips.push('<span class="chip">优先级 ' + toNumber(item.priority_score).toFixed(2) + "</span>");
    }
    if (item.due_at) {
      chips.push('<span class="chip">到期 ' + esc(ALA.formatDateTime(item.due_at)) + "</span>");
    }
    if (status === "removed") {
      chips.push('<span class="chip chip-removed">已删除</span>');
    }

    var gradeStrip =
      status === "removed"
        ? ""
        : '<div class="grade-strip" hidden><span class="grade-hint">本次掌握程度：</span>' +
          gradeButtons() +
          "</div>";

    return (
      '<article class="review-item" data-kp-id="' + esc(kpId) + '" data-status="' + esc(status) + '">' +
      '<div class="ri-head">' +
      '<h3 class="ri-title">' + esc(item.title || kpId) + "</h3>" +
      '<span class="badge badge-soft" title="掌握度">' + masteryPct + "%</span>" +
      "</div>" +
      '<div class="ri-meta">' + chips.join("") + "</div>" +
      '<div class="ri-actions">' + itemActions(status) + "</div>" +
      gradeStrip +
      "</article>"
    );
  }

  function renderToday() {
    var list = $("review-list");
    if (!list) {
      return;
    }
    if (!todayPlan) {
      list.innerHTML = '<p class="muted">复习数据暂不可用（后端可能尚未就绪）。</p>';
      return;
    }
    var items = Array.isArray(todayPlan.items) ? todayPlan.items : [];
    if (!items.length) {
      list.innerHTML =
        '<p class="muted">今日暂无到期待复习项。可在「知识点」页把内容加入复习计划。</p>';
      return;
    }
    list.innerHTML = items
      .map(function (item) {
        return renderItem(item, "active");
      })
      .join("");
  }

  async function renderAll() {
    var list = $("review-list");
    if (!list) {
      return;
    }
    list.innerHTML = '<p class="muted">加载中…</p>';
    try {
      var items = await API.get("/review/items", { status: "all" });
      if (!items || !items.length) {
        list.innerHTML = '<p class="muted">暂无复习项。可在「知识点」页把内容加入复习计划。</p>';
        return;
      }
      list.innerHTML = items
        .map(function (item) {
          return renderItem(item, item.status || "active");
        })
        .join("");
    } catch (error) {
      list.innerHTML =
        '<p class="muted">加载复习项失败：' + esc(error && error.message ? error.message : "未知错误") + "</p>";
    }
  }

  function renderCurrentTab() {
    if (currentTab === "today") {
      renderToday();
    } else {
      renderAll();
    }
  }

  function syncBudgetInput() {
    var input = $("budget-input");
    if (!input) {
      return;
    }
    var value = null;
    if (todayPlan && typeof todayPlan.budget_minutes === "number") {
      value = todayPlan.budget_minutes;
    } else if (ALA.state.profile && typeof ALA.state.profile.daily_minutes === "number") {
      value = ALA.state.profile.daily_minutes;
    }
    if (value !== null && document.activeElement !== input) {
      input.value = value;
    }
  }

  // ---------------------------------------------------------------------------
  // 交互：完成 / 删除 / 恢复 / 刷新 / 预算
  // ---------------------------------------------------------------------------

  async function refreshAll() {
    var state = await ALA.loadDashboard(true);
    todayPlan = state.today;
    syncBudgetInput();
    renderCurrentTab();
  }

  async function completeReview(kpId, grade, button) {
    if (button) {
      button.disabled = true;
    }
    try {
      var result = await API.post(
        "/review/items/" + encodeURIComponent(kpId) + "/complete",
        { grade: grade }
      );
      var nextDue = result && result.next_due_at ? ALA.formatDateTime(result.next_due_at) : "稍后";
      ALA.toast("已完成复习，下次到期：" + nextDue, "success");
      await refreshAll();
    } catch (error) {
      ALA.toast((error && error.message) || "完成复习失败", "error");
    } finally {
      if (button) {
        button.disabled = false;
      }
    }
  }

  async function deleteReviewItem(kpId) {
    if (!global.confirm("确定从复习计划中删除该知识点吗？历史作答与掌握度记录会保留。")) {
      return;
    }
    try {
      await API.del("/review/items/" + encodeURIComponent(kpId));
      ALA.toast("已删除复习项", "success");
      await refreshAll();
    } catch (error) {
      ALA.toast((error && error.message) || "删除失败", "error");
    }
  }

  async function restoreReviewItem(kpId) {
    try {
      await API.post("/review/items/" + encodeURIComponent(kpId) + "/restore");
      ALA.toast("已恢复复习项", "success");
      await refreshAll();
    } catch (error) {
      ALA.toast((error && error.message) || "恢复失败", "error");
    }
  }

  function bindListEvents() {
    var list = $("review-list");
    if (!list) {
      return;
    }
    list.addEventListener("click", function (event) {
      var button = event.target.closest("button");
      if (!button) {
        return;
      }
      var item = button.closest(".review-item");
      if (!item) {
        return;
      }
      var kpId = item.getAttribute("data-kp-id");
      var action = button.getAttribute("data-action");
      var grade = button.getAttribute("data-grade");

      if (action === "complete") {
        var strip = item.querySelector(".grade-strip");
        if (strip) {
          strip.hidden = !strip.hidden;
        }
      } else if (action === "delete") {
        deleteReviewItem(kpId);
      } else if (action === "restore") {
        restoreReviewItem(kpId);
      } else if (grade !== null) {
        completeReview(kpId, Number(grade), button);
      }
    });
  }

  function bindTabs() {
    var tabs = document.querySelectorAll(".tabs .tab");
    Array.prototype.forEach.call(tabs, function (tab) {
      tab.addEventListener("click", function () {
        currentTab = tab.getAttribute("data-tab") || "today";
        Array.prototype.forEach.call(tabs, function (other) {
          var active = other === tab;
          other.classList.toggle("is-active", active);
          other.setAttribute("aria-selected", active ? "true" : "false");
        });
        renderCurrentTab();
      });
    });
  }

  function bindBudget() {
    var save = $("budget-save");
    if (!save) {
      return;
    }
    save.addEventListener("click", async function () {
      var input = $("budget-input");
      var value = Number(input ? input.value : NaN);
      if (!isFinite(value) || value < 0) {
        ALA.toast("请输入不小于 0 的分钟数", "error");
        return;
      }
      try {
        var result = await API.put("/review/budget", { budget_minutes: Math.round(value) });
        ALA.toast("每日预算已更新为 " + result.budget_minutes + " 分钟", "success");
        await refreshAll();
      } catch (error) {
        ALA.toast((error && error.message) || "保存预算失败", "error");
      }
    });
  }

  // ---------------------------------------------------------------------------
  // 提醒：权限、提示音、定时检查
  // ---------------------------------------------------------------------------

  function supportsNotification() {
    return typeof global.Notification !== "undefined";
  }

  function setNotifyStatus(text) {
    var el = $("notify-status");
    if (el) {
      el.textContent = text;
    }
  }

  function updateNotifyStatus(permission) {
    var perm = permission;
    if (!perm && supportsNotification()) {
      perm = global.Notification.permission;
    }
    if (!supportsNotification()) {
      setNotifyStatus("通知权限：当前浏览器不支持桌面通知");
      return;
    }
    var labels = {
      granted: "已授权",
      denied: "已拒绝（可在浏览器设置中重新开启）",
      default: "未授权（点击「授权桌面通知」开启）",
    };
    setNotifyStatus("通知权限：" + (labels[perm] || perm));
  }

  function playChime() {
    var Ctx = global.AudioContext || global.webkitAudioContext;
    if (!Ctx) {
      return;
    }
    try {
      if (!audioContext) {
        audioContext = new Ctx();
      }
      if (audioContext.state === "suspended") {
        audioContext.resume();
      }
      var now = audioContext.currentTime;
      var tones = [
        { freq: 880.0, offset: 0 },
        { freq: 1174.66, offset: 0.18 },
      ];
      tones.forEach(function (tone) {
        var osc = audioContext.createOscillator();
        var gain = audioContext.createGain();
        osc.type = "sine";
        osc.frequency.value = tone.freq;
        gain.gain.setValueAtTime(0.0001, now + tone.offset);
        gain.gain.exponentialRampToValueAtTime(0.25, now + tone.offset + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + tone.offset + 0.42);
        osc.connect(gain);
        gain.connect(audioContext.destination);
        osc.start(now + tone.offset);
        osc.stop(now + tone.offset + 0.46);
      });
    } catch (error) {
      /* 静默降级：提示音非关键路径 */
    }
  }

  function fireDesktopNotification(count) {
    if (!supportsNotification() || global.Notification.permission !== "granted") {
      return false;
    }
    try {
      var body =
        "今日有 " + count + " 个知识点待复习" +
        (todayPlan && todayPlan.total_minutes ? "，预计 " + todayPlan.total_minutes + " 分钟" : "") +
        "。";
      new global.Notification("该复习啦 📚", { body: body, tag: "ala-review-reminder" });
      return true;
    } catch (error) {
      return false;
    }
  }

  function dateKey(date) {
    return (
      date.getFullYear() + "-" + String(date.getMonth() + 1).padStart(2, "0") +
      "-" + String(date.getDate()).padStart(2, "0")
    );
  }

  function readLastFired() {
    try {
      return global.localStorage.getItem(LAST_FIRED_KEY);
    } catch (error) {
      return null;
    }
  }

  function writeLastFired(key) {
    try {
      global.localStorage.setItem(LAST_FIRED_KEY, key);
    } catch (error) {
      /* ignore */
    }
  }

  /**
   * 检查是否到达提醒条件：启用 + 已过提醒时间 + 今日有到期项 + 当日未触发过。
   * 命中则触发一次桌面通知与提示音。
   */
  function checkReminder() {
    if (!reminderLoaded || !reminder.enabled) {
      return;
    }
    var items = todayPlan && Array.isArray(todayPlan.items) ? todayPlan.items : [];
    if (!items.length) {
      return;
    }
    var parts = String(reminder.time || "20:00").split(":");
    var hours = parseInt(parts[0], 10);
    var minutes = parseInt(parts[1], 10);
    if (!isFinite(hours) || !isFinite(minutes)) {
      return;
    }
    var now = new Date();
    var target = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hours, minutes, 0, 0);
    if (now.getTime() < target.getTime()) {
      return;
    }
    var key = dateKey(now);
    if (readLastFired() === key) {
      return;
    }
    writeLastFired(key);

    var shown = fireDesktopNotification(items.length);
    playChime();
    var summary = "今日有 " + items.length + " 个知识点待复习。";
    ALA.toast("复习提醒：" + summary, "info");
    if (!shown) {
      setNotifyStatus("提醒已触发，但桌面通知未授权（已播放提示音）。");
    }
  }

  function startReminderScheduler() {
    if (reminderTimer) {
      return;
    }
    checkReminder();
    reminderTimer = global.setInterval(checkReminder, REMINDER_CHECK_MS);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) {
        checkReminder();
      }
    });
  }

  async function loadReminder() {
    try {
      var result = await API.get("/review/reminder");
      reminder = {
        enabled: !!result.enabled,
        time: normalizeTime(result.time),
      };
      reminderLoaded = true;
      applyReminderInputs();
    } catch (error) {
      setNotifyStatus("提醒设置读取失败：" + ((error && error.message) || "未知错误"));
    }
  }

  function normalizeTime(value) {
    return /^([01]\d|2[0-3]):[0-5]\d$/.test(String(value || "")) ? String(value) : "20:00";
  }

  function applyReminderInputs() {
    var enabledEl = $("reminder-enabled");
    var timeEl = $("reminder-time");
    if (enabledEl) {
      enabledEl.checked = reminder.enabled;
    }
    if (timeEl) {
      timeEl.value = reminder.time;
    }
  }

  function bindReminder() {
    var save = $("reminder-save");
    if (save) {
      save.addEventListener("click", async function () {
        var enabledEl = $("reminder-enabled");
        var timeEl = $("reminder-time");
        var payload = {
          enabled: !!(enabledEl && enabledEl.checked),
          time: normalizeTime(timeEl ? timeEl.value : reminder.time),
        };
        try {
          var result = await API.put("/review/reminder", payload);
          reminder = { enabled: !!result.enabled, time: normalizeTime(result.time) };
          reminderLoaded = true;
          applyReminderInputs();
          ALA.toast("提醒设置已保存", "success");
        } catch (error) {
          ALA.toast((error && error.message) || "保存提醒设置失败", "error");
        }
      });
    }

    var notifyButton = $("notify-enable");
    if (notifyButton) {
      notifyButton.addEventListener("click", async function () {
        if (!supportsNotification()) {
          setNotifyStatus("通知权限：当前浏览器不支持桌面通知");
          return;
        }
        try {
          var permission = await global.Notification.requestPermission();
          updateNotifyStatus(permission);
          if (permission === "granted") {
            ALA.toast("桌面通知已开启", "success");
          }
        } catch (error) {
          setNotifyStatus("通知授权失败：" + ((error && error.message) || "未知错误"));
        }
      });
    }

    var soundButton = $("sound-test");
    if (soundButton) {
      soundButton.addEventListener("click", function () {
        playChime();
        ALA.toast("已播放提示音", "info");
      });
    }
  }

  // ---------------------------------------------------------------------------
  // 初始化
  // ---------------------------------------------------------------------------

  function init() {
    bindTabs();
    bindListEvents();
    bindBudget();
    bindReminder();

    var refresh = $("review-refresh");
    if (refresh) {
      refresh.addEventListener("click", function () {
        refreshAll();
      });
    }

    updateNotifyStatus(null);

    // 复用 app.js 的共享仪表盘数据（含今日计划）。
    ALA.loadDashboard().then(function (state) {
      todayPlan = state.today;
      syncBudgetInput();
      renderCurrentTab();
    });

    loadReminder();
    startReminderScheduler();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})(window);