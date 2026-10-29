/**
 * app.js — 站点导航与首页仪表盘（T9）。
 *
 * 职责：
 *  - 装配导航高亮、主题切换、本地用户标识展示。
 *  - 拉取并渲染仪表盘：画像（GET /api/profile）、统计（GET /api/profile/analytics）、
 *    今日复习计划（GET /api/review/today）。
 *  - 通过 `window.ALA` 暴露共享工具（HTML 转义、时间格式化、Toast、loadDashboard 等），
 *    供同页面的 review.js 复用；其余前端页面（T10~T12）也可选用。
 *
 * 消费根键严格对齐 C6-UI@1 / plan 2.2.6：
 *  - /api/profile:          daily_minutes, reminder_enabled, reminder_time, learning_style, questionnaire_completed
 *  - /api/profile/analytics: streak_days, attempts, correct_rate, kps_in_review, review_due_today, by_day[],
 *                            mastery_by_chapter[]
 *  - /api/review/today:      items[].{kp_id,chapter_id,title,due_at,overdue_days,est_minutes,priority_score,mastery,level},
 *                            total_minutes, budget_minutes, truncated, dropped_count, note
 *
 * null 处理：`truncated==false` 不以裁剪语气提示；`note` 为空串时隐藏提示条；
 *           `overdue_days==0` 显示「今日到期」。
 */
