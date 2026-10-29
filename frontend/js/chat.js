/**
 * chat.js — 类豆包聊天界面（T11，契约 C4-CHAT@1 与 C6-UI@1）。
 *
 * 职责（plan 2.2.4 / T11）：
 *  - 角色切换：teacher（老师细讲）/ student（像学生提问）/ feynman（费曼：用户讲给 AI，AI 点评）。
 *    切换到某角色时调用 `POST /api/chat/sessions` 创建对应 `mode` 的会话。
 *  - 会话管理：列表 `GET /api/chat/sessions?limit=30`、历史 `GET /api/chat/sessions/{id}/messages?limit=100`、
 *    删除 `DELETE /api/chat/sessions/{id}`。
 *  - 流式对话：`POST /api/chat/stream`，经 `API.stream`（fetch + ReadableStream）解析四类 SSE 帧：
 *      meta  → 显示「本地模拟 / 盘古」提供方标识 + 记录 message_id
 *      delta → 逐字追加文本
 *      done  → 以完整内容收尾、移除流式光标、刷新会话顺序
 *      error → 保底错误提示（对话可继续）
 *  - 可选 `kp_id` 上下文：从知识点页带 `?kp_id=` 进入时注入会话与消息。
 *  - 无后端时展示可读错误（顶部横幅 + 消息内提示），不崩溃。
 *  - 语音（契约 C-ASR-UI@1，plan 2.2.7）：
 *      `#asr-record-btn` 主通道 = 录音 → asr.js 封装 WAV → `POST /api/asr` → 填入输入框（不自动发送）；
 *      后端不可用/失败 → 自动降级到浏览器原生 `SpeechRecognition`（`#voice-input-btn`）；
 *      `done` 帧按 `#speak-toggle` 开关用 `SpeechSynthesis` 朗读（`#speak-stop-btn` 可停止）。
 *
 * 依赖：`/static/js/api.js`（`window.API`，由 T9 提供，本文件不改）、
 *       `/static/js/asr.js`（`window.ALA.asr`，T4 新增，负责采集/WAV/上传）。
 * 说明：契约要求使用 fetch + ReadableStream（**不使用 EventSource**，因需 POST + 自定义 body）。
 */
