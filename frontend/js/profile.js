/**
 * profile.js — 学习方式测评与进度（T12）。
 *
 * 职责：
 *  - 动态渲染学习习惯问卷（single_choice / multi_choice / scale 三种题型）。
 *  - 提交问卷（POST /api/profile/questionnaire）或单次测评（POST /api/profile/assessment），
 *    展示 AssessmentResult（learning_style / style_scores / recommended_modes / rationale）。
 *  - 展示自适应策略（GET /api/profile/strategy）。
 *  - 展示学习进度统计（GET /api/profile/analytics?days=）。
 *  - 修改偏好设置（PUT /api/profile，部分更新）。
 *  - 首次使用引导：GET /api/profile 的 questionnaire_completed==false 时高亮引导。
 *
 * 依赖：js/api.js（window.API，契约 C6-UI@1）、js/app.js（window.ALA 工具与导航）。
 * 消费根键严格对齐 plan 2.2.5 / 2.2.6：
 *  - /api/profile/questionnaire:  questions[].{qid,text,type,options[].{value,label},dimension,min,max}
 *  - /api/profile/assessment:      learning_style, style_scores, recommended_modes, rationale
 *  - /api/profile/strategy:        recommended_modes, session_length_min, review_intensity_multiplier,
 *                                  daily_minutes_effective, rationale, generated_at
 *  - /api/profile/analytics:       streak_days, total_minutes_estimate, attempts, correct_rate,
 *                                  kps_in_review, review_due_today, by_day[], mastery_by_chapter[]
 *  - PUT /api/profile:             部分字段（nickname/daily_minutes/reminder_enabled/reminder_time）
 */
