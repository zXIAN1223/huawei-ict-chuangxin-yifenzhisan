/**
 * knowledge.js — 知识点浏览与主动回忆练习交互逻辑（T10 独占写入）。
 *
 * 消费契约：
 *   - C6-UI@1：调用 window.API（由 T9 的 api.js 提供；本文件不重复实现 fetch 封装）。
 *   - C1-KNOWLEDGE@1：GET /api/knowledge/chapters、/kps、/kps/{kp_id}、
 *     /kps/{kp_id}/mindmap、/kps/{kp_id}/images。
 *   - C3-QUIZ@1：GET /api/quiz/{kp_id}、POST /api/quiz/{kp_id}/attempt。
 *   - C2-REVIEW@1：POST /api/review/items（加入复习）。
 *   - C5-PROFILE@1：POST /api/log/behavior（kp_view 行为日志，失败静默）。
 *
 * 主要能力：
 *   1) 章节 → 知识点列表浏览，支持标签与难度区间筛选；
 *   2) 渲染 content_blocks 全部类型（paragraph/list/image/table/callout/formula）；
 *   3) 配图渲染，加载失败时显示占位与 caption（AC-R7-2）；
 *   4) 自绘 SVG 思维导图（window.Mindmap，离线可用，AC-R7-3）；
 *   5) 答题交互：取题 → 作答 → 提交 → 展示判分结果与更新后的复习状态。
 */
