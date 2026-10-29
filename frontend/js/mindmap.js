/**
 * mindmap.js — 思维导图自绘渲染器（T10 独占写入）。
 *
 * 职责：依据后端返回的 MindmapNode 递归结构 `{id, label, note, children}`，
 * 使用原生 SVG（document.createElementNS）在前端自绘「横向树 / 放射状」导图：
 *   - 递归计算节点尺寸（按文本宽度估算）与坐标（叶子按遍历顺序纵向排布，
 *     父节点取其首尾子节点纵坐标的中点）；
 *   - 绘制父子连线（三次贝塞尔曲线）、节点框、标签与备注；
 *   - 完全离线可用，**不依赖任何外部 CDN / 第三方库**（满足 R7.4 / AC-R7-3）。
 *
 * 导出（挂载到 window.Mindmap）：
 *   Mindmap.render(container, root, options?) -> {svg, width, height} | null
 *   Mindmap.estimateTextWidth(text, fontSize)  -> number
 *   Mindmap.version                            -> string
 *
 * 说明：
 *   - 入参 root 不会被修改（内部先深拷贝再布局）。
 *   - 所有文本均通过 textContent 写入，避免 HTML 注入。
 *   - 颜色/字号通过 CSS 类控制（见 knowledge.css），可随明暗主题自适应。
 */