(function (global) {
  "use strict";

  var API = global.API;
  if (!API) {
    console.error("[app.js] api.js 未加载，仪表盘功能不可用");
    return;
  }

  var STYLE_LABELS = {
    visual: "视觉型",
    auditory: "听觉型",
    reading: "读写型",
    kinesthetic: "动觉型",
    mixed: "混合型",
  };

  var CHAPTER_LABELS = {
    cells: "细胞的结构与功能",
    genetics: "遗传的分子基础",
  };

  var THEME_KEY = "ala_theme";

  // ---------------------------------------------------------------------------
  // 通用工具
  // ---------------------------------------------------------------------------

  /** HTML 转义，防止渲染用户/后端文本时注入标记。 */
  function esc(value) {
    if (value === undefined || value === null) {
      return "";
    }
    return String(value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  /** 安全数值转换。 */
  function toNumber(value, fallback) {
    var num = Number(value);
    if (!isFinite(num)) {
      return fallback === undefined ? 0 : fallback;
    }
    return num;
  }

  /** 比率（0..1）转百分比文本。 */
  function formatPercent(rate) {
    return Math.round(toNumber(rate) * 100) + "%";
  }

  /** 补齐两位。 */
  function pad2(value) {
    return String(value).padStart(2, "0");
  }

  /** ISO-8601 时间 → 本地「MM-DD HH:MM」。 */
  function formatDateTime(iso) {
    if (!iso) {
      return "—";
    }
    var date = new Date(iso);
    if (isNaN(date.getTime())) {
      return String(iso);
    }
    return (
      pad2(date.getMonth() + 1) + "-" + pad2(date.getDate()) + " " +
      pad2(date.getHours()) + ":" + pad2(date.getMinutes())
    );
  }

  /** "YYYY-MM-DD"（或任意含日期片段）→ "MM-DD"。 */
  function formatShortDate(value) {
    if (!value) {
      return "";
    }
    var match = String(value).match(/(\d{4})-(\d{2})-(\d{2})/);
    return match ? match[2] + "-" + match[3] : String(value);
  }

  /** 逾期文案：0 天显示「今日到期」，否则「逾期 N 天」。 */
  function overdueLabel(days) {
    var value = Math.round(toNumber(days));
    return value <= 0 ? "今日到期" : "逾期 " + value + " 天";
  }

  /** 章节 id → 可读名称。 */
  function chapterLabel(chapterId) {
    if (!chapterId) {
      return "未分章";
    }
    return CHAPTER_LABELS[chapterId] || String(chapterId);
  }

  /** 顶部 Toast 提示。 */
  function toast(message, variant) {
    var host = document.getElementById("toast-host");
    if (!host) {
      return;
    }
    var el = document.createElement("div");
    el.className = "toast toast-" + (variant || "info");
    el.textContent = message;
    host.appendChild(el);
    global.setTimeout(function () {
      el.style.opacity = "0";
      el.style.transform = "translateY(8px)";
      global.setTimeout(function () {
        el.remove();
      }, 240);
    }, 3600);
  }

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) {
      el.textContent = text;
    }
  }

  // ---------------------------------------------------------------------------
  // 导航 / 主题 / 用户标识
  // ---------------------------------------------------------------------------

  /** 依据当前路径判断所处页面。 */
  function currentPage() {
    var path = global.location.pathname || "/";
    if (path === "/" || /index\.html$/.test(path)) {
      return "home";
    }
    if (path.indexOf("knowledge.html") !== -1) {
      return "knowledge";
    }
    if (path.indexOf("chat.html") !== -1) {
      return "chat";
    }
    if (path.indexOf("profile.html") !== -1) {
      return "profile";
    }
    return "home";
  }

  /** 高亮当前页导航项；其它页面亦可调用以复用导航组件。 */
  function mountNav(root) {
    var scope = root || document;
    var page = currentPage();
    var links = scope.querySelectorAll(".main-nav a[data-page]");
    Array.prototype.forEach.call(links, function (link) {
      var active = link.getAttribute("data-page") === page;
      link.classList.toggle("is-active", active);
      if (active) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    });
  }

  /** 绑定明暗主题切换按钮。 */
  function mountTheme() {
    var button = document.getElementById("theme-toggle");
    if (!button) {
      return;
    }
    function apply(theme) {
      document.documentElement.setAttribute("data-theme", theme);
      button.textContent = theme === "dark" ? "☀️" : "🌙";
    }
    apply(document.documentElement.getAttribute("data-theme") || "light");
    button.addEventListener("click", function () {
      var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      apply(next);
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (err) {
        /* ignore */
      }
    });
  }

  /** 展示本地用户标识前缀。 */
  function mountUserId() {
    var short = "";
    try {
      var id = API.userId();
      short = id.slice(0, 8);
    } catch (err) {
      short = "本地用户";
    }
    setText("user-id-short", short);
    setText("footer-user", short);
  }

  // ---------------------------------------------------------------------------
  // 仪表盘数据与渲染
  // ---------------------------------------------------------------------------

  var state = {
    profile: null,
    analytics: null,
    today: null,
  };

  function safeGet(path, params) {
    return API.get(path, params).then(
      function (data) {
        return { ok: true, data: data };
      },
      function (error) {
        return { ok: false, error: error };
      }
    );
  }

  function renderBanners() {
    var firstRun = document.getElementById("first-run-banner");
    if (firstRun) {
      var needQuestionnaire =
        state.profile && state.profile.questionnaire_completed === false;
      firstRun.hidden = !needQuestionnaire;
    }

    var backend = document.getElementById("backend-banner");
    if (backend) {
      // 画像与今日计划均取不到，判定后端不可用。
      backend.hidden = !(state.profile === null && state.today === null);
    }
  }

  function renderProfileCard() {
    var label = state.profile && state.profile.learning_style
      ? STYLE_LABELS[state.profile.learning_style] || state.profile.learning_style
      : "混合型（未测评）";
    setText("style-label", label);
  }

  var STAT_KEYS = ["streak_days", "attempts", "correct_rate", "kps_in_review", "review_due_today"];

  function renderStats() {
    var analytics = state.analytics;
    var values = {
      streak_days: analytics ? toNumber(analytics.streak_days) + " 天" : "–",
      attempts: analytics ? String(toNumber(analytics.attempts)) : "–",
      correct_rate: analytics ? formatPercent(analytics.correct_rate) : "–",
      kps_in_review: analytics ? String(toNumber(analytics.kps_in_review)) : "–",
      review_due_today: analytics ? String(toNumber(analytics.review_due_today)) : "–",
    };
    STAT_KEYS.forEach(function (key) {
      var el = document.querySelector('[data-stat="' + key + '"]');
      if (el) {
        el.textContent = values[key];
      }
    });
  }

  function renderTodaySummary() {
    var plan = state.today;
    var items = plan && Array.isArray(plan.items) ? plan.items : [];

    setText("today-count-badge", items.length + " 项");
    setText("today-total", items.length ? toNumber(plan.total_minutes) : 0);
    setText("today-budget", plan ? toNumber(plan.budget_minutes, 30) : 30);

    var budget = Math.max(1, toNumber(plan && plan.budget_minutes, 30));
    var total = toNumber(plan && plan.total_minutes);
    var percent = Math.min(100, Math.round((total / budget) * 100));
    var bar = document.getElementById("today-progress");
    if (bar) {
      bar.style.width = percent + "%";
    }

    var noteEl = document.getElementById("today-note");
    if (noteEl) {
      var note = plan && typeof plan.note === "string" ? plan.note.trim() : "";
      if (note) {
        noteEl.hidden = false;
        noteEl.textContent = note;
        // truncated==true 时以警示样式呈现裁剪说明；否则作为普通预算提示。
        noteEl.classList.toggle("banner-warn", !!plan.truncated);
        noteEl.classList.toggle("banner-info", !plan.truncated);
      } else {
        noteEl.hidden = true;
      }
    }

    var emptyEl = document.getElementById("today-empty");
    if (emptyEl) {
      if (!plan) {
        emptyEl.hidden = false;
        emptyEl.textContent = "复习数据暂不可用（后端可能尚未就绪）。";
      } else if (items.length === 0) {
        emptyEl.hidden = false;
        emptyEl.textContent = "今日暂无到期待复习，保持节奏，明天见 🌱";
      } else {
        emptyEl.hidden = true;
      }
    }
  }

  function renderWeekChart() {
    var host = document.getElementById("week-chart");
    if (!host) {
      return;
    }
    var days = state.analytics && Array.isArray(state.analytics.by_day) ? state.analytics.by_day : [];
    if (!days.length) {
      host.innerHTML = '<p class="muted">暂无学习记录</p>';
      return;
    }
    var maxAttempts = 1;
    days.forEach(function (day) {
      maxAttempts = Math.max(maxAttempts, toNumber(day.attempts));
    });
    host.innerHTML = days
      .map(function (day) {
        var attempts = toNumber(day.attempts);
        var minutes = toNumber(day.minutes);
        var height = attempts ? Math.max(6, Math.round((attempts / maxAttempts) * 100)) : 0;
        var title = day.date + "：作答 " + attempts + " 次 / " + minutes + " 分钟";
        return (
          '<div class="bar-col" title="' + esc(title) + '">' +
          '<div class="bar-track"><div class="bar-fill" style="height:' + height + '%"></div></div>' +
          '<span class="bar-label">' + esc(formatShortDate(day.date)) + "</span>" +
          "</div>"
        );
      })
      .join("");
  }

  function renderMastery() {
    var host = document.getElementById("mastery-list");
    if (!host) {
      return;
    }
    var rows =
      state.analytics && Array.isArray(state.analytics.mastery_by_chapter)
        ? state.analytics.mastery_by_chapter
        : [];
    if (!rows.length) {
      host.innerHTML = '<p class="muted">暂无掌握度数据（先加入复习并完成作答）。</p>';
      return;
    }
    host.innerHTML = rows
      .map(function (row) {
        var percent = Math.round(toNumber(row.avg_mastery) * 100);
        return (
          '<div class="mastery-row">' +
          '<span class="mastery-name">' + esc(chapterLabel(row.chapter_id)) + "</span>" +
          '<div class="progress"><div class="progress-bar" style="width:' + percent + '%"></div></div>' +
          '<span class="mastery-pct">' + percent + "%</span>" +
          "</div>"
        );
      })
      .join("");
  }

  function renderDashboard() {
    renderBanners();
    renderProfileCard();
    renderStats();
    renderTodaySummary();
    renderWeekChart();
    renderMastery();
  }

  var dashboardPromise = null;

  /**
   * 加载并渲染仪表盘数据（带缓存；force=true 时强制刷新）。
   * @param {boolean} [force]
   * @returns {Promise<{profile:object|null,analytics:object|null,today:object|null}>}
   */
  function loadDashboard(force) {
    if (dashboardPromise && !force) {
      return dashboardPromise;
    }
    dashboardPromise = Promise.all([
      safeGet("/profile"),
      safeGet("/profile/analytics"),
      safeGet("/review/today"),
    ]).then(function (results) {
      state.profile = results[0].ok ? results[0].data : null;
      state.analytics = results[1].ok ? results[1].data : null;
      state.today = results[2].ok ? results[2].data : null;
      renderDashboard();
      return state;
    });
    return dashboardPromise;
  }

  // ---------------------------------------------------------------------------
  // 对外共享命名空间
  // ---------------------------------------------------------------------------

  global.ALA = {
    // 工具
    esc: esc,
    toNumber: toNumber,
    formatPercent: formatPercent,
    formatDateTime: formatDateTime,
    formatShortDate: formatShortDate,
    overdueLabel: overdueLabel,
    chapterLabel: chapterLabel,
    toast: toast,
    // 导航
    currentPage: currentPage,
    mountNav: mountNav,
    // 数据
    state: state,
    loadDashboard: loadDashboard,
  };

  function init() {
    mountNav(document);
    mountTheme();
    mountUserId();
    loadDashboard(false);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})(window);