(function (global) {
  "use strict";

  var API = global.API;
  if (!API) {
    console.error("[chat.js] api.js 未加载，聊天功能不可用");
    return;
  }

  var THEME_KEY = "ala_theme";
  var SESSION_LIMIT = 30;
  var MESSAGE_LIMIT = 100;

  //: 角色配置：名称、徽标文案、行为说明与欢迎语（严格对应 teacher/student/feynman）。
  var ROLES = {
    teacher: {
      label: "老师",
      desc: "老师细讲 · 像耐心的生物老师，由浅入深把知识点讲透、举例说明。",
      greeting: "我是你的生物老师，想让我讲解哪个知识点？把问题发给我，我尽量讲得清楚又好懂。",
      suggestions: [
        "请用生活中好懂的例子讲解细胞膜",
        "线粒体和叶绿体有什么区别？",
      ],
    },
    student: {
      label: "学生",
      desc: "像学生提问 · AI 扮演爱刨根问底的同学，通过提问引导你把知识讲清楚。",
      greeting: "我来当你的同学，通过一步步提问帮你把知识讲明白～你可以先说说你正在学的内容。",
      suggestions: [
        "我刚开始学细胞的结构，你问我几个问题吧",
        "针对细胞核，我想听听你会怎么问",
      ],
    },
    feynman: {
      label: "费曼",
      desc: "费曼技巧 · 你用自己的话讲解，AI 给出「先肯定 → 指出遗漏或偏差 → 追问」的点评。",
      greeting: "请用你自己的话讲解这个知识点，讲完后我会帮你检查：哪些讲对了、哪里讲漏了、还有哪些值得追问。",
      suggestions: [
        "我用三句话讲清 DNA 半保留复制",
        "让我试着讲讲酶的作用和特点",
      ],
    },
  };

  var MODE_LABELS = { teacher: "老师", student: "学生", feynman: "费曼" };
  //: provider 标签映射（C-LLM-PROVIDER@1：llm / pangu / local）。未知值仍原样显示。
  var PROVIDER_LABELS = { llm: "大模型", pangu: "大模型(盘古)", local: "本地模拟" };

  var state = {
    sessions: [],
    current: null,     // 当前会话对象（ChatSessionView）
    mode: "teacher",   // 当前选中角色（无会话时用于新建）
    kpId: null,        // 从 URL 注入的知识点上下文
    kpTitle: "",
    sending: false,    // 是否有进行中的流式请求
    streamProvider: null,
  };

  // ---------------------------------------------------------------------------
  // 通用工具
  // ---------------------------------------------------------------------------

  /** HTML 转义，防止后端/用户文本注入标记。 */
  function esc(value) {
    if (value === undefined || value === null) {
      return "";
    }
    return String(value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  /** 补齐两位。 */
  function pad2(value) {
    return String(value).padStart(2, "0");
  }

  /** ISO-8601 → 本地「MM-DD HH:MM」。 */
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

  /** 当前时间「HH:MM」。 */
  function nowClock() {
    var date = new Date();
    return pad2(date.getHours()) + ":" + pad2(date.getMinutes());
  }

  /** 将错误对象转为可读文案。 */
  function errorText(error) {
    if (!error) {
      return "未知错误";
    }
    var code = error.code ? "（" + error.code + "）" : "";
    if (error.code === "NETWORK_ERROR") {
      return "无法连接后端服务" + code + "。请确认已运行 python run.py 后重试。";
    }
    if (error.code === "SESSION_NOT_FOUND") {
      return "会话不存在或已删除，请新建一个对话。";
    }
    if (error.code === "KP_NOT_FOUND") {
      return "关联的知识点不存在，已切换为不关联知识点的对话。";
    }
    return (error.message || "请求失败") + code;
  }

  function $(id) {
    return document.getElementById(id);
  }

  function setText(id, text) {
    var el = $(id);
    if (el) {
      el.textContent = text;
    }
  }

  /** 顶部 Toast。 */
  function toast(message, variant) {
    var host = $("toast-host");
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

  // ---------------------------------------------------------------------------
  // 站点导航 / 主题 / 用户标识（与其它页面保持一致）
  // ---------------------------------------------------------------------------

  function mountNav() {
    var links = document.querySelectorAll(".main-nav a[data-page]");
    Array.prototype.forEach.call(links, function (link) {
      var active = link.getAttribute("data-page") === "chat";
      link.classList.toggle("is-active", active);
      if (active) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    });
  }

  function mountTheme() {
    var button = $("theme-toggle");
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

  function mountUserId() {
    var short = "本地用户";
    try {
      short = API.userId().slice(0, 8);
    } catch (err) {
      /* ignore */
    }
    setText("user-id-short", short);
    setText("footer-user", short);
  }

  // ---------------------------------------------------------------------------
  // 后端不可用横幅
  // ---------------------------------------------------------------------------

  function showBackendBanner(error) {
    var banner = $("backend-banner");
    if (!banner) {
      return;
    }
    var detail = $("backend-banner-text");
    if (detail && error) {
      detail.innerHTML =
        esc(errorText(error)) +
        " 请确认已运行 <code>python run.py</code> 并在 127.0.0.1:8000 访问本页。";
    }
    banner.hidden = false;
  }

  function hideBackendBanner() {
    var banner = $("backend-banner");
    if (banner) {
      banner.hidden = true;
    }
  }

  // ---------------------------------------------------------------------------
  // 角色 UI
  // ---------------------------------------------------------------------------

  function updateRoleUI() {
    var buttons = document.querySelectorAll(".role-btn[data-role]");
    Array.prototype.forEach.call(buttons, function (btn) {
      var active = btn.getAttribute("data-role") === state.mode;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });

    var role = ROLES[state.mode] || ROLES.teacher;
    setText("role-desc", role.desc);
    setText("mode-badge", MODE_LABELS[state.mode] || state.mode);

    var tip = $("feynman-tip");
    if (tip) {
      tip.hidden = state.mode !== "feynman";
    }
  }

  /** 渲染欢迎区（角色欢迎语 + 建议提问）。 */
  function renderWelcome() {
    var role = ROLES[state.mode] || ROLES.teacher;
    setText("welcome-text", role.greeting);
    var chips = $("welcome-chips");
    if (!chips) {
      return;
    }
    chips.innerHTML = "";
    (role.suggestions || []).forEach(function (text) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "suggest-chip";
      btn.textContent = text;
      btn.addEventListener("click", function () {
        var input = $("composer-input");
        if (input) {
          input.value = text;
          input.focus();
          autoResizeInput();
        }
      });
      chips.appendChild(btn);
    });
  }

  function toggleWelcome() {
    var welcome = $("chat-welcome");
    var list = $("msg-list");
    if (!welcome || !list) {
      return;
    }
    var hasMessages = list.children.length > 0;
    welcome.hidden = hasMessages;
  }

  // ---------------------------------------------------------------------------
  // 会话：加载 / 渲染 / 选择 / 创建 / 删除
  // ---------------------------------------------------------------------------

  function renderSessions() {
    var host = $("session-list");
    if (!host) {
      return;
    }
    setText("session-count", String(state.sessions.length));
    if (!state.sessions.length) {
      host.innerHTML = '<p class="muted">还没有会话，选择角色即可开始。</p>';
      return;
    }
    host.innerHTML = "";
    state.sessions.forEach(function (session) {
      var row = document.createElement("div");
      row.className = "session-item" + (state.current && state.current.session_id === session.session_id ? " is-active" : "");
      row.setAttribute("role", "button");
      row.setAttribute("tabindex", "0");
      row.dataset.sessionId = session.session_id;

      var title = document.createElement("span");
      title.className = "session-item-title";
      title.textContent = session.title || "新对话";

      var sub = document.createElement("span");
      sub.className = "session-item-sub";
      sub.innerHTML =
        '<span class="mode-dot ' + esc(session.mode) + '"></span>' +
        esc(MODE_LABELS[session.mode] || session.mode) + " · " + esc(formatDateTime(session.updated_at || session.created_at));

      var del = document.createElement("button");
      del.type = "button";
      del.className = "session-del";
      del.title = "删除该会话";
      del.setAttribute("aria-label", "删除该会话");
      del.textContent = "✕";
      del.addEventListener("click", function (event) {
        event.stopPropagation();
        deleteSession(session);
      });

      row.appendChild(title);
      row.appendChild(sub);
      row.appendChild(del);
      row.addEventListener("click", function () {
        selectSession(session);
      });
      row.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectSession(session);
        }
      });
      host.appendChild(row);
    });
  }

  function loadSessions() {
    return API.get("/chat/sessions", { limit: SESSION_LIMIT }).then(
      function (sessions) {
        hideBackendBanner();
        state.sessions = Array.isArray(sessions) ? sessions : [];
        renderSessions();
        return state.sessions;
      },
      function (error) {
        state.sessions = [];
        renderSessions();
        showBackendBanner(error);
        toast(errorText(error), "error");
        return [];
      }
    );
  }

  /** 渲染一条历史/实时消息。 */
  function renderMessage(message, options) {
    var opts = options || {};
    var list = $("msg-list");
    if (!list) {
      return null;
    }
    var role = message.role === "assistant" ? "assistant"
      : message.role === "system" ? "system"
      : message.role === "error" ? "error"
      : "user";

    var wrap = document.createElement("div");
    wrap.className = "msg msg-" + role;

    var avatar = document.createElement("div");
    avatar.className = "msg-avatar";
    avatar.setAttribute("aria-hidden", "true");
    if (role === "user") {
      avatar.textContent = "我";
    } else if (role === "assistant") {
      avatar.textContent = (ROLES[state.mode] || ROLES.teacher).label.charAt(0);
    } else if (role === "system") {
      avatar.textContent = "ⓘ";
    } else {
      avatar.textContent = "!";
    }

    var body = document.createElement("div");
    body.className = "msg-body";

    var bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    bubble.textContent = message.content || "";
    if (opts.streaming) {
      bubble.classList.add("is-streaming");
    }

    var meta = document.createElement("div");
    meta.className = "msg-meta";
    meta.textContent = opts.metaText || "";

    body.appendChild(bubble);
    body.appendChild(meta);
    wrap.appendChild(avatar);
    wrap.appendChild(body);
    list.appendChild(wrap);
    toggleWelcome();
    scrollToBottom();

    return {
      wrap: wrap,
      bubble: bubble,
      meta: meta,
      setContent: function (text) {
        bubble.textContent = text;
      },
      appendText: function (text) {
        bubble.textContent += text;
      },
      setMeta: function (text) {
        meta.textContent = text || "";
      },
      stopStreaming: function () {
        bubble.classList.remove("is-streaming");
      },
    };
  }

  function clearMessages() {
    var list = $("msg-list");
    if (list) {
      list.innerHTML = "";
    }
    toggleWelcome();
  }

  function selectSession(session) {
    state.current = session;
    state.mode = session.mode || state.mode;
    updateRoleUI();
    setText("chat-title", session.title || "新对话");

    var providerBadge = $("provider-badge");
    if (providerBadge) {
      providerBadge.hidden = true;
    }

    renderSessions();
    clearMessages();

    return API.get("/chat/sessions/" + encodeURIComponent(session.session_id) + "/messages", { limit: MESSAGE_LIMIT }).then(
      function (messages) {
        (Array.isArray(messages) ? messages : []).forEach(function (message) {
          if (message.role === "system") {
            return; // 历史中的 system 消息不渲染为气泡
          }
          renderMessage(message, {
            metaText: message.role === "assistant"
              ? providerLabelOf(message.provider) + " · " + formatDateTime(message.created_at)
              : formatDateTime(message.created_at),
          });
        });
        toggleWelcome();
        scrollToBottom();
      },
      function (error) {
        toast(errorText(error), "error");
      }
    );
  }

  function providerLabelOf(provider) {
    if (!provider) {
      return "";
    }
    return PROVIDER_LABELS[provider] || provider;
  }

  /**
   * 创建会话（`POST /api/chat/sessions`）。
   * kp_id 存在但知识点已失效时，自动降级为不带上下文的会话。
   */
  function createSession(mode, options) {
    var opts = options || {};
    var payload = { mode: mode };
    if (state.kpId) {
      payload.kp_id = state.kpId;
    }
    return API.post("/chat/sessions", payload).then(
      function (session) {
        hideBackendBanner();
        state.sessions.unshift(session);
        renderSessions();
        return session;
      },
      function (error) {
        if (error && error.code === "KP_NOT_FOUND" && payload.kp_id) {
          // 上下文知识点失效：去掉 kp_id 重试一次，避免阻塞对话。
          state.kpId = null;
          updateKpContextUI();
          toast("关联知识点不存在，已切换为通用对话。", "info");
          return API.post("/chat/sessions", { mode: mode });
        }
        throw error;
      }
    );
  }

  /** 切换到某角色；若当前会话即为该角色则不重复创建。 */
  function switchRole(mode) {
    if (!ROLES[mode] || state.sending) {
      return;
    }
    if (state.current && state.current.mode === mode) {
      state.mode = mode;
      updateRoleUI();
      return;
    }
    state.mode = mode;
    updateRoleUI();
    renderWelcome();

    createSession(mode).then(
      function (session) {
        state.current = session;
        setText("chat-title", session.title || "新对话");
        clearMessages();
        renderSessions();
        var providerBadge = $("provider-badge");
        if (providerBadge) {
          providerBadge.hidden = true;
        }
      },
      function (error) {
        showBackendBanner(error);
        toast(errorText(error), "error");
      }
    );
  }

  function deleteSession(session) {
    if (state.sending && state.current && state.current.session_id === session.session_id) {
      toast("回复生成中，暂不能删除当前会话。", "info");
      return;
    }
    if (!global.confirm("删除该会话及全部消息？此操作不可恢复。")) {
      return;
    }
    API.del("/chat/sessions/" + encodeURIComponent(session.session_id)).then(
      function () {
        state.sessions = state.sessions.filter(function (item) {
          return item.session_id !== session.session_id;
        });
        if (state.current && state.current.session_id === session.session_id) {
          state.current = null;
          clearMessages();
          setText("chat-title", "新对话");
          renderWelcome();
        }
        renderSessions();
        toast("会话已删除", "success");
      },
      function (error) {
        toast(errorText(error), "error");
      }
    );
  }

  // ---------------------------------------------------------------------------
  // 发送消息（流式）
  // ---------------------------------------------------------------------------

  function scrollToBottom() {
    var scroll = $("chat-scroll");
    if (scroll) {
      scroll.scrollTop = scroll.scrollHeight;
    }
  }

  function autoResizeInput() {
    var input = $("composer-input");
    if (!input) {
      return;
    }
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  }

  function updateComposerState() {
    var input = $("composer-input");
    var send = $("send-btn");
    var hint = $("composer-hint");
    if (input) {
      input.disabled = state.sending;
    }
    if (send) {
      send.disabled = state.sending;
      send.textContent = state.sending ? "生成中…" : "发送";
    }
    if (hint) {
      hint.textContent = state.sending
        ? "AI 正在生成回复…"
        : "Enter 发送 · Shift+Enter 换行";
    }
  }

  function sendMessage() {
    if (state.sending) {
      return;
    }
    var input = $("composer-input");
    if (!input) {
      return;
    }
    var text = (input.value || "").trim();
    if (!text) {
      input.focus();
      return;
    }

    input.value = "";
    autoResizeInput();

    // 新一轮对话开始前停止上一条回复的朗读，避免叠音（R4.3）。
    if (isSpeaking()) {
      stopSpeaking();
    }

    state.sending = true;
    updateComposerState();

    var ready = state.current
      ? Promise.resolve(state.current)
      : createSession(state.mode).then(function (session) {
          state.current = session;
          setText("chat-title", session.title || "新对话");
          renderSessions();
          return session;
        });

    ready.then(
      function (session) {
        runStream(session, text);
      },
      function (error) {
        state.sending = false;
        updateComposerState();
        if (input) {
          input.value = text; // 回填，便于重试
        }
        showBackendBanner(error);
        renderMessage({ role: "error", content: errorText(error) });
        toast(errorText(error), "error");
      }
    );
  }

  /** 执行一次流式对话：渲染用户气泡 + 助手占位气泡，并处理四类 SSE 帧。 */
  function runStream(session, text) {
    // 1) 用户消息（乐观渲染）
    renderMessage(
      { role: "user", content: text },
      { metaText: nowClock() }
    );

    // 2) 助手占位气泡（带流式光标）
    var assistant = renderMessage(
      { role: "assistant", content: "" },
      { streaming: true, metaText: "正在生成…" }
    );

    var finished = false;
    function finalize(metaText) {
      if (finished) {
        return;
      }
      finished = true;
      if (assistant) {
        assistant.stopStreaming();
        if (!assistant.bubble.textContent) {
          assistant.setMeta(metaText || "（无内容）");
        }
      }
      state.sending = false;
      updateComposerState();
      scrollToBottom();
    }

    // 3) 通过 API.stream（fetch + ReadableStream）解析 SSE 帧
    var streamPromise = API.stream(
      "/chat/stream",
      {
        session_id: session.session_id,
        content: text,
        mode: session.mode,
        kp_id: state.kpId || undefined,
      },
      {
        // meta（首帧，必发）：显示提供方标识
        onMeta: function (meta) {
          if (!meta) {
            return;
          }
          state.streamProvider = meta.provider || null;
          var providerBadge = $("provider-badge");
          if (providerBadge) {
            providerBadge.hidden = false;
            providerBadge.textContent = providerLabelOf(meta.provider) || "未知提供方";
          }
          if (assistant) {
            assistant.setMeta(
              providerLabelOf(meta.provider) + " · 生成中" + (meta.message_id ? " #" + meta.message_id : "")
            );
          }
        },
        // delta（0..n 帧）：逐字追加
        onDelta: function (delta) {
          var piece = delta && typeof delta.text === "string" ? delta.text : "";
          if (!piece) {
            return;
          }
          if (assistant) {
            assistant.appendText(piece);
          }
          scrollToBottom();
        },
        // done（结束，必发）：以完整内容收尾
        onDone: function (done) {
          var fullText = done && typeof done.content === "string" ? done.content : null;
          if (assistant && fullText !== null && fullText !== "") {
            assistant.setContent(fullText);
          }
          var provider = (done && done.provider) || state.streamProvider;
          if (assistant) {
            assistant.setMeta(providerLabelOf(provider) + " · " + nowClock());
          }
          var providerBadge = $("provider-badge");
          if (providerBadge && provider) {
            providerBadge.hidden = false;
            providerBadge.textContent = providerLabelOf(provider);
          }
          finalize();
          // 播报（R4.1/R4.3）：先停止上一段朗读再按开关朗读最新回复，避免叠音。
          stopSpeaking();
          var spokenText = fullText !== null ? fullText : (assistant ? assistant.bubble.textContent : "");
          if (isSpeakEnabled() && speakSupported() && spokenText) {
            speak(spokenText);
          }
          // 会话顺序可能变化（后端更新了 updated_at），轻量刷新列表。
          refreshSessionsQuiet();
        },
        // error：保底提示，对话仍可继续
        onError: function (error) {
          if (assistant) {
            assistant.stopStreaming();
            if (!assistant.bubble.textContent) {
              // 尚无内容：整条转为错误提示
              assistant.wrap.className = "msg msg-error";
              assistant.setContent(errorText(error));
              assistant.setMeta("");
            } else {
              // 已有部分内容：追加错误备注
              assistant.wrap.className = "msg msg-error";
              assistant.appendText("\n\n" + errorText(error));
              assistant.setMeta("");
            }
          }
          showBackendBanner(error);
          // 出错时停止朗读，避免继续读旧内容（2.3.4）。
          stopSpeaking();
          finalize();
        },
      }
    );

    // API.stream 在无 onError 时才抛错；此处有 onError，仍兜底以防回调未触发。
    if (streamPromise && typeof streamPromise.then === "function") {
      streamPromise.then(function () {
        finalize();
      }, function (error) {
        if (assistant) {
          assistant.wrap.className = "msg msg-error";
          assistant.setContent(errorText(error));
        }
        showBackendBanner(error);
        stopSpeaking();
        finalize();
      });
    }
  }

  /** 静默刷新会话列表（保持当前选中不变），用于更新排序。 */
  function refreshSessionsQuiet() {
    API.get("/chat/sessions", { limit: SESSION_LIMIT }).then(
      function (sessions) {
        if (!Array.isArray(sessions)) {
          return;
        }
        // 保留当前会话引用（以最新对象替换，保持选中）。
        var currentId = state.current ? state.current.session_id : null;
        state.sessions = sessions;
        if (currentId) {
          var latest = sessions.filter(function (item) {
            return item.session_id === currentId;
          })[0];
          if (latest) {
            state.current = latest;
          }
        }
        renderSessions();
      },
      function () {
        /* 静默失败：不影响主流程 */
      }
    );
  }

  // ---------------------------------------------------------------------------
  // 知识点上下文（?kp_id=）
  // ---------------------------------------------------------------------------

  function readKpId() {
    try {
      var params = new URLSearchParams(global.location.search || "");
      var kpId = params.get("kp_id");
      return kpId && kpId.trim() ? kpId.trim() : null;
    } catch (err) {
      return null;
    }
  }

  function updateKpContextUI() {
    var chip = $("kp-context");
    if (!chip) {
      return;
    }
    if (!state.kpId) {
      chip.hidden = true;
      chip.textContent = "";
      return;
    }
    chip.hidden = false;
    chip.textContent = state.kpTitle
      ? "上下文：" + state.kpTitle
      : "上下文知识点：" + state.kpId;
    chip.title = "本次对话将结合该知识点内容作答";
  }

  function loadKpContext() {
    if (!state.kpId) {
      updateKpContextUI();
      return;
    }
    updateKpContextUI();
    // 拉取知识点标题用于展示；失败静默（不阻塞对话）。
    API.get("/knowledge/kps/" + encodeURIComponent(state.kpId)).then(
      function (kp) {
        if (kp && kp.title) {
          state.kpTitle = kp.title;
          updateKpContextUI();
        }
      },
      function () {
        /* 静默：上下文仍以 kp_id 生效 */
      }
    );
  }

  // ---------------------------------------------------------------------------
  // 语音：录音上传（主）+ 浏览器原生识别（降级）+ 播报
  // 契约 C-ASR-UI@1（plan 2.2.7）。音频采集/WAV 封装/上传在 asr.js（window.ALA.asr）；
  // 本段只做 UI 编排、降级与播报，不触碰文本输入与 SSE 主流程（R7.7/R3.4）。
  // ---------------------------------------------------------------------------

  var SPEAK_STORAGE_KEY = "ala_speak_enabled"; // "1" 开 / "0" 关（默认关）
  var RECORD_TIMER_INTERVAL_MS = 200;
  var MAX_RECORD_SECONDS = 120;
  var SPEAK_CHUNK_MAX_CHARS = 160; // 分句朗读，规避部分浏览器对长文本的截断

  //: `/api/asr` 错误码 → 可读中文（对照 C-ASR-API@1 的错误表）。
  var ASR_ERROR_MESSAGES = {
    ASR_UNAVAILABLE: "后端语音识别服务暂不可用（未安装或未启用识别模型）",
    ASR_BAD_AUDIO: "录音格式不被识别（需 16kHz 单声道 PCM16 WAV），请重新录音",
    ASR_TOO_LONG: "录音过长，已超出后端允许的最大时长",
    ASR_FAILED: "后端语音识别失败，请稍后重试",
    MISSING_USER_ID: "缺少本地用户标识，请刷新页面后重试",
    NETWORK_ERROR: "无法连接后端语音识别服务",
    ASR_EMPTY_AUDIO: "没有录到有效音频，请靠近麦克风再试",
    CAPTURE_UNSUPPORTED: "当前浏览器不支持麦克风录音",
    CAPTURE_NOT_RECORDING: "当前没有进行中的录音",
  };

  //: 原生 `SpeechRecognition` 错误码 → 可读中文（R3.3）。
  var NATIVE_ERROR_MESSAGES = {
    "not-allowed": "麦克风/语音识别权限被拒绝，请在浏览器设置中允许后重试",
    "service-not-allowed": "浏览器禁止使用语音识别服务",
    "audio-capture": "未检测到麦克风，或麦克风被其他应用占用",
    "network": "语音识别网络异常，请检查网络后重试",
    "no-speech": "没有听到声音，请再说一次",
    "aborted": "语音识别已取消",
    "language-not-supported": "当前浏览器不支持中文语音识别",
  };

  var voiceState = {
    recording: false,     // 主通道：正在录音
    uploading: false,     // 主通道：正在上传/等待识别结果
    listening: false,     // 降级通道：原生识别进行中
    nativeBaseText: "",   // 原生识别启动时输入框中的既有文本
    nativeFinalText: "",  // 原生识别已确认（isFinal）的文本
    nativeHadError: false,
    recordTimerId: null,
    elapsedMs: 0,
    speakEnabledMemory: false, // localStorage 不可用时（隐私模式）的内存兜底
    speakSupportedMemory: null,
    speaking: false,
    speakQueue: [],
    speakToken: 0,
    recognition: null,
  };

  /** 取 asr.js 暴露的模块（未加载时返回 null，调用方需降级而非崩溃）。 */
  function asrModule() {
    return (global.ALA && global.ALA.asr) || null;
  }

  function setAsrStatus(text, variant) {
    var el = $("asr-status");
    if (!el) {
      return;
    }
    el.textContent = text || "";
    el.className = "voice-status" + (variant ? " is-" + variant : "");
  }

  function setVoiceStatus(text, variant) {
    var el = $("voice-status");
    if (!el) {
      return;
    }
    el.textContent = text || "";
    el.className = "voice-status voice-status-sub" + (variant ? " is-" + variant : "");
  }

  // ------------------------------ 输入框回填 ------------------------------

  /**
   * 派发原生 `input` 事件，保证输入框的联动逻辑（自动增高、外部监听）同步执行。
   * 语音回填**不自动发送**，只写值 + 派发事件。
   */
  function dispatchComposerInputEvent(input) {
    var inputEvent;
    try {
      inputEvent = new global.Event("input", { bubbles: true });
    } catch (constructorError) {
      inputEvent = document.createEvent("Event");
      inputEvent.initEvent("input", true, false);
    }
    input.dispatchEvent(inputEvent);
  }

  /** 覆盖写入输入框（原生识别 interim 实时回填用）。 */
  function writeComposerValue(value) {
    var input = $("composer-input");
    if (!input) {
      return;
    }
    input.value = value === undefined || value === null ? "" : String(value);
    autoResizeInput();
    dispatchComposerInputEvent(input);
  }

  /** 追加写入输入框（录音识别结果用，保留用户已输入的内容）。 */
  function appendComposerValue(value) {
    var text = typeof value === "string" ? value.trim() : "";
    if (!text) {
      return false;
    }
    var input = $("composer-input");
    if (!input) {
      return false;
    }
    var existing = input.value || "";
    input.value = existing ? existing.replace(/\s+$/, "") + " " + text : text;
    autoResizeInput();
    dispatchComposerInputEvent(input);
    try {
      input.focus();
    } catch (focusError) {
      /* 失焦不影响回填 */
    }
    return true;
  }

  // ------------------------------ 通用文案 ------------------------------

  function formatDuration(durationMs) {
    var totalSeconds = Math.floor((durationMs || 0) / 1000);
    return pad2(Math.floor(totalSeconds / 60)) + ":" + pad2(totalSeconds % 60);
  }

  function asrErrorText(error) {
    if (!error) {
      return "语音识别失败";
    }
    var known = error.code ? ASR_ERROR_MESSAGES[error.code] : "";
    if (known) {
      return known;
    }
    var message = error.message || "语音识别失败";
    return error.code ? message + "（" + error.code + "）" : message;
  }

  // ------------------------------ 主通道：录音上传 ------------------------------

  function recordSupported() {
    var asr = asrModule();
    return !!(asr && typeof asr.isSupported === "function" && asr.isSupported());
  }

  function isRecording() {
    return voiceState.recording === true;
  }

  function updateRecordButton() {
    var btn = $("asr-record-btn");
    if (!btn) {
      return;
    }
    var recording = voiceState.recording;
    btn.setAttribute("aria-pressed", recording ? "true" : "false");
    var label = btn.querySelector(".voice-btn-text");
    if (label) {
      label.textContent = recording ? "停止并识别" : "录音";
    }
    if (recording) {
      btn.classList.add("is-recording");
    } else {
      btn.classList.remove("is-recording");
    }
    btn.disabled = voiceState.uploading || !recordSupported();
  }

  function startRecordTimer() {
    stopRecordTimer();
    voiceState.elapsedMs = 0;
    voiceState.recordTimerId = global.setInterval(function () {
      voiceState.elapsedMs += RECORD_TIMER_INTERVAL_MS;
      if (voiceState.elapsedMs > MAX_RECORD_SECONDS * 1000) {
        // 到达本地安全上限：自动结束并走正常上传路径。
        stopVoiceRecord();
        return;
      }
      setAsrStatus(
        "正在录音 " + formatDuration(voiceState.elapsedMs) + "…再次点击「停止并识别」结束",
        ""
      );
    }, RECORD_TIMER_INTERVAL_MS);
  }

  function stopRecordTimer() {
    if (voiceState.recordTimerId !== null) {
      global.clearInterval(voiceState.recordTimerId);
      voiceState.recordTimerId = null;
    }
  }

  /**
   * 主通道启动录音。
   * @returns {Promise<boolean>} 是否成功进入录音状态
   */
  function startVoiceRecord() {
    if (voiceState.uploading) {
      return Promise.resolve(false);
    }
    if (voiceState.recording) {
      return Promise.resolve(true);
    }
    var asr = asrModule();
    if (!asr) {
      setAsrStatus("录音模块未加载（asr.js），已改用「原生识别」或键盘输入", "error");
      return Promise.resolve(false);
    }

    // 与原生识别互斥：先停掉可能进行中的原生识别。
    stopNativeInput();

    setAsrStatus("正在请求麦克风权限…", "warn");
    return asr.startCapture().then(
      function () {
        voiceState.recording = true;
        updateRecordButton();
        startRecordTimer();
        setAsrStatus("正在录音 " + formatDuration(0) + "…再次点击「停止并识别」结束", "");
        return true;
      },
      function (captureError) {
        voiceState.recording = false;
        stopRecordTimer();
        updateRecordButton();
        handleAsrFailure(captureError, "麦克风启动失败");
        return false;
      }
    );
  }

  /**
   * 主通道停止录音 → 封装 WAV → `POST /api/asr` → 回填输入框。
   * @returns {Promise<boolean>} 是否成功得到并回填识别结果
   */
  function stopVoiceRecord() {
    if (!voiceState.recording) {
      return Promise.resolve(false);
    }
    var asr = asrModule();
    stopRecordTimer();
    voiceState.recording = false;
    voiceState.uploading = true;
    updateRecordButton();
    setAsrStatus("正在封装录音并上传识别…", "warn");

    var captureStopPromise;
    if (asr && typeof asr.stopCapture === "function") {
      captureStopPromise = asr.stopCapture();
    } else {
      captureStopPromise = Promise.reject({
        code: "CAPTURE_UNSUPPORTED",
        message: "录音模块未加载",
      });
    }

    return captureStopPromise.then(
      function (captureResult) {
        return asr.transcribeViaBackend(captureResult.blob).then(
          function (text) {
            voiceState.uploading = false;
            updateRecordButton();
            var transcript = typeof text === "string" ? text.trim() : "";
            if (!transcript) {
              setAsrStatus("没有识别到语音内容，请再说一次", "warn");
              return false;
            }
            appendComposerValue(transcript);
            // 状态栏只展示预览，避免整段识别文本撑满一行。
            var preview = transcript.length > 40 ? transcript.slice(0, 40) + "…" : transcript;
            setAsrStatus("识别结果：" + preview, "ok");
            return true;
          },
          function (uploadError) {
            voiceState.uploading = false;
            updateRecordButton();
            handleAsrFailure(uploadError, "录音识别失败");
            return false;
          }
        );
      },
      function (stopError) {
        voiceState.uploading = false;
        updateRecordButton();
        handleAsrFailure(stopError, "录音识别失败");
        return false;
      }
    );
  }

  /** 主通道按钮切换：录音中 → 停止上传；否则 → 开始录音。 */
  function toggleVoiceRecord() {
    if (voiceState.recording) {
      return stopVoiceRecord();
    }
    return startVoiceRecord();
  }

  /**
   * 统一失败处理（R7.7）：给出可读中文提示，并在原生识别可用时**自动降级**；
   * 原生也不可用时明确提示「语音输入不可用」。任何分支都不影响文本输入与发送。
   */
  function handleAsrFailure(error, prefix) {
    var message = asrErrorText(error);
    var fullMessage = prefix ? prefix + "：" + message : message;
    if (nativeInputSupported()) {
      setAsrStatus(fullMessage + "，已自动切换到浏览器原生识别…", "warn");
      startNativeInput({ notice: "后端识别不可用，已切换到浏览器原生识别，请说话…" });
      return;
    }
    setAsrStatus(fullMessage + "；语音输入不可用，可直接用键盘输入", "error");
    setVoiceStatus("语音输入不可用：后端识别失败，且当前浏览器不支持原生识别", "error");
  }

  // ------------------------------ 降级通道：原生识别 ------------------------------

  function getSpeechRecognitionCtor() {
    return global.SpeechRecognition || global.webkitSpeechRecognition || null;
  }

  function nativeInputSupported() {
    return typeof getSpeechRecognitionCtor() === "function";
  }

  function isListening() {
    return voiceState.listening === true;
  }

  function updateNativeInputButton() {
    var btn = $("voice-input-btn");
    if (!btn) {
      return;
    }
    var listening = voiceState.listening;
    btn.setAttribute("aria-pressed", listening ? "true" : "false");
    var label = btn.querySelector(".voice-btn-text");
    if (label) {
      label.textContent = listening ? "停止识别" : "原生识别";
    }
    if (listening) {
      btn.classList.add("is-listening");
    } else {
      btn.classList.remove("is-listening");
    }
    btn.disabled = !nativeInputSupported();
  }

  /**
   * 启动浏览器原生识别（`lang=zh-CN`、`interimResults=true`、`continuous=false`）。
   * @param {{notice?: string}} [options]
   * @returns {boolean} 是否成功发起
   */
  function startNativeInput(options) {
    var settings = options || {};
    if (!nativeInputSupported()) {
      setVoiceStatus("当前浏览器不支持原生语音识别，请使用键盘输入", "warn");
      return false;
    }
    if (voiceState.listening) {
      return true;
    }

    var RecognitionCtor = getSpeechRecognitionCtor();
    var recognition;
    try {
      recognition = new RecognitionCtor();
    } catch (constructorError) {
      setVoiceStatus(
        "原生语音识别初始化失败：" + ((constructorError && constructorError.message) || "未知原因"),
        "error"
      );
      return false;
    }

    recognition.lang = "zh-CN";
    recognition.interimResults = true;
    recognition.continuous = false;

    var input = $("composer-input");
    voiceState.nativeBaseText = input && input.value ? input.value.replace(/\s+$/, "") : "";
    voiceState.nativeFinalText = "";
    voiceState.nativeHadError = false;

    recognition.onstart = function () {
      voiceState.listening = true;
      updateNativeInputButton();
      setVoiceStatus(settings.notice || "正在使用浏览器原生识别，请说话…", "");
    };

    recognition.onresult = function (event) {
      var interimText = "";
      var finalText = "";
      var results = event && event.results ? event.results : [];
      var startIndex = (event && event.resultIndex) || 0;
      for (var i = startIndex; i < results.length; i++) {
        var result = results[i];
        var transcript = result && result[0] ? result[0].transcript : "";
        if (result.isFinal) {
          finalText += transcript;
        } else {
          interimText += transcript;
        }
      }
      voiceState.nativeFinalText += finalText;

      var parts = [];
      if (voiceState.nativeBaseText) {
        parts.push(voiceState.nativeBaseText);
      }
      if (voiceState.nativeFinalText.trim()) {
        parts.push(voiceState.nativeFinalText.trim());
      }
      if (interimText.trim()) {
        parts.push(interimText.trim());
      }
      var combined = "";
      for (var j = 0; j < parts.length; j++) {
        if (!parts[j]) {
          continue;
        }
        combined += (combined ? " " : "") + parts[j];
      }
      writeComposerValue(combined);
    };

    recognition.onerror = function (event) {
      voiceState.nativeHadError = true;
      var code = (event && event.error) || "unknown";
      var message = NATIVE_ERROR_MESSAGES[code] || ("语音识别出错（" + code + "）");
      setVoiceStatus(message, code === "aborted" ? "warn" : "error");
    };

    recognition.onend = function () {
      voiceState.listening = false;
      voiceState.recognition = null;
      updateNativeInputButton();
      if (voiceState.nativeHadError) {
        return; // 保留 onerror 的具体提示
      }
      if (voiceState.nativeFinalText.trim()) {
        setVoiceStatus("识别完成，结果已填入输入框（未自动发送）", "ok");
      } else {
        setVoiceStatus("原生识别已结束，未识别到内容", "warn");
      }
    };

    try {
      recognition.start();
    } catch (startError) {
      voiceState.recognition = null;
      voiceState.listening = false;
      updateNativeInputButton();
      setVoiceStatus(
        "无法启动原生语音识别：" + ((startError && startError.message) || "请稍后重试"),
        "error"
      );
      return false;
    }

    voiceState.recognition = recognition;
    voiceState.listening = true; // 乐观置位，onstart 会再次确认
    updateNativeInputButton();
    return true;
  }

  function stopNativeInput() {
    var recognition = voiceState.recognition;
    if (recognition && typeof recognition.stop === "function") {
      try {
        recognition.stop();
      } catch (stopError) {
        /* 已结束时 stop 可能抛错，忽略 */
      }
    }
    voiceState.listening = false;
    updateNativeInputButton();
  }

  // ------------------------------ 播报（SpeechSynthesis） ------------------------------

  function speakSupported() {
    if (voiceState.speakSupportedMemory !== null) {
      return voiceState.speakSupportedMemory;
    }
    var supported = !!(
      global.speechSynthesis &&
      typeof global.SpeechSynthesisUtterance === "function"
    );
    voiceState.speakSupportedMemory = supported;
    return supported;
  }

  function isSpeakEnabled() {
    var enabled = false;
    try {
      enabled = global.localStorage.getItem(SPEAK_STORAGE_KEY) === "1";
    } catch (storageError) {
      enabled = voiceState.speakEnabledMemory;
    }
    return enabled;
  }

  function setSpeakEnabled(enabled) {
    var flag = enabled === true;
    voiceState.speakEnabledMemory = flag;
    try {
      global.localStorage.setItem(SPEAK_STORAGE_KEY, flag ? "1" : "0");
    } catch (storageError) {
      /* 隐私模式：仅本次会话生效 */
    }
    updateSpeakToggle();
  }

  function updateSpeakToggle() {
    var toggle = $("speak-toggle");
    if (!toggle) {
      return;
    }
    var supported = speakSupported();
    var enabled = supported && isSpeakEnabled();
    toggle.setAttribute("aria-pressed", enabled ? "true" : "false");
    toggle.disabled = !supported;
    if (!supported) {
      toggle.title = "当前浏览器不支持语音合成，无法播报";
    } else {
      toggle.title = enabled
        ? "已开启：AI 回复完成后自动朗读（点击关闭）"
        : "已关闭：点击开启自动朗读";
    }
    if (enabled) {
      toggle.classList.add("is-active");
    } else {
      toggle.classList.remove("is-active");
    }
    updateSpeakStopButton();
  }

  function updateSpeakStopButton() {
    var stopButton = $("speak-stop-btn");
    if (!stopButton) {
      return;
    }
    var supported = speakSupported();
    // 仅在有朗读内容时显示「停止播报」，其余情况保持隐藏（id 仍保留，对照契约）。
    stopButton.hidden = !supported || !voiceState.speaking;
    stopButton.disabled = !supported || !voiceState.speaking;
  }

  function isSpeaking() {
    return voiceState.speaking === true;
  }

  /** 长文本按句切分（≤160 字），避免部分浏览器截断或提前结束朗读。 */
  function splitSpeechChunks(text) {
    var chunks = [];
    var remaining = text;
    var punctuations = "。！？；：，、\n";
    while (remaining.length > SPEAK_CHUNK_MAX_CHARS) {
      var windowText = remaining.slice(0, SPEAK_CHUNK_MAX_CHARS);
      var cutIndex = -1;
      for (var i = windowText.length - 1; i >= 0; i--) {
        if (punctuations.indexOf(windowText.charAt(i)) !== -1) {
          cutIndex = i;
          break;
        }
      }
      if (cutIndex <= 0) {
        cutIndex = SPEAK_CHUNK_MAX_CHARS - 1;
      }
      chunks.push(remaining.slice(0, cutIndex + 1));
      remaining = remaining.slice(cutIndex + 1);
    }
    if (remaining.trim()) {
      chunks.push(remaining);
    }
    return chunks;
  }

  function speakNextChunk(token) {
    if (token !== voiceState.speakToken) {
      return; // 已被 stopSpeaking 取消
    }
    if (!voiceState.speakQueue.length) {
      voiceState.speaking = false;
      updateSpeakStopButton();
      return;
    }
    var chunk = voiceState.speakQueue.shift();
    var utterance;
    try {
      utterance = new global.SpeechSynthesisUtterance(chunk);
    } catch (constructorError) {
      voiceState.speaking = false;
      voiceState.speakQueue = [];
      updateSpeakStopButton();
      return;
    }
    utterance.lang = "zh-CN";
    utterance.rate = 1;
    utterance.pitch = 1;
    utterance.onend = function () {
      if (token !== voiceState.speakToken) {
        return;
      }
      speakNextChunk(token);
    };
    utterance.onerror = function () {
      if (token !== voiceState.speakToken) {
        return;
      }
      voiceState.speaking = false;
      voiceState.speakQueue = [];
      updateSpeakStopButton();
    };
    try {
      global.speechSynthesis.speak(utterance);
    } catch (speakError) {
      voiceState.speaking = false;
      voiceState.speakQueue = [];
      updateSpeakStopButton();
    }
  }

  /**
   * 中文朗读文本（R4.1）。总是先取消上一段朗读，避免叠音（R4.3）。
   * @returns {boolean} 是否已开始朗读
   */
  function speak(text) {
    if (!speakSupported()) {
      return false;
    }
    var content = typeof text === "string" ? text.trim() : "";
    if (!content) {
      return false;
    }
    stopSpeaking();
    var chunks = splitSpeechChunks(content);
    if (!chunks.length) {
      return false;
    }
    voiceState.speakToken += 1;
    voiceState.speakQueue = chunks;
    voiceState.speaking = true;
    updateSpeakStopButton();
    speakNextChunk(voiceState.speakToken);
    return true;
  }

  /** 停止朗读并清空待播队列（R4.3）。 */
  function stopSpeaking() {
    voiceState.speakToken += 1;
    voiceState.speakQueue = [];
    voiceState.speaking = false;
    if (global.speechSynthesis) {
      try {
        global.speechSynthesis.cancel();
      } catch (cancelError) {
        /* ignore */
      }
    }
    updateSpeakStopButton();
  }

  // ------------------------------ 绑定与初始化 ------------------------------

  function bindVoiceEvents() {
    var recordButton = $("asr-record-btn");
    if (recordButton) {
      recordButton.addEventListener("click", function () {
        toggleVoiceRecord();
      });
    }

    var nativeButton = $("voice-input-btn");
    if (nativeButton) {
      nativeButton.addEventListener("click", function () {
        if (voiceState.listening) {
          stopNativeInput();
          setVoiceStatus("已停止原生识别", "");
          return;
        }
        if (voiceState.recording) {
          setVoiceStatus("正在录音，请先点击「停止并识别」结束录音", "warn");
          return;
        }
        startNativeInput({});
      });
    }

    var speakToggle = $("speak-toggle");
    if (speakToggle) {
      speakToggle.addEventListener("click", function () {
        if (!speakSupported()) {
          setVoiceStatus("当前浏览器不支持语音合成，无法播报", "warn");
          return;
        }
        var next = !isSpeakEnabled();
        setSpeakEnabled(next);
        if (!next) {
          stopSpeaking();
        }
      });
    }

    var speakStopButton = $("speak-stop-btn");
    if (speakStopButton) {
      speakStopButton.addEventListener("click", function () {
        stopSpeaking();
      });
    }
  }

  /** 初始化语音区域的可用性提示（能力缺失只禁用对应按钮，不阻塞聊天主流程）。 */
  function initVoiceUI() {
    var asrReady = recordSupported();
    var recordButton = $("asr-record-btn");
    if (recordButton) {
      if (asrReady) {
        recordButton.title = "录音并上传后端识别（主通道）";
      } else {
        recordButton.disabled = true;
        recordButton.title = "当前浏览器不支持麦克风录音";
      }
    }
    if (asrReady) {
      setAsrStatus("录音上传识别（主通道）：点击「录音」开始说话", "");
    } else if (nativeInputSupported()) {
      setAsrStatus("当前浏览器不支持录音上传识别，可改用「原生识别」或键盘输入", "warn");
    } else {
      setAsrStatus("语音输入不可用：浏览器既不支持录音也不支持原生识别，请用键盘输入", "error");
    }

    var nativeButton = $("voice-input-btn");
    if (nativeButton && !nativeInputSupported()) {
      nativeButton.disabled = true;
      nativeButton.title = "当前浏览器不支持原生语音识别";
      setVoiceStatus("当前浏览器不支持原生语音识别", "warn");
    } else {
      setVoiceStatus("浏览器原生识别（降级通道）", "");
    }

    if (!speakSupported()) {
      var speakToggle = $("speak-toggle");
      if (speakToggle) {
        speakToggle.title = "当前浏览器不支持语音合成，无法播报";
      }
    }

    updateRecordButton();
    updateNativeInputButton();
    updateSpeakToggle();
  }

  /** 挂载对外调试/验证入口 `window.ALA.voice`（契约 C-ASR-UI@1）。 */
  function mountVoiceApi() {
    global.ALA = global.ALA || {};
    global.ALA.voice = {
      recordSupported: recordSupported(),
      isRecording: isRecording,
      startRecording: function () {
        return startVoiceRecord();
      },
      stopRecording: function () {
        return stopVoiceRecord();
      },
      transcribeViaBackend: function (wavBlob) {
        var asr = asrModule();
        if (!asr || typeof asr.transcribeViaBackend !== "function") {
          return Promise.reject({
            code: "CAPTURE_UNSUPPORTED",
            message: "录音模块未加载（asr.js）",
          });
        }
        return asr.transcribeViaBackend(wavBlob);
      },
      nativeInputSupported: nativeInputSupported(),
      inputSupported: nativeInputSupported(),
      isListening: isListening,
      startInput: function () {
        return startNativeInput({});
      },
      stopInput: stopNativeInput,
      speakSupported: speakSupported(),
      isSpeaking: isSpeaking,
      speak: speak,
      stopSpeaking: stopSpeaking,
    };
  }

  // ---------------------------------------------------------------------------
  // 事件绑定与初始化
  // ---------------------------------------------------------------------------

  function bindEvents() {
    var roleButtons = document.querySelectorAll(".role-btn[data-role]");
    Array.prototype.forEach.call(roleButtons, function (btn) {
      btn.addEventListener("click", function () {
        switchRole(btn.getAttribute("data-role"));
      });
    });

    var newBtn = $("new-session-btn");
    if (newBtn) {
      newBtn.addEventListener("click", function () {
        switchRole(state.mode);
      });
    }

    var clearBtn = $("clear-btn");
    if (clearBtn) {
      clearBtn.addEventListener("click", function () {
        clearMessages();
        renderWelcome();
      });
    }

    var form = $("composer");
    if (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        sendMessage();
      });
    }

    var input = $("composer-input");
    if (input) {
      input.addEventListener("keydown", function (event) {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          sendMessage();
        }
      });
      input.addEventListener("input", autoResizeInput);
    }
  }

  function init() {
    mountNav();
    mountTheme();
    mountUserId();
    bindEvents();
    bindVoiceEvents();
    initVoiceUI();
    mountVoiceApi();

    state.kpId = readKpId();
    loadKpContext();

    state.mode = state.mode || "teacher";
    updateRoleUI();
    renderWelcome();
    toggleWelcome();
    updateComposerState();

    loadSessions().then(function (sessions) {
      if (sessions.length) {
        selectSession(sessions[0]);
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})(window);