(function (global) {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  var VERSION = "1.0.0";

  // 默认布局参数（可通过 options 覆盖）
  var DEFAULTS = {
    fontSize: 13,
    noteFontSize: 11,
    nodeHeight: 34,
    nodeHeightWithNote: 48,
    nodeMinWidth: 96,
    nodeMaxWidth: 320,
    paddingX: 14,
    gapX: 56,
    gapY: 12,
    marginX: 20,
    marginY: 20,
    radius: 9,
    maxDepthLayers: 6
  };

  /** 创建 SVG 命名空间元素并批量设置属性。 */
  function createSvgElement(tag, attributes) {
    var element = global.document.createElementNS(SVG_NS, tag);
    if (attributes) {
      Object.keys(attributes).forEach(function (key) {
        var value = attributes[key];
        if (value !== undefined && value !== null) {
          element.setAttribute(key, String(value));
        }
      });
    }
    return element;
  }

  /**
   * 估算文本像素宽度：CJK/全角字符按 1em，ASCII 按 0.56em，其它按 0.85em。
   * 不依赖 canvas.measureText，保证离线 / 无渲染上下文时依然可用。
   *
   * @param {string} text
   * @param {number} fontSize
   * @returns {number}
   */
  function estimateTextWidth(text, fontSize) {
    var str = text === undefined || text === null ? "" : String(text);
    var width = 0;
    for (var i = 0; i < str.length; i++) {
      var code = str.charCodeAt(i);
      var ratio;
      if (code >= 0x2e80 && code <= 0xffef) {
        ratio = 1.0; // CJK 汉字 / 全角标点
      } else if (code < 0x80) {
        ratio = 0.56; // ASCII
      } else {
        ratio = 0.85; // 其它宽字符
      }
      width += ratio * fontSize;
      if (code >= 0xd800 && code <= 0xdbff && i + 1 < str.length) {
        i++; // 跳过代理对低半区（emoji 等）
      }
    }
    return width;
  }

  /** 将文本按最大像素宽度截断，超出部分以省略号替代。 */
  function truncateToWidth(text, maxWidth, fontSize) {
    var str = text === undefined || text === null ? "" : String(text);
    if (estimateTextWidth(str, fontSize) <= maxWidth) {
      return str;
    }
    var ellipsis = "…";
    var ellipsisWidth = estimateTextWidth(ellipsis, fontSize);
    var kept = "";
    for (var i = 0; i < str.length; i++) {
      var candidate = kept + str.charAt(i);
      if (estimateTextWidth(candidate, fontSize) + ellipsisWidth > maxWidth) {
        break;
      }
      kept = candidate;
    }
    return kept + ellipsis;
  }

  /** 深拷贝输入节点，避免污染调用方数据。 */
  function cloneNode(source) {
    var src = source && typeof source === "object" ? source : {};
    var children = Array.isArray(src.children) ? src.children : [];
    return {
      id: src.id === undefined || src.id === null ? "" : String(src.id),
      label: src.label === undefined || src.label === null ? "" : String(src.label),
      note: src.note ? String(src.note) : "",
      children: children.map(cloneNode)
    };
  }

  /** 合并默认参数与调用方覆盖参数。 */
  function assignOptions(base, override) {
    var result = {};
    Object.keys(base).forEach(function (key) {
      result[key] = base[key];
    });
    if (override) {
      Object.keys(override).forEach(function (key) {
        if (override[key] !== undefined && override[key] !== null) {
          result[key] = override[key];
        }
      });
    }
    return result;
  }

  /**
   * 计算整棵树的布局，为每个节点写入 width/height/x/y/depth。
   *
   * 纵向：叶子节点按深度优先遍历顺序自上而下排布，父节点取其首尾子节点的中点；
   * 横向：子节点 x = 父节点右边缘 + gapX，逐层向右展开。
   *
   * 最后按实际包围盒做纵向归一化，避免父节点高于首个叶子时被裁切。
   */
  function computeLayout(root, options) {
    var cursorY = 0;

    function measure(node) {
      var labelWidth = estimateTextWidth(node.label, options.fontSize);
      var noteWidth = node.note
        ? estimateTextWidth(node.note, options.noteFontSize)
        : 0;
      var contentWidth = Math.max(labelWidth, noteWidth);
      node.width = Math.min(
        options.nodeMaxWidth,
        Math.max(options.nodeMinWidth, Math.ceil(contentWidth) + options.paddingX * 2)
      );
      node.height = node.note ? options.nodeHeightWithNote : options.nodeHeight;
      node.children.forEach(measure);
    }

    function assignVertical(node) {
      if (node.children.length === 0) {
        node.y = cursorY + node.height / 2;
        cursorY += node.height + options.gapY;
        return;
      }
      node.children.forEach(assignVertical);
      node.y =
        (node.children[0].y + node.children[node.children.length - 1].y) / 2;
    }

    function assignHorizontal(node, x, depth) {
      node.x = x;
      node.depth = Math.min(depth, options.maxDepthLayers - 1);
      var childX = x + node.width + options.gapX;
      node.children.forEach(function (child) {
        assignHorizontal(child, childX, depth + 1);
      });
    }

    measure(root);
    assignVertical(root);
    assignHorizontal(root, options.marginX, 0);

    var allNodes = [];
    var minY = Infinity;
    var maxY = -Infinity;
    var maxRight = 0;
    (function collect(node) {
      allNodes.push(node);
      minY = Math.min(minY, node.y - node.height / 2);
      maxY = Math.max(maxY, node.y + node.height / 2);
      maxRight = Math.max(maxRight, node.x + node.width);
      node.children.forEach(collect);
    })(root);

    var offsetY = options.marginY - minY;
    allNodes.forEach(function (node) {
      node.y += offsetY;
    });

    return {
      nodes: allNodes,
      width: Math.ceil(maxRight + options.marginX),
      height: Math.ceil(maxY - minY + options.marginY * 2)
    };
  }

  /** 构造父子连线（三次贝塞尔曲线）。 */
  function buildLinkElement(parent, child) {
    var x1 = parent.x + parent.width;
    var y1 = parent.y;
    var x2 = child.x;
    var y2 = child.y;
    var midX = (x1 + x2) / 2;
    return createSvgElement("path", {
      "class": "mm-link",
      "data-depth": child.depth,
      d:
        "M " + x1 + " " + y1 +
        " C " + midX + " " + y1 + ", " + midX + " " + y2 + ", " + x2 + " " + y2,
      fill: "none"
    });
  }

  /** 构造单个节点元素（节点框 + 标签 + 可选备注 + 悬停提示）。 */
  function buildNodeElement(node, options) {
    var group = createSvgElement("g", {
      "class": "mm-node",
      "data-depth": node.depth,
      transform: "translate(" + node.x + "," + (node.y - node.height / 2) + ")"
    });

    group.appendChild(
      createSvgElement("rect", {
        "class": "mm-node-box",
        x: 0,
        y: 0,
        width: node.width,
        height: node.height,
        rx: options.radius,
        ry: options.radius
      })
    );

    var maxTextWidth = node.width - options.paddingX * 2;
    var hasNote = !!node.note;

    var label = createSvgElement("text", {
      "class": "mm-label",
      x: options.paddingX,
      y: hasNote ? node.height * 0.36 : node.height / 2,
      "dominant-baseline": "middle"
    });
    label.textContent = truncateToWidth(node.label, maxTextWidth, options.fontSize);
    group.appendChild(label);

    if (hasNote) {
      var note = createSvgElement("text", {
        "class": "mm-note",
        x: options.paddingX,
        y: node.height * 0.72,
        "dominant-baseline": "middle"
      });
      note.textContent = truncateToWidth(
        node.note,
        maxTextWidth,
        options.noteFontSize
      );
      group.appendChild(note);
    }

    // 原生 <title> 悬停提示：完整展示 label 与 note
    var title = global.document.createElementNS(SVG_NS, "title");
    title.textContent = node.note ? node.label + " — " + node.note : node.label;
    group.appendChild(title);

    return group;
  }

  /** 清空容器内的既有内容（含上次渲染的导图）。 */
  function clearContainer(container) {
    while (container.firstChild) {
      container.removeChild(container.firstChild);
    }
  }

  /**
   * 渲染思维导图到指定容器。
   *
   * @param {Element|string} container 容器元素或其 CSS 选择器
   * @param {{id?:string,label?:string,note?:string,children?:Array}} root 根节点
   * @param {Object} [options] 可选的布局参数覆盖
   * @returns {{svg: SVGElement, width: number, height: number}|null}
   *          无有效数据时返回 null 并渲染占位提示。
   */
  function render(container, root, options) {
    var target =
      typeof container === "string"
        ? global.document.querySelector(container)
        : container;
    if (!target) {
      throw new Error("Mindmap.render：未找到容器元素");
    }
    clearContainer(target);

    if (!root || typeof root !== "object" || !root.label) {
      var empty = global.document.createElement("p");
      empty.className = "muted";
      empty.textContent = "暂无思维导图数据";
      target.appendChild(empty);
      return null;
    }

    var opts = assignOptions(DEFAULTS, options);
    var tree = cloneNode(root);
    var layout = computeLayout(tree, opts);

    var svg = createSvgElement("svg", {
      "class": "mindmap-svg",
      xmlns: SVG_NS,
      viewBox: "0 0 " + layout.width + " " + layout.height,
      width: layout.width,
      height: layout.height,
      role: "img",
      "aria-label": "思维导图：" + tree.label
    });

    var linkLayer = createSvgElement("g", { "class": "mm-links" });
    var nodeLayer = createSvgElement("g", { "class": "mm-nodes" });

    // 先画连线（位于底层），再画节点（覆盖在线之上）
    layout.nodes.forEach(function (node) {
      node.children.forEach(function (child) {
        linkLayer.appendChild(buildLinkElement(node, child));
      });
    });
    layout.nodes.forEach(function (node) {
      nodeLayer.appendChild(buildNodeElement(node, opts));
    });

    svg.appendChild(linkLayer);
    svg.appendChild(nodeLayer);
    target.appendChild(svg);

    return { svg: svg, width: layout.width, height: layout.height };
  }

  global.Mindmap = {
    version: VERSION,
    render: render,
    estimateTextWidth: estimateTextWidth
  };
})(window);