(function (global) {
  "use strict";

  var API = global.API;
  var ALA = global.ALA || {};

  if (!API) {
    console.error("[profile.js] api.js 未加载，测评页面功能不可用");
    return;
  }

  // ---------------------------------------------------------------------------
  // 常量
  // ---------------------------------------------------------------------------

  var STYLE_LABELS = {
    visual: "视觉型",
    auditory: "听觉型",
    reading: "读写型",
    kinesthetic: "动觉型",
    mixed: "混合型",
  };

  /** 学习风格维度显示顺序（与 VARK 计分维度一致）。 */
  var STYLE_ORDER = ["visual", "auditory", "reading", "kinesthetic"];

  /** 推荐学习方式代码 → 中文标签（未收录的代码回退显示原始代码）。 */
  var MODE_LABELS = {
    flashcard_visual: "视觉卡片",
    mindmap: "思维导图",
    chat_teacher: "老师讲解",
    listen_summary: "听读小结",
    read_summary: "阅读归纳",
    note_taking: "结构化笔记",
    feynman: "费曼讲解",
    recall_practice: "主动回忆练习",
  };

  var DIMENSION_LABELS = {
    style: "学习风格",
    habit: "习惯",
    time: "时间",
    goal: "目标",
  };

  var CHAPTER_LABELS = {
    cells: "细胞的结构与功能",
    genetics: "遗传的分子基础",
  };

  // ---------------------------------------------------------------------------
  // 工具（优先复用 ALA，缺失时本地兜底）
  // ---------------------------------------------------------------------------

  function esc(value) {
    if (typeof ALA.esc === "function") {
      return ALA.esc(value);
    }
    if (value === undefined || value === null) {
      return "";
    }
    return String(value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function toast(message, variant) {
    if (typeof ALA.toast === "function") {
      ALA.toast(message, variant);
      return;
    }
    var host = document.getElementById("toast-host");
    if (!host) {
      return;
    }
    var el = document.createElement("div");
    el.className = "toast toast-" + (variant || "info");
    el.textContent = message;
    host.appendChild(el);
    global.setTimeout(function () {
      el.remove();
    }, 3600);
  }

  function toNumber(value, fallback) {
    var num = Number(value);
    if (!isFinite(num)) {
      return fallback === undefined ? 0 : fallback;
    }
    return num;
  }

  function pad2(value) {
    return String(value).padStart(2, "0");
  }

  function formatShortDate(value) {
    if (!value) {
      return "";
    }
    var match = String(value).match(/(\d{4})-(\d{2})-(\d{2})/);
    return match ? match[2] + "-" + match[3] : String(value);
  }

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) {
      el.textContent = text;
    }
  }

  function styleLabel(key) {
    if (!key) {
      return "混合型（未测评）";
    }
    return STYLE_LABELS[key] || String(key);
  }

  function modeChipHtml(code) {
    var label = MODE_LABELS[code] || String(code);
    return (
      '<span class="mode-chip">' +
      esc(label) +
      '<span class="mode-code">' + esc(code) + "</span></span>"
    );
  }

  function chapterLabel(chapterId) {
    if (!chapterId) {
      return "未分章";
    }
    return CHAPTER_LABELS[chapterId] || String(chapterId);
  }

  /** 包装 GET，失败返回 {ok:false,error}，避免单个接口失败中断整页。 */
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

  // ---------------------------------------------------------------------------
  // 状态
  // ---------------------------------------------------------------------------

  var state = {
    profile: null,
    questionnaire: null,
    answers: {},
    result: null,
    strategy: null,
    analyticsDays: 7,
  };

  // ---------------------------------------------------------------------------
  // 问卷渲染
  // ---------------------------------------------------------------------------

  /** 单选题渲染：radio 选项。 */
  function renderSingleChoice(question, currentValue) {
    var html = ['<div class="q-options" role="radiogroup">'];
    (question.options || []).forEach(function (option) {
      var checked = currentValue === option.value ? " checked" : "";
      html.push(
        '<label class="q-option' + (checked ? " is-checked" : "") + '">' +
          '<input type="radio" name="q-' + esc(question.qid) + '" value="' + esc(option.value) +
          '" data-qid="' + esc(question.qid) + '"' + checked + " />" +
          "<span>" + esc(option.label) + "</span>" +
        "</label>"
      );
    });
    html.push("</div>");
    return html.join("");
  }

  /** 多选题渲染：checkbox 选项。 */
  function renderMultiChoice(question, currentValue) {
    var selected = Array.isArray(currentValue) ? currentValue : [];
    var html = ['<div class="q-options" role="group">'];
    (question.options || []).forEach(function (option) {
      var checked = selected.indexOf(option.value) !== -1;
      html.push(
        '<label class="q-option' + (checked ? " is-checked" : "") + '">' +
          '<input type="checkbox" name="q-' + esc(question.qid) + '" value="' + esc(option.value) +
          '" data-qid="' + esc(question.qid) + '"' + (checked ? " checked" : "") + " />" +
          "<span>" + esc(option.label) + "</span>" +
        "</label>"
      );
    });
    html.push("</div>");
    return html.join("");
  }

  /** 量表题渲染：range 滑块 + 当前值（依 min/max）。 */
  function renderScale(question, currentValue) {
    var min = question.min === null || question.min === undefined ? 1 : Number(question.min);
    var max = question.max === null || question.max === undefined ? 5 : Number(question.max);
    if (!isFinite(min) || !isFinite(max) || max <= min) {
      min = 1;
      max = 5;
    }
    var value = currentValue === undefined || currentValue === null || currentValue === ""
      ? Math.round((min + max) / 2)
      : Number(currentValue);
    if (!isFinite(value) || value < min) {
      value = min;
    }
    if (value > max) {
      value = max;
    }
    return (
      '<div class="q-scale">' +
        '<input type="range" name="q-' + esc(question.qid) + '" data-qid="' + esc(question.qid) +
        '" min="' + min + '" max="' + max + '" step="1" value="' + value + '" />' +
        '<span class="q-scale-value" data-scale-output="' + esc(question.qid) + '">' + value + "</span>" +
      "</div>" +
      '<div class="q-scale-ends"><span>' + min + "（少）</span><span>" + max + "（多）</span></div>"
    );
  }

  /** 依题型分派渲染；未支持的题型返回可读占位。 */
  function renderQuestionBody(question, currentValue) {
    switch (question.type) {
      case "single_choice":
        return renderSingleChoice(question, currentValue);
      case "multi_choice":
        return renderMultiChoice(question, currentValue);
      case "scale":
        return renderScale(question, currentValue);
      default:
        return '<p class="muted">暂不支持的题型：' + esc(question.type) + "</p>";
    }
  }

  /**
   * 渲染整份问卷表单。
   * @param {boolean} [useStored] 是否用画像中的历史作答回填（默认 true；重置时传 false）。
   */
  function renderQuestionnaire(useStored) {
    var form = document.getElementById("questionnaire-form");
    if (!form) {
      return;
    }
    var doc = state.questionnaire;
    var questions = doc && Array.isArray(doc.questions) ? doc.questions : [];

    setText("questionnaire-title", (doc && doc.title) || "学习习惯问卷");
    setText(
      "questionnaire-desc",
      (doc && doc.description) || "完成问卷后即可获得专属学习风格测评与推荐学习方式。"
    );

    if (!questions.length) {
      form.innerHTML = '<p class="muted">问卷暂不可用（后端未就绪或数据缺失）。</p>';
      var submitBtn = document.getElementById("questionnaire-submit");
      if (submitBtn) {
        submitBtn.disabled = true;
      }
      return;
    }

    var stored =
      useStored !== false && state.profile && state.profile.questionnaire_answers
        ? state.profile.questionnaire_answers
        : {};
    state.answers = {};

    form.innerHTML = questions
      .map(function (question, index) {
        var currentValue = Object.prototype.hasOwnProperty.call(stored, question.qid)
          ? stored[question.qid]
          : undefined;
        var dimLabel = DIMENSION_LABELS[question.dimension] || question.dimension;
        return (
          '<div class="q-item" data-qid="' + esc(question.qid) + '" data-type="' + esc(question.type) + '">' +
            '<div class="q-item-head">' +
              '<span class="q-index">' + (index + 1) + "</span>" +
              '<span class="q-title">' + esc(question.text) +
                '<span class="q-dim">· ' + esc(dimLabel) + "</span></span>" +
            "</div>" +
            renderQuestionBody(question, currentValue) +
          "</div>"
        );
      })
      .join("");

    var submitBtnEl = document.getElementById("questionnaire-submit");
    if (submitBtnEl) {
      submitBtnEl.disabled = false;
    }
    // 依据回填值初始化 answers 与计数/高亮状态。
    collectAnswersFromForm();
  }

  /**
   * 绑定选项/滑块交互（表单元素在 HTML 中静态存在，仅需绑定一次，避免重复监听）。
   * 负责更新选中样式、当前值输出与已作答计数。
   */
  function bindQuestionnaireInputs() {
    var form = document.getElementById("questionnaire-form");
    if (!form) {
      return;
    }
    form.addEventListener("change", function (event) {
      var target = event.target;
      if (!target || !target.getAttribute) {
        return;
      }
      var item = target.closest ? target.closest(".q-item") : null;

      if (target.type === "range") {
        var output = form.querySelector('[data-scale-output="' + target.getAttribute("data-qid") + '"]');
        if (output) {
          output.textContent = target.value;
        }
      } else if (target.type === "radio" || target.type === "checkbox") {
        // 同步选中态样式（radio 需清除同组其它项的选中态）。
        if (item) {
          var options = item.querySelectorAll(".q-option");
          Array.prototype.forEach.call(options, function (option) {
            var input = option.querySelector("input");
            option.classList.toggle("is-checked", !!(input && input.checked));
          });
        }
      }
      collectAnswersFromForm();
    });

    // range 拖动即时更新数值显示（change 在部分浏览器释放时才触发）。
    form.addEventListener("input", function (event) {
      var target = event.target;
      if (target && target.type === "range") {
        var output = form.querySelector('[data-scale-output="' + target.getAttribute("data-qid") + '"]');
        if (output) {
          output.textContent = target.value;
        }
        collectAnswersFromForm();
      }
    });
  }

  /**
   * 从表单收集答案到 state.answers，并更新「已作答」计数。
   * - single_choice: 字符串值
   * - multi_choice:  字符串数组
   * - scale:         整数
   * 未作答的 qid 不写入 answers（避免提交空值影响后端判分）。
   */
  function collectAnswersFromForm() {
    var form = document.getElementById("questionnaire-form");
    if (!form || !state.questionnaire) {
      return;
    }
    var questions = Array.isArray(state.questionnaire.questions) ? state.questionnaire.questions : [];
    var answers = {};
    var answered = 0;

    questions.forEach(function (question) {
      var value;
      if (question.type === "multi_choice") {
        value = Array.prototype.slice
          .call(form.querySelectorAll('input[name="q-' + question.qid + '"]:checked'))
          .map(function (input) {
            return input.value;
          });
        if (value.length === 0) {
          return;
        }
      } else if (question.type === "scale") {
        var range = form.querySelector('input[name="q-' + question.qid + '"]');
        if (!range) {
          return;
        }
        value = Number(range.value);
        if (!isFinite(value)) {
          return;
        }
      } else {
        var checked = form.querySelector('input[name="q-' + question.qid + '"]:checked');
        if (!checked) {
          return;
        }
        value = checked.value;
      }
      answers[question.qid] = value;
      answered += 1;
    });

    state.answers = answers;
    setText("questionnaire-progress", "已作答 " + answered + " / " + questions.length);
    updateQuestionnaireStatus(questions.length, answered);
  }

  /** 依据已作答数量更新问卷状态徽标。 */
  function updateQuestionnaireStatus(total, answered) {
    var badge = document.getElementById("questionnaire-status");
    if (!badge) {
      return;
    }
    if (state.profile && state.profile.questionnaire_completed) {
      badge.textContent = "已完成";
      badge.classList.remove("badge-soft");
      return;
    }
    badge.textContent = total && answered === total ? "可提交" : "未完成";
  }

  // ---------------------------------------------------------------------------
  // 提交 / 测评
  // ---------------------------------------------------------------------------

  /** 校验问卷是否全部作答；未作答项返回其 qid 列表。 */
  function findUnanswered() {
    var questions = state.questionnaire && Array.isArray(state.questionnaire.questions)
      ? state.questionnaire.questions
      : [];
    var unanswered = [];
    var form = document.getElementById("questionnaire-form");
    questions.forEach(function (question) {
      var hasValue = Object.prototype.hasOwnProperty.call(state.answers, question.qid);
      var item = form ? form.querySelector('.q-item[data-qid="' + question.qid + '"]') : null;
      if (item) {
        item.classList.toggle("is-unanswered", !hasValue);
      }
      if (!hasValue) {
        unanswered.push(question.qid);
      }
    });
    return unanswered;
  }

  function clearUnansweredHighlight() {
    var form = document.getElementById("questionnaire-form");
    if (!form) {
      return;
    }
    Array.prototype.forEach.call(form.querySelectorAll(".q-item.is-unanswered"), function (item) {
      item.classList.remove("is-unanswered");
    });
  }

  /**
   * 提交答卷。
   * @param {string} path  "/profile/questionnaire"（存答案）或 "/profile/assessment"（仅测评）
   * @param {boolean} markCompleted 成功后是否重新拉取画像以刷新完成状态
   */
  function submitAnswers(path, markCompleted) {
    var unanswered = findUnanswered();
    if (unanswered.length > 0) {
      toast("还有 " + unanswered.length + " 道题未作答，请补全后再提交", "error");
      var firstName = unanswered[0];
      var form = document.getElementById("questionnaire-form");
      var firstItem = form ? form.querySelector('.q-item[data-qid="' + firstName + '"]') : null;
      if (firstItem && firstItem.scrollIntoView) {
        firstItem.scrollIntoView({ behavior: "smooth", block: "center" });
      }
      return;
    }

    setButtonBusy("questionnaire-submit", true);
    API.post(path, { answers: state.answers }).then(
      function (result) {
        state.result = result;
        clearUnansweredHighlight();
        renderResult(result);
        if (markCompleted) {
          toast("问卷已提交，已生成你的学习风格测评", "success");
          // 刷新画像完成状态，并同步最新策略/进度。
          loadProfile().then(function () {
            loadStrategy();
            loadAnalytics(state.analyticsDays);
          });
        } else {
          toast("已重新计算测评结果", "success");
          loadStrategy();
        }
      },
      function (error) {
        toast("提交失败：" + ((error && error.message) || "未知错误"), "error");
        showBackendBanner(true);
      }
    ).then(function () {
      setButtonBusy("questionnaire-submit", false);
    });
  }

  function setButtonBusy(id, busy) {
    var btn = document.getElementById(id);
    if (btn) {
      btn.disabled = !!busy;
    }
  }

  // ---------------------------------------------------------------------------
  // 测评结果渲染
  // ---------------------------------------------------------------------------

  function renderStyleBars(scores, dominantStyle) {
    var host = document.getElementById("style-bars");
    if (!host) {
      return;
    }
    var safeScores = scores && typeof scores === "object" ? scores : {};
    host.innerHTML = STYLE_ORDER.map(function (key) {
      var raw = toNumber(safeScores[key]);
      // 归一化分 ∈[0,1] → 百分比；越界值裁剪。
      var ratio = Math.max(0, Math.min(1, raw));
      var percent = Math.round(ratio * 100);
      var isDominant = dominantStyle === key;
      return (
        '<div class="style-row' + (isDominant ? " is-dominant" : "") + '">' +
          '<span class="style-name">' + esc(STYLE_LABELS[key]) +
            (isDominant ? "（主导）" : "") + "</span>" +
          '<div class="progress"><div class="progress-bar" style="width:' + percent + '%"></div></div>' +
          '<span class="style-pct">' + percent + "%</span>" +
        "</div>"
      );
    }).join("");
  }

  function renderModeChips(hostId, modes) {
    var host = document.getElementById(hostId);
    if (!host) {
      return;
    }
    var list = Array.isArray(modes) ? modes : [];
    if (!list.length) {
      host.innerHTML = '<span class="muted">暂无推荐</span>';
      return;
    }
    host.innerHTML = list.map(modeChipHtml).join("");
  }

  function renderRationale(hostId, rationale) {
    var host = document.getElementById(hostId);
    if (!host) {
      return;
    }
    var list = Array.isArray(rationale) ? rationale : [];
    if (!list.length) {
      host.innerHTML = '<li class="muted">暂无说明</li>';
      return;
    }
    host.innerHTML = list
      .map(function (line) {
        return "<li>" + esc(line) + "</li>";
      })
      .join("");
  }

  function renderResult(result) {
    var card = document.getElementById("result-card");
    if (!card) {
      return;
    }
    card.hidden = false;

    var styleKey = result && result.learning_style;
    setText("result-style", styleLabel(styleKey));
    renderStyleBars(result && result.style_scores, styleKey);
    renderModeChips("result-modes", result && result.recommended_modes);
    renderRationale("result-rationale", result && result.rationale);
  }

  // ---------------------------------------------------------------------------
  // 策略渲染
  // ---------------------------------------------------------------------------

  function renderStrategy(strategy) {
    state.strategy = strategy || null;
    var data = strategy || {};
    setText("strategy-session", isFinite(Number(data.session_length_min)) ? String(data.session_length_min) : "–");
    setText(
      "strategy-intensity",
      isFinite(Number(data.review_intensity_multiplier))
        ? Number(data.review_intensity_multiplier).toFixed(1) + "×"
        : "–"
    );
    setText(
      "strategy-budget",
      isFinite(Number(data.daily_minutes_effective)) ? String(data.daily_minutes_effective) : "–"
    );
    renderModeChips("strategy-modes", data.recommended_modes);
    renderRationale("strategy-rationale", data.rationale);

    var generated = document.getElementById("strategy-generated");
    if (generated) {
      if (data.generated_at) {
        generated.hidden = false;
        generated.textContent = "策略生成时间：" + (ALA.formatDateTime ? ALA.formatDateTime(data.generated_at) : data.generated_at);
      } else {
        generated.hidden = true;
      }
    }
  }

  // ---------------------------------------------------------------------------
  // 进度统计渲染
  // ---------------------------------------------------------------------------

  function renderAnalytics(analytics) {
    var data = analytics || {};
    setText("stat-streak", isFinite(Number(data.streak_days)) ? String(data.streak_days) + " 天" : "–");
    setText("stat-attempts", isFinite(Number(data.attempts)) ? String(data.attempts) : "–");
    setText(
      "stat-correct-rate",
      isFinite(Number(data.correct_rate)) ? Math.round(Number(data.correct_rate) * 100) + "%" : "–"
    );
    setText("stat-kps", isFinite(Number(data.kps_in_review)) ? String(data.kps_in_review) : "–");
    setText("stat-due", isFinite(Number(data.review_due_today)) ? String(data.review_due_today) : "–");

    renderByDayChart(Array.isArray(data.by_day) ? data.by_day : []);
    renderMastery(Array.isArray(data.mastery_by_chapter) ? data.mastery_by_chapter : []);
  }

  function renderByDayChart(days) {
    var host = document.getElementById("by-day-chart");
    if (!host) {
      return;
    }
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

  function renderMastery(rows) {
    var host = document.getElementById("mastery-list");
    if (!host) {
      return;
    }
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

  // ---------------------------------------------------------------------------
  // 数据加载
  // ---------------------------------------------------------------------------

  function showBackendBanner(show) {
    var banner = document.getElementById("backend-banner");
    if (banner) {
      banner.hidden = !show;
    }
  }

  function renderHero() {
    var profile = state.profile;
    setText("style-label", styleLabel(profile && profile.learning_style));
    setText(
      "profile-daily-minutes",
      profile && isFinite(Number(profile.daily_minutes)) ? String(profile.daily_minutes) : "30"
    );
  }

  function renderGuide() {
    var banner = document.getElementById("guide-banner");
    if (!banner) {
      return;
    }
    var needQuestionnaire = !!(state.profile && state.profile.questionnaire_completed === false);
    banner.hidden = !needQuestionnaire;
    var card = document.getElementById("questionnaire-card");
    if (card) {
      card.classList.toggle("is-highlighted", needQuestionnaire);
    }
  }

  function fillSettingsForm() {
    var profile = state.profile;
    if (!profile) {
      return;
    }
    var nickname = document.getElementById("setting-nickname");
    var minutes = document.getElementById("setting-daily-minutes");
    var enabled = document.getElementById("setting-reminder-enabled");
    var time = document.getElementById("setting-reminder-time");
    if (nickname) {
      nickname.value = profile.nickname || "";
    }
    if (minutes) {
      minutes.value = isFinite(Number(profile.daily_minutes)) ? profile.daily_minutes : 30;
    }
    if (enabled) {
      enabled.checked = profile.reminder_enabled !== false;
    }
    if (time) {
      time.value = profile.reminder_time || "20:00";
    }
  }

  /** 读取画像；成功渲染 hero/引导/设置并隐藏后端不可用提示。 */
  function loadProfile() {
    return safeGet("/profile").then(function (res) {
      if (!res.ok) {
        state.profile = null;
        showBackendBanner(true);
        renderGuide();
        return null;
      }
      state.profile = res.data;
      showBackendBanner(false);
      renderHero();
      renderGuide();
      fillSettingsForm();
      return res.data;
    });
  }

  function loadQuestionnaire() {
    return safeGet("/profile/questionnaire").then(function (res) {
      if (!res.ok) {
        state.questionnaire = null;
        setText("questionnaire-status", "不可用");
        var form = document.getElementById("questionnaire-form");
        if (form) {
          form.innerHTML = '<p class="muted">问卷暂不可用（后端未就绪或数据缺失）。</p>';
        }
        setButtonBusy("questionnaire-submit", true);
        return;
      }
      state.questionnaire = res.data;
      renderQuestionnaire();
    });
  }

  function loadStrategy() {
    return safeGet("/profile/strategy").then(function (res) {
      if (!res.ok) {
        renderStrategy(null);
        renderRationale("strategy-rationale", ["策略暂不可用（后端未就绪）。"]);
        return;
      }
      renderStrategy(res.data);
    });
  }

  function loadAnalytics(days) {
    state.analyticsDays = isFinite(Number(days)) ? Number(days) : 7;
    return safeGet("/profile/analytics", { days: state.analyticsDays }).then(function (res) {
      if (!res.ok) {
        renderAnalytics(null);
        return;
      }
      renderAnalytics(res.data);
    });
  }

  // ---------------------------------------------------------------------------
  // 设置保存
  // ---------------------------------------------------------------------------

  function saveSettings() {
    var nicknameEl = document.getElementById("setting-nickname");
    var minutesEl = document.getElementById("setting-daily-minutes");
    var enabledEl = document.getElementById("setting-reminder-enabled");
    var timeEl = document.getElementById("setting-reminder-time");

    var payload = {};
    if (nicknameEl) {
      payload.nickname = nicknameEl.value.trim() || "学习者";
    }
    if (minutesEl) {
      var minutes = Number(minutesEl.value);
      payload.daily_minutes = isFinite(minutes) && minutes >= 0 ? Math.round(minutes) : 30;
    }
    if (enabledEl) {
      payload.reminder_enabled = !!enabledEl.checked;
    }
    if (timeEl && timeEl.value) {
      payload.reminder_time = timeEl.value;
    }

    setButtonBusy("settings-save", true);
    API.put("/profile", payload).then(
      function (profile) {
        state.profile = profile;
        renderHero();
        fillSettingsForm();
        toast("设置已保存", "success");
        // 预算变化会影响进度统计的展示预算，刷新一次。
        loadAnalytics(state.analyticsDays);
      },
      function (error) {
        toast("保存失败：" + ((error && error.message) || "未知错误"), "error");
      }
    ).then(function () {
      setButtonBusy("settings-save", false);
    });
  }

  // ---------------------------------------------------------------------------
  // 事件绑定
  // ---------------------------------------------------------------------------

  function bindEvents() {
    var submit = document.getElementById("questionnaire-submit");
    if (submit) {
      submit.addEventListener("click", function () {
        submitAnswers("/profile/questionnaire", true);
      });
    }

    var reset = document.getElementById("questionnaire-reset");
    if (reset) {
      reset.addEventListener("click", function () {
        clearUnansweredHighlight();
        if (state.questionnaire) {
          // 忽略历史作答回填，重置为各题默认值。
          renderQuestionnaire(false);
        }
        toast("已重置作答", "info");
      });
    }

    var assessmentOnly = document.getElementById("assessment-only");
    if (assessmentOnly) {
      assessmentOnly.addEventListener("click", function () {
        submitAnswers("/profile/assessment", false);
      });
    }

    var guideStart = document.getElementById("guide-start");
    if (guideStart) {
      guideStart.addEventListener("click", function () {
        var card = document.getElementById("questionnaire-card");
        if (card && card.scrollIntoView) {
          card.scrollIntoView({ behavior: "smooth", block: "start" });
        }
      });
    }

    var strategyRefresh = document.getElementById("strategy-refresh");
    if (strategyRefresh) {
      strategyRefresh.addEventListener("click", function () {
        strategyRefresh.disabled = true;
        loadStrategy().then(function () {
          strategyRefresh.disabled = false;
          toast("策略已刷新", "info");
        });
      });
    }

    var strategyRefreshTop = document.getElementById("strategy-refresh-top");
    if (strategyRefreshTop) {
      strategyRefreshTop.addEventListener("click", function () {
        loadStrategy().then(function () {
          toast("策略已刷新", "info");
          var card = document.getElementById("strategy-card");
          if (card && card.scrollIntoView) {
            card.scrollIntoView({ behavior: "smooth", block: "start" });
          }
        });
      });
    }

    var analyticsCard = document.getElementById("analytics-card");
    if (analyticsCard) {
      var tabs = analyticsCard.querySelectorAll(".tab[data-days]");
      Array.prototype.forEach.call(tabs, function (tab) {
        tab.addEventListener("click", function () {
          Array.prototype.forEach.call(tabs, function (other) {
            var active = other === tab;
            other.classList.toggle("is-active", active);
            other.setAttribute("aria-selected", active ? "true" : "false");
          });
          loadAnalytics(tab.getAttribute("data-days"));
        });
      });
    }

    var save = document.getElementById("settings-save");
    if (save) {
      save.addEventListener("click", saveSettings);
    }
  }

  // ---------------------------------------------------------------------------
  // 初始化
  // ---------------------------------------------------------------------------

  function init() {
    if (typeof ALA.mountNav === "function") {
      ALA.mountNav(document);
    }
    bindEvents();
    bindQuestionnaireInputs();

    // 先取画像（决定引导与设置回填），再渲染问卷（以画像历史作答回填）。
    loadProfile().then(function () {
      return loadQuestionnaire();
    });

    loadStrategy();
    loadAnalytics(state.analyticsDays);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})(window);