(function (global) {
  "use strict";

  var API = global.API;
  var Mindmap = global.Mindmap;

  // 题型 → 中文标签
  var QUIZ_TYPE_LABELS = {
    single_choice: "单选",
    multi_choice: "多选",
    true_false: "判断",
    short_answer: "简答"
  };

  // callout variant → 展示标签与图标
  var CALLOUT_META = {
    tip: { icon: "💡", label: "提示" },
    note: { icon: "📝", label: "笔记" },
    warning: { icon: "⚠️", label: "注意" }
  };

  // 判分来源 → 中文说明
  var GRADED_BY_LABELS = {
    local: "本地关键词判分",
    ai: "AI 语义判分",
    "local+ai": "关键词 + AI 判分"
  };

  // 难度 / 重要性文字分级
  var LEVEL_TEXT = { 1: "很低", 2: "较低", 3: "中等", 4: "较高", 5: "很高" };

  // 页面状态
  var state = {
    chapters: [],
    currentChapterId: null,
    knowledgePoints: [], // 当前筛选下的知识点元数据列表
    tagOptions: [], // 全部标签（用于筛选器）
    selectedTags: [],
    difficultyMin: null,
    difficultyMax: null,
    currentKpId: null,
    currentKp: null, // 完整知识点对象
    currentMindmap: null,
    quizItems: []
  };

  // DOM 缓存
  var dom = {};

  // ------------------------------------------------------------------ //
  // 通用工具
  // ------------------------------------------------------------------ //

  function createElement(tag, className, text) {
    var node = global.document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function clearElement(node) {
    while (node && node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function onDomReady(callback) {
    if (global.document.readyState === "loading") {
      global.document.addEventListener("DOMContentLoaded", callback);
    } else {
      callback();
    }
  }

  /** 将任意错误对象收敛为可读文本。 */
  function describeError(error) {
    if (!error) {
      return "未知错误";
    }
    return error.message || error.code || String(error);
  }

  /** 数组或字符串统一的答案展示。 */
  function formatAnswer(answer) {
    if (Array.isArray(answer)) {
      return answer.join("、");
    }
    if (answer === undefined || answer === null) {
      return "";
    }
    return String(answer);
  }

  /** ISO 时间字符串 → 本地可读文本（容错，非法值原样返回）。 */
  function formatDateTime(value) {
    if (!value) {
      return "-";
    }
    var date = new Date(value);
    if (isNaN(date.getTime())) {
      return String(value);
    }
    return date.toLocaleString("zh-CN", { hour12: false });
  }

  function toast(message, type) {
    if (!dom.toastHost || !message) {
      return;
    }
    var element = createElement("div", "toast toast-" + (type || "info"), message);
    dom.toastHost.appendChild(element);
    global.setTimeout(function () {
      element.style.opacity = "0";
      global.setTimeout(function () {
        if (element.parentNode) {
          element.parentNode.removeChild(element);
        }
      }, 240);
    }, 3200);
  }

  function showBackendBanner() {
    if (dom.backendBanner) {
      dom.backendBanner.hidden = false;
    }
  }

  function hideBackendBanner() {
    if (dom.backendBanner) {
      dom.backendBanner.hidden = true;
    }
  }

  // ------------------------------------------------------------------ //
  // DOM 初始化 / 主题 / 用户标识
  // ------------------------------------------------------------------ //

  function cacheDom() {
    dom.backendBanner = global.document.getElementById("backend-banner");
    dom.chapterList = global.document.getElementById("chapter-list");
    dom.kpCount = global.document.getElementById("kp-count");
    dom.tagFilter = global.document.getElementById("tag-filter");
    dom.difficultyMin = global.document.getElementById("difficulty-min");
    dom.difficultyMax = global.document.getElementById("difficulty-max");
    dom.filterReset = global.document.getElementById("filter-reset");
    dom.kpList = global.document.getElementById("kp-list");
    dom.readerStatus = global.document.getElementById("reader-status");
    dom.readerStatusText = global.document.getElementById("reader-status-text");
    dom.article = global.document.getElementById("kp-article");
    dom.kpTitle = global.document.getElementById("kp-title");
    dom.kpSummary = global.document.getElementById("kp-summary");
    dom.kpMeta = global.document.getElementById("kp-meta");
    dom.kpTags = global.document.getElementById("kp-tags");
    dom.addReviewBtn = global.document.getElementById("add-review-btn");
    dom.kpContent = global.document.getElementById("kp-content");
    dom.kpGallery = global.document.getElementById("kp-gallery");
    dom.mindmapCanvas = global.document.getElementById("mindmap-canvas");
    dom.mindmapFit = global.document.getElementById("mindmap-fit");
    dom.mindmapRefresh = global.document.getElementById("mindmap-refresh");
    dom.quizList = global.document.getElementById("quiz-list");
    dom.quizProgress = global.document.getElementById("quiz-progress");
    dom.toastHost = global.document.getElementById("toast-host");
    dom.themeToggle = global.document.getElementById("theme-toggle");
    dom.userIdShort = global.document.getElementById("user-id-short");
    dom.userIdFooter = global.document.getElementById("footer-user");
  }

  function setupThemeToggle() {
    var root = global.document.documentElement;
    if (!dom.themeToggle) {
      return;
    }
    var syncIcon = function () {
      dom.themeToggle.textContent =
        root.getAttribute("data-theme") === "dark" ? "☀️" : "🌙";
    };
    syncIcon();
    dom.themeToggle.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try {
        global.localStorage.setItem("ala_theme", next);
      } catch (err) {
        /* localStorage 不可用时仅内存生效 */
      }
      syncIcon();
    });
  }

  function renderUserIdentifier() {
    if (!API) {
      return;
    }
    var userId = API.userId();
    if (dom.userIdShort) {
      dom.userIdShort.textContent = userId.slice(0, 8);
    }
    if (dom.userIdFooter) {
      dom.userIdFooter.textContent = userId;
    }
  }

  // ------------------------------------------------------------------ //
  // 阅读区状态
  // ------------------------------------------------------------------ //

  function showReaderStatus(text) {
    if (dom.readerStatusText) {
      dom.readerStatusText.textContent = text;
    }
    if (dom.readerStatus) {
      dom.readerStatus.hidden = false;
    }
    if (dom.article) {
      dom.article.hidden = true;
    }
  }

  function showArticle() {
    if (dom.readerStatus) {
      dom.readerStatus.hidden = true;
    }
    if (dom.article) {
      dom.article.hidden = false;
    }
  }

  // ------------------------------------------------------------------ //
  // 章节
  // ------------------------------------------------------------------ //

  async function loadChapters() {
    var chapters = await API.get("/knowledge/chapters");
    state.chapters = Array.isArray(chapters) ? chapters : [];
  }

  function renderChapterList() {
    if (!dom.chapterList) {
      return;
    }
    clearElement(dom.chapterList);
    if (!state.chapters.length) {
      dom.chapterList.appendChild(createElement("p", "muted", "暂无章节"));
      return;
    }
    state.chapters.forEach(function (chapter) {
      var item = createElement("button", "chapter-item");
      item.type = "button";
      item.dataset.chapterId = chapter.chapter_id;
      if (chapter.chapter_id === state.currentChapterId) {
        item.classList.add("is-active");
      }
      item.appendChild(
        createElement("span", "chapter-item-title", chapter.chapter_title || chapter.chapter_id)
      );
      item.appendChild(
        createElement(
          "span",
          "chapter-item-sub",
          (chapter.kp_count || 0) + " 个知识点"
        )
      );
      item.addEventListener("click", function () {
        selectChapter(chapter.chapter_id);
      });
      dom.chapterList.appendChild(item);
    });
  }

  async function selectChapter(chapterId) {
    state.currentChapterId = chapterId;
    renderChapterList();
    await loadKnowledgePointList();
    if (state.knowledgePoints.length) {
      selectKnowledgePoint(state.knowledgePoints[0].kp_id);
    } else {
      state.currentKpId = null;
      state.currentKp = null;
      showReaderStatus("该章节下没有符合当前筛选条件的知识点。");
    }
  }

  // ------------------------------------------------------------------ //
  // 知识点列表与筛选
  // ------------------------------------------------------------------ //

  function buildKnowledgePointQuery() {
    var params = {};
    if (state.currentChapterId) {
      params.chapter_id = state.currentChapterId;
    }
    if (state.selectedTags.length) {
      params.tags = state.selectedTags.slice();
    }
    if (state.difficultyMin !== null) {
      params.difficulty_min = state.difficultyMin;
    }
    if (state.difficultyMax !== null) {
      params.difficulty_max = state.difficultyMax;
    }
    return params;
  }

  async function loadKnowledgePointList() {
    if (!dom.kpList) {
      return;
    }
    clearElement(dom.kpList);
    dom.kpList.appendChild(createElement("p", "muted", "加载中…"));
    try {
      var points = await API.get("/knowledge/kps", buildKnowledgePointQuery());
      state.knowledgePoints = Array.isArray(points) ? points : [];
      renderKnowledgePointList();
    } catch (error) {
      state.knowledgePoints = [];
      clearElement(dom.kpList);
      dom.kpList.appendChild(createElement("p", "muted", "加载失败：" + describeError(error)));
      updateKnowledgePointCount();
    }
  }

  function renderKnowledgePointList() {
    if (!dom.kpList) {
      return;
    }
    clearElement(dom.kpList);
    if (!state.knowledgePoints.length) {
      dom.kpList.appendChild(
        createElement("p", "muted", "没有符合筛选条件的知识点，试试重置筛选。")
      );
      updateKnowledgePointCount();
      return;
    }
    state.knowledgePoints.forEach(function (meta) {
      var item = createElement("button", "kp-item");
      item.type = "button";
      item.dataset.kpId = meta.kp_id;
      if (meta.kp_id === state.currentKpId) {
        item.classList.add("is-active");
      }
      item.appendChild(createElement("span", "kp-item-title", meta.title || meta.kp_id));
      if (meta.summary) {
        item.appendChild(createElement("p", "kp-item-summary", meta.summary));
      }
      var metaRow = createElement("div", "kp-item-meta");
      metaRow.appendChild(createElement("span", "chip", "难度 " + meta.difficulty + "/5"));
      metaRow.appendChild(createElement("span", "chip", "重要性 " + meta.importance + "/5"));
      metaRow.appendChild(
        createElement("span", "chip", "约 " + meta.est_minutes + " 分钟")
      );
      metaRow.appendChild(createElement("span", "chip", (meta.quiz_count || 0) + " 题"));
      item.appendChild(metaRow);
      item.addEventListener("click", function () {
        selectKnowledgePoint(meta.kp_id);
      });
      dom.kpList.appendChild(item);
    });
    updateKnowledgePointCount();
  }

  function updateKnowledgePointCount() {
    if (dom.kpCount) {
      dom.kpCount.textContent = state.knowledgePoints.length + " 个";
    }
  }

  function highlightKnowledgePointItem() {
    if (!dom.kpList) {
      return;
    }
    var items = dom.kpList.querySelectorAll(".kp-item");
    Array.prototype.forEach.call(items, function (item) {
      item.classList.toggle("is-active", item.dataset.kpId === state.currentKpId);
    });
  }

  async function loadTagOptions() {
    try {
      var allPoints = await API.get("/knowledge/kps");
      var tagSet = {};
      (Array.isArray(allPoints) ? allPoints : []).forEach(function (meta) {
        (meta.tags || []).forEach(function (tag) {
          tagSet[tag] = true;
        });
      });
      state.tagOptions = Object.keys(tagSet).sort(function (a, b) {
        return a.localeCompare(b, "zh-CN");
      });
      renderTagFilter();
    } catch (error) {
      if (dom.tagFilter) {
        clearElement(dom.tagFilter);
        dom.tagFilter.appendChild(createElement("span", "muted", "标签加载失败"));
      }
    }
  }

  function renderTagFilter() {
    if (!dom.tagFilter) {
      return;
    }
    clearElement(dom.tagFilter);
    if (!state.tagOptions.length) {
      dom.tagFilter.appendChild(createElement("span", "muted", "暂无标签"));
      return;
    }
    state.tagOptions.forEach(function (tag) {
      var chip = createElement("button", "tag-chip", tag);
      chip.type = "button";
      chip.dataset.tag = tag;
      if (state.selectedTags.indexOf(tag) !== -1) {
        chip.classList.add("is-active");
      }
      chip.addEventListener("click", function () {
        toggleTag(tag, chip);
      });
      dom.tagFilter.appendChild(chip);
    });
  }

  function toggleTag(tag, chip) {
    var index = state.selectedTags.indexOf(tag);
    if (index === -1) {
      state.selectedTags.push(tag);
      chip.classList.add("is-active");
    } else {
      state.selectedTags.splice(index, 1);
      chip.classList.remove("is-active");
    }
    loadKnowledgePointList();
  }

  function resetFilters() {
    state.selectedTags = [];
    state.difficultyMin = null;
    state.difficultyMax = null;
    if (dom.difficultyMin) {
      dom.difficultyMin.value = "";
    }
    if (dom.difficultyMax) {
      dom.difficultyMax.value = "";
    }
    renderTagFilter();
    loadKnowledgePointList();
  }

  function bindFilters() {
    var onDifficultyChange = function () {
      state.difficultyMin = dom.difficultyMin.value ? Number(dom.difficultyMin.value) : null;
      state.difficultyMax = dom.difficultyMax.value ? Number(dom.difficultyMax.value) : null;
      if (
        state.difficultyMin !== null &&
        state.difficultyMax !== null &&
        state.difficultyMin > state.difficultyMax
      ) {
        toast("难度下限不能大于上限", "error");
        return;
      }
      loadKnowledgePointList();
    };
    if (dom.difficultyMin) {
      dom.difficultyMin.addEventListener("change", onDifficultyChange);
    }
    if (dom.difficultyMax) {
      dom.difficultyMax.addEventListener("change", onDifficultyChange);
    }
    if (dom.filterReset) {
      dom.filterReset.addEventListener("click", resetFilters);
    }
  }

  // ------------------------------------------------------------------ //
  // 知识点详情
  // ------------------------------------------------------------------ //

  async function selectKnowledgePoint(kpId) {
    state.currentKpId = kpId;
    highlightKnowledgePointItem();
    showReaderStatus("正在加载知识点…");

    var encoded = encodeURIComponent(kpId);
    try {
      var results = await Promise.all([
        API.get("/knowledge/kps/" + encoded),
        API.get("/knowledge/kps/" + encoded + "/mindmap"),
        API.get("/knowledge/kps/" + encoded + "/images"),
        // 题目接口不阻塞正文：失败时退化为空题组
        API.get("/quiz/" + encoded).catch(function () {
          return [];
        })
      ]);
      // 防止快速切换时旧请求覆盖新选择
      if (state.currentKpId !== kpId) {
        return;
      }
      var knowledgePoint = results[0];
      var mindmap = results[1];
      var images = results[2];
      var quizItems = results[3];

      state.currentKp = knowledgePoint;
      state.currentMindmap = mindmap;
      state.quizItems = Array.isArray(quizItems) ? quizItems : [];

      renderArticle(knowledgePoint, mindmap, images);
      renderQuiz(state.quizItems);
      showArticle();
      logBehavior("kp_view", kpId, { chapter_id: knowledgePoint.chapter_id });
      updateUrlKnowledgePointId(kpId);
    } catch (error) {
      if (state.currentKpId !== kpId) {
        return;
      }
      showReaderStatus("加载知识点失败：" + describeError(error));
      toast("加载知识点失败：" + describeError(error), "error");
    }
  }

  function renderArticle(knowledgePoint, mindmap, images) {
    dom.kpTitle.textContent = knowledgePoint.title || knowledgePoint.kp_id;
    dom.kpSummary.textContent = knowledgePoint.summary || "";

    // 元信息 chips
    clearElement(dom.kpMeta);
    appendMetaChip(
      dom.kpMeta,
      "难度 " + knowledgePoint.difficulty + "/5",
      "（" + (LEVEL_TEXT[knowledgePoint.difficulty] || "") + "）"
    );
    appendMetaChip(
      dom.kpMeta,
      "重要性 " + knowledgePoint.importance + "/5",
      "（" + (LEVEL_TEXT[knowledgePoint.importance] || "") + "）"
    );
    dom.kpMeta.appendChild(
      createElement("span", "chip", "预计 " + knowledgePoint.est_minutes + " 分钟")
    );
    dom.kpMeta.appendChild(
      createElement("span", "chip", (knowledgePoint.quiz || []).length + " 题练习")
    );

    // 标签
    clearElement(dom.kpTags);
    (knowledgePoint.tags || []).forEach(function (tag) {
      dom.kpTags.appendChild(createElement("span", "badge badge-soft", tag));
    });
    (knowledgePoint.theory_tags || []).forEach(function (tag) {
      dom.kpTags.appendChild(createElement("span", "badge badge-primary", tag));
    });

    // 正文内容块
    clearElement(dom.kpContent);
    dom.kpContent.appendChild(renderContentBlocks(knowledgePoint.content_blocks));

    // 配图画廊
    clearElement(dom.kpGallery);
    dom.kpGallery.appendChild(renderGallery(images));

    // 思维导图
    renderMindmap(mindmap);
  }

  function appendMetaChip(container, mainText, note) {
    var chip = createElement("span", "chip", mainText + (note || ""));
    container.appendChild(chip);
  }

  // ------------------------------------------------------------------ //
  // content_blocks 渲染（全类型）
  // ------------------------------------------------------------------ //

  function renderContentBlocks(blocks) {
    var fragment = global.document.createDocumentFragment();
    if (!Array.isArray(blocks)) {
      return fragment;
    }
    blocks.forEach(function (block) {
      var element = renderContentBlock(block);
      if (element) {
        fragment.appendChild(element);
      }
    });
    return fragment;
  }

  function renderContentBlock(block) {
    if (!block || typeof block !== "object") {
      return null;
    }
    switch (block.type) {
      case "paragraph":
        return renderParagraphBlock(block);
      case "list":
        return renderListBlock(block);
      case "image":
        return renderImageFigure(block);
      case "table":
        return renderTableBlock(block);
      case "callout":
        return renderCalloutBlock(block);
      case "formula":
        return renderFormulaBlock(block);
      default:
        return renderUnknownBlock(block);
    }
  }

  function renderParagraphBlock(block) {
    return createElement("p", "cb-paragraph", block.text || "");
  }

  function renderListBlock(block) {
    var list = createElement(block.ordered ? "ol" : "ul", "cb-list");
    (block.items || []).forEach(function (item) {
      list.appendChild(createElement("li", null, item));
    });
    return list;
  }

  function renderTableBlock(block) {
    var figure = createElement("figure", "cb-table");
    if (block.caption) {
      figure.appendChild(createElement("figcaption", "cb-table-caption", block.caption));
    }
    var headers = Array.isArray(block.headers) ? block.headers : [];
    var rows = Array.isArray(block.rows) ? block.rows : [];
    var columnCount = headers.length;
    rows.forEach(function (row) {
      columnCount = Math.max(columnCount, Array.isArray(row) ? row.length : 0);
    });

    var table = createElement("table", "cb-table-el");
    if (headers.length) {
      var thead = createElement("thead");
      var headRow = createElement("tr");
      for (var i = 0; i < columnCount; i++) {
        headRow.appendChild(createElement("th", null, headers[i] === undefined ? "" : headers[i]));
      }
      thead.appendChild(headRow);
      table.appendChild(thead);
    }
    if (rows.length) {
      var tbody = createElement("tbody");
      rows.forEach(function (row) {
        var tr = createElement("tr");
        var cells = Array.isArray(row) ? row : [row];
        for (var c = 0; c < columnCount; c++) {
          tr.appendChild(createElement("td", null, cells[c] === undefined ? "" : cells[c]));
        }
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
    }

    var scroll = createElement("div", "table-scroll");
    scroll.appendChild(table);
    figure.appendChild(scroll);
    return figure;
  }

  function renderCalloutBlock(block) {
    var variant = CALLOUT_META[block.variant] ? block.variant : "note";
    var meta = CALLOUT_META[variant];
    var box = createElement("div", "cb-callout cb-callout-" + variant);
    var icon = createElement("span", "cb-callout-icon");
    icon.textContent = meta.icon;
    icon.setAttribute("title", meta.label);
    icon.setAttribute("aria-label", meta.label);
    box.appendChild(icon);
    box.appendChild(createElement("p", "cb-callout-body", block.text || ""));
    return box;
  }

  function renderFormulaBlock(block) {
    var box = createElement("div", "cb-formula");
    // LaTeX 以等宽文本兜底展示，不依赖 MathJax/KaTeX 等外部 CDN
    box.appendChild(createElement("code", "cb-formula-code", block.latex || ""));
    if (block.caption) {
      box.appendChild(createElement("p", "cb-formula-caption", block.caption));
    }
    return box;
  }

  function renderUnknownBlock(block) {
    return createElement(
      "div",
      "cb-unknown",
      "[暂不支持的内容块类型：" + (block.type || "未知") + "]"
    );
  }

  // ------------------------------------------------------------------ //
  // 配图（含加载失败占位）
  // ------------------------------------------------------------------ //

  /**
   * 渲染单张图片。加载失败时隐藏 img 并显示占位元素与 caption（AC-R7-2）。
   *
   * @param {{src:string, caption?:string, alt?:string}} ref
   * @returns {HTMLElement} figure 元素
   */
  function renderImageFigure(ref) {
    var figure = createElement("figure", "cb-image");
    var media = createElement("div", "figure-media");

    var fallback = createElement("div", "img-fallback");
    fallback.hidden = true;
    fallback.appendChild(createElement("span", "img-fallback-icon", "🖼️"));
    fallback.appendChild(
      createElement(
        "p",
        "img-fallback-caption",
        "图片暂不可用" + (ref.caption ? "：" + ref.caption : "")
      )
    );

    var image = global.document.createElement("img");
    image.className = "figure-img";
    image.alt = ref.alt || ref.caption || "";
    image.loading = "lazy";
    image.decoding = "async";

    var showFallback = function () {
      image.hidden = true;
      fallback.hidden = false;
      figure.classList.add("is-fallback");
    };

    image.addEventListener("error", showFallback);
    image.addEventListener("load", function () {
      image.hidden = false;
      fallback.hidden = true;
      figure.classList.remove("is-fallback");
    });

    if (ref.src) {
      image.src = ref.src;
    } else {
      showFallback();
    }

    media.appendChild(image);
    media.appendChild(fallback);
    figure.appendChild(media);
    if (ref.caption) {
      figure.appendChild(createElement("figcaption", "figure-caption", ref.caption));
    }
    return figure;
  }

  function renderGallery(images) {
    var fragment = global.document.createDocumentFragment();
    if (!Array.isArray(images) || !images.length) {
      fragment.appendChild(createElement("p", "muted", "该知识点暂无配图。"));
      return fragment;
    }
    images.forEach(function (ref) {
      fragment.appendChild(renderImageFigure(ref));
    });
    return fragment;
  }

  // ------------------------------------------------------------------ //
  // 思维导图（自绘 SVG）
  // ------------------------------------------------------------------ //

  function renderMindmap(mindmap) {
    if (!dom.mindmapCanvas) {
      return;
    }
    if (!Mindmap || typeof Mindmap.render !== "function") {
      clearElement(dom.mindmapCanvas);
      dom.mindmapCanvas.appendChild(
        createElement("p", "muted", "思维导图渲染器未加载（mindmap.js）。")
      );
      return;
    }
    if (!mindmap || !mindmap.label) {
      clearElement(dom.mindmapCanvas);
      dom.mindmapCanvas.appendChild(createElement("p", "muted", "该知识点暂无思维导图。"));
      return;
    }
    try {
      Mindmap.render(dom.mindmapCanvas, mindmap);
    } catch (error) {
      clearElement(dom.mindmapCanvas);
      dom.mindmapCanvas.appendChild(
        createElement("p", "muted", "思维导图渲染失败：" + describeError(error))
      );
    }
  }

  function bindMindmapActions() {
    if (dom.mindmapRefresh) {
      dom.mindmapRefresh.addEventListener("click", function () {
        renderMindmap(state.currentMindmap);
      });
    }
    if (dom.mindmapFit && dom.mindmapCanvas) {
      dom.mindmapFit.addEventListener("click", function () {
        dom.mindmapCanvas.classList.toggle("is-fit");
        dom.mindmapFit.textContent = dom.mindmapCanvas.classList.contains("is-fit")
          ? "原始尺寸"
          : "适应宽度";
      });
    }
  }

  // ------------------------------------------------------------------ //
  // 答题交互
  // ------------------------------------------------------------------ //

  function renderQuiz(items) {
    if (!dom.quizList) {
      return;
    }
    clearElement(dom.quizList);

    updateQuizProgress();
    if (!items || !items.length) {
      dom.quizList.appendChild(createElement("p", "muted", "该知识点暂无练习题。"));
      return;
    }
    items.forEach(function (item, index) {
      dom.quizList.appendChild(renderQuizCard(item, index));
    });
  }

  function renderQuizCard(item, index) {
    var card = createElement("article", "quiz-card");
    card.dataset.qid = item.qid;

    var head = createElement("div", "quiz-head");
    head.appendChild(createElement("span", "quiz-index", "第 " + (index + 1) + " 题"));
    head.appendChild(
      createElement("span", "badge badge-primary", QUIZ_TYPE_LABELS[item.type] || item.type)
    );
    head.appendChild(createElement("span", "chip", "难度 " + item.difficulty + "/5"));
    head.appendChild(createElement("span", "chip", "重要性 " + item.importance + "/5"));
    card.appendChild(head);

    card.appendChild(createElement("p", "quiz-question", item.question || ""));

    // 选项 / 作答区
    if (item.type === "single_choice" || item.type === "true_false") {
      card.appendChild(renderChoiceOptions(item, "radio"));
    } else if (item.type === "multi_choice") {
      card.appendChild(renderChoiceOptions(item, "checkbox"));
    } else {
      var textarea = createElement("textarea", "input quiz-answer-input");
      textarea.placeholder = "请用文字作答，力求覆盖关键概念…";
      textarea.setAttribute("aria-label", "简答题作答区");
      card.appendChild(textarea);
    }

    // 提交按钮
    var foot = createElement("div", "quiz-foot");
    var submit = createElement("button", "btn btn-sm btn-primary", "提交作答");
    submit.type = "button";
    submit.addEventListener("click", function () {
      submitQuizAttempt(card, item, submit);
    });
    foot.appendChild(submit);
    card.appendChild(foot);

    return card;
  }

  function renderChoiceOptions(item, inputType) {
    var wrapper = createElement("div", "quiz-options");
    var name = "quiz-" + state.currentKpId + "-" + item.qid;
    var options = Array.isArray(item.options) ? item.options : [];
    options.forEach(function (optionText, optionIndex) {
      var label = createElement("label", "quiz-option");
      var input = global.document.createElement("input");
      input.type = inputType;
      input.name = name;
      input.value = optionText;
      label.appendChild(input);
      label.appendChild(
        createElement("span", null, optionText)
      );
      // 选项可点击整行（label 天然支持），保留无障碍语义
      input.setAttribute("aria-label", optionText);
      wrapper.appendChild(label);
    });
    if (!options.length) {
      wrapper.appendChild(createElement("p", "muted", "该题缺少选项数据。"));
    }
    return wrapper;
  }

  /** 收集当前题卡的用户作答；未作答返回 null。 */
  function collectQuizAnswer(card, item) {
    if (item.type === "single_choice" || item.type === "true_false") {
      var checked = card.querySelector('input[type="radio"]:checked');
      return checked ? checked.value : null;
    }
    if (item.type === "multi_choice") {
      var checkedBoxes = card.querySelectorAll('input[type="checkbox"]:checked');
      if (!checkedBoxes.length) {
        return null;
      }
      var values = [];
      Array.prototype.forEach.call(checkedBoxes, function (box) {
        values.push(box.value);
      });
      return values;
    }
    var textarea = card.querySelector("textarea");
    var text = textarea ? textarea.value.trim() : "";
    return text ? text : null;
  }

  async function submitQuizAttempt(card, item, submitButton) {
    var answer = collectQuizAnswer(card, item);
    if (answer === null) {
      toast("请先完成作答再提交", "error");
      return;
    }
    submitButton.disabled = true;
    try {
      var response = await API.post(
        "/quiz/" + encodeURIComponent(state.currentKpId) + "/attempt",
        { qid: item.qid, user_answer: answer }
      );
      showGradeResult(card, response);

      updateQuizProgress();
      if (response && response.grade && response.grade.is_correct) {
        toast("回答正确 🎉", "success");
      } else {
        toast("已提交，看看解析吧", "info");
      }
    } catch (error) {
      toast("提交失败：" + describeError(error), "error");
    }
    submitButton.disabled = false;
  }

  function showGradeResult(card, response) {
    var grade = (response && response.grade) || {};
    var reviewState = response ? response.review_state : null;

    var existing = card.querySelector(".quiz-result");
    if (existing) {
      existing.parentNode.removeChild(existing);
    }

    card.classList.toggle("is-correct", !!grade.is_correct);
    card.classList.toggle("is-incorrect", !grade.is_correct);

    var box = createElement(
      "div",
      "quiz-result " + (grade.is_correct ? "is-correct" : "is-incorrect")
    );

    var head = createElement("div", "result-head");
    head.appendChild(
      createElement(
        "span",
        "badge " + (grade.is_correct ? "chip-ok" : "chip-removed"),
        grade.is_correct ? "回答正确" : "需要加强"
      )
    );
    head.appendChild(
      createElement("span", "result-score", (grade.score === undefined ? 0 : grade.score) + " 分")
    );
    if (grade.graded_by) {
      head.appendChild(
        createElement(
          "span",
          "chip",
          "判分方式：" + (GRADED_BY_LABELS[grade.graded_by] || grade.graded_by)
        )
      );
    }
    box.appendChild(head);

    appendResultBlock(box, "参考答案", formatAnswer(grade.expected_answer), true);

    if (Array.isArray(grade.keyword_hits) && grade.keyword_hits.length) {
      var hitsBlock = createElement("div", "result-block");
      hitsBlock.appendChild(createElement("span", "result-label", "命中关键词"));
      var hits = createElement("div", "kw-hits");
      grade.keyword_hits.forEach(function (keyword) {
        hits.appendChild(createElement("span", "chip chip-ok", keyword));
      });
      hitsBlock.appendChild(hits);
      box.appendChild(hitsBlock);
    }

    if (grade.feedback) {
      appendResultBlock(box, "反馈", grade.feedback, true);
    } else if (grade.explanation) {
      appendResultBlock(box, "解析", grade.explanation, true);
    }

    if (reviewState) {
      var strip = createElement("div", "review-state-strip");
      strip.appendChild(
        createElement("span", "chip", "记忆层级 " + (reviewState.level || 0))
      );
      strip.appendChild(
        createElement(
          "span",
          "chip",
          "掌握度 " + Math.round((reviewState.mastery || 0) * 100) + "%"
        )
      );
      strip.appendChild(
        createElement("span", "chip", "下次复习 " + formatDateTime(reviewState.due_at))
      );
      box.appendChild(strip);
    } else {
      box.appendChild(
        createElement("p", "review-hint", "该知识点尚未加入复习，点击上方「加入复习」后可跟踪掌握度。")
      );
    }

    card.appendChild(box);
  }

  function appendResultBlock(container, label, value, preserveWhitespace) {
    var block = createElement("div", "result-block");
    block.appendChild(createElement("span", "result-label", label));
    var paragraph = createElement("p", "result-value", value || "-");
    if (preserveWhitespace) {
      paragraph.style.whiteSpace = "pre-wrap";
    }
    block.appendChild(paragraph);
    container.appendChild(block);
  }

  function updateQuizProgress() {
    if (!dom.quizProgress) {
      return;
    }
    var answered = dom.quizList
      ? dom.quizList.querySelectorAll(".quiz-result").length
      : 0;
    dom.quizProgress.textContent = answered + " / " + state.quizItems.length;
  }

  // ------------------------------------------------------------------ //
  // 加入复习 / 行为日志 / URL 同步
  // ------------------------------------------------------------------ //

  async function addCurrentKnowledgePointToReview() {
    if (!state.currentKp) {
      toast("请先选择一个知识点", "error");
      return;
    }
    if (dom.addReviewBtn) {
      dom.addReviewBtn.disabled = true;
    }
    try {
      await API.post("/review/items", {
        kp_id: state.currentKp.kp_id,
        chapter_id: state.currentKp.chapter_id
      });
      toast("已加入复习计划", "success");
    } catch (error) {
      toast("加入复习失败：" + describeError(error), "error");
    }
    if (dom.addReviewBtn) {
      dom.addReviewBtn.disabled = false;
    }
  }

  /** 记录前端行为日志（失败静默，不影响主流程）。 */
  function logBehavior(actionType, kpId, payload) {
    API.post("/log/behavior", {
      action_type: actionType,
      kp_id: kpId || null,
      payload: payload || {}
    }).catch(function () {
      /* 行为日志失败可忽略 */
    });
  }

  function updateUrlKnowledgePointId(kpId) {
    try {
      var url = global.location.pathname + "?kp_id=" + encodeURIComponent(kpId);
      global.history.replaceState(null, "", url);
    } catch (error) {
      /* file:// 等环境可能不支持，忽略 */
    }
  }

  // ------------------------------------------------------------------ //
  // 启动
  // ------------------------------------------------------------------ //

  async function openKnowledgePointById(kpId) {
    try {
      var knowledgePoint = await API.get("/knowledge/kps/" + encodeURIComponent(kpId));
      state.currentChapterId = knowledgePoint.chapter_id;
      renderChapterList();
      await loadKnowledgePointList();
      await selectKnowledgePoint(kpId);
    } catch (error) {
      toast("无法打开知识点：" + describeError(error), "error");
      var first = state.chapters[0];
      if (first) {
        await selectChapter(first.chapter_id);
      }
    }
  }

  async function initialize() {
    cacheDom();
    setupThemeToggle();
    renderUserIdentifier();
    bindFilters();
    bindMindmapActions();

    if (dom.addReviewBtn) {
      dom.addReviewBtn.addEventListener("click", addCurrentKnowledgePointToReview);
    }

    if (!API) {
      showBackendBanner();
      showReaderStatus("共享 API 工具（api.js）未加载，页面功能不可用。");
      return;
    }

    try {
      await loadChapters();
      hideBackendBanner();
    } catch (error) {
      showBackendBanner();
      renderChapterList();
      if (dom.chapterList) {
        clearElement(dom.chapterList);
        dom.chapterList.appendChild(
          createElement("p", "muted", "加载章节失败：" + describeError(error))
        );
      }
      showReaderStatus("无法连接后端服务，请确认已运行 python run.py。");
      return;
    }

    renderChapterList();

    var params = new global.URLSearchParams(global.location.search);
    var deepLinkKpId = params.get("kp_id");
    if (deepLinkKpId) {
      await openKnowledgePointById(deepLinkKpId);
    } else {
      var firstChapter = state.chapters[0];
      if (firstChapter) {
        await selectChapter(firstChapter.chapter_id);
      } else {
        showReaderStatus("内容库中暂无章节数据。");
      }
    }

    // 标签选项按需加载，不阻塞首屏
    loadTagOptions();
  }

  onDomReady(initialize);
})(window);