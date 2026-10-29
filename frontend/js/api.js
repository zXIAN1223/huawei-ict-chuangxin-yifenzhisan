/**
 * api.js — 前端共享 API 工具（契约 C6-UI@1，plan 2.2.6）。
 *
 * 唯一写入者：T9。所有前端页面（index/knowledge/chat/profile）共用本文件。
 *
 * 导出：挂载到 `window.API`，形状严格如下（T10/T11/T12 依赖）：
 *
 *   const API = {
 *     base: "/api",
 *     userId(): string,                              // 读/生成 localStorage['ala_user_id']（UUID4）
 *     async get(path, params?),                      // 带 X-User-Id 头
 *     async post(path, body?),
 *     async put(path, body?),
 *     async del(path),
 *     async stream(path, body, {onMeta,onDelta,onDone,onError})  // fetch + ReadableStream 解析 SSE
 *   };
 *
 * 约定：
 *  - 所有请求携带请求头 `X-User-Id: API.userId()`。
 *  - 非 2xx 响应：抛出 `{code, message, detail?, status}` 对象（detail 仅在响应体含 detail 时出现）。
 *  - `stream` 不抛错，异常经 `onError` 回调上抛；未提供 `onError` 时按普通请求抛错。
 *  - 不使用 EventSource（聊天需 POST + 自定义 body），改用 fetch + ReadableStream 手工解析 SSE。
 */
(function (global) {
  "use strict";

  var BASE = "/api";
  var USER_ID_KEY = "ala_user_id";

  // localStorage 在隐私模式/禁用场景可能抛错，使用内存兜底保证 userId() 可用。
  var memoryStore = {};
  function storeGet(key) {
    try {
      return global.localStorage.getItem(key);
    } catch (err) {
      return Object.prototype.hasOwnProperty.call(memoryStore, key) ? memoryStore[key] : null;
    }
  }
  function storeSet(key, value) {
    try {
      global.localStorage.setItem(key, value);
    } catch (err) {
      memoryStore[key] = value;
    }
  }

  /**
   * 生成 UUID4 字符串。优先使用 crypto.randomUUID，降级到 getRandomValues / Math.random。
   * @returns {string}
   */
  function generateUuid4() {
    if (global.crypto && typeof global.crypto.randomUUID === "function") {
      try {
        return global.crypto.randomUUID();
      } catch (err) {
        /* fallthrough：部分旧实现会抛错，改用字节生成 */
      }
    }
    var bytes = new Uint8Array(16);
    if (global.crypto && typeof global.crypto.getRandomValues === "function") {
      global.crypto.getRandomValues(bytes);
    } else {
      for (var i = 0; i < 16; i++) {
        bytes[i] = Math.floor(Math.random() * 256);
      }
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
    bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10
    var hex = [];
    for (var j = 0; j < 16; j++) {
      hex.push(bytes[j].toString(16).padStart(2, "0"));
    }
    return (
      hex.slice(0, 4).join("") + "-" +
      hex.slice(4, 6).join("") + "-" +
      hex.slice(6, 8).join("") + "-" +
      hex.slice(8, 10).join("") + "-" +
      hex.slice(10, 16).join("")
    );
  }

  /**
   * 读取本地用户标识；不存在时生成 UUID4 并持久化（首次访问即创建）。
   * @returns {string}
   */
  function userId() {
    var existing = storeGet(USER_ID_KEY);
    if (typeof existing === "string" && existing.trim()) {
      return existing;
    }
    var generated = generateUuid4();
    storeSet(USER_ID_KEY, generated);
    return generated;
  }

  /**
   * 将参数对象编码为查询串；null/undefined 值跳过；数组按重复键展开。
   * @param {Object<string, (string|number|boolean|Array)>} [params]
   * @returns {string} 形如 "?a=1&b=2"，无参数时返回空串。
   */
  function buildQuery(params) {
    if (!params) {
      return "";
    }
    var parts = [];
    Object.keys(params).forEach(function (key) {
      var value = params[key];
      if (value === undefined || value === null) {
        return;
      }
      if (Array.isArray(value)) {
        value.forEach(function (item) {
          if (item !== undefined && item !== null) {
            parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(item));
          }
        });
      } else {
        parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(value));
      }
    });
    return parts.length ? "?" + parts.join("&") : "";
  }

  /**
   * 组装统一错误体。
   * @param {number} status
   * @param {string} statusText
   * @param {Object|null} payload
   * @returns {{code: string, message: string, status: number, detail?: object}}
   */
  function normalizeError(status, statusText, payload) {
    var error = {
      code: (payload && payload.code) || "HTTP_" + status,
      message: (payload && payload.message) || statusText || "请求失败",
      status: status,
    };
    if (payload && payload.detail !== undefined) {
      error.detail = payload.detail;
    }
    return error;
  }

  /**
   * 安全解析响应体：204/空体返回 null，非 JSON 时返回 null 而不抛错。
   * @param {Response} response
   * @returns {Promise<*>}
   */
  async function parseBody(response) {
    if (response.status === 204) {
      return null;
    }
    var text = await response.text();
    if (!text) {
      return null;
    }
    try {
      return JSON.parse(text);
    } catch (err) {
      return null;
    }
  }

  /**
   * 通用请求。
   * @param {string} method
   * @param {string} path
   * @param {*} [body]
   * @returns {Promise<*>}
   */
  async function request(method, path, body) {
    var headers = { "X-User-Id": userId(), Accept: "application/json" };
    var options = { method: method, headers: headers };
    if (body !== undefined && body !== null) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }

    var response;
    try {
      response = await global.fetch(BASE + path, options);
    } catch (networkError) {
      throw {
        code: "NETWORK_ERROR",
        message: (networkError && networkError.message) || "网络请求失败",
      };
    }

    if (!response.ok) {
      var payload = await parseBody(response);
      throw normalizeError(response.status, response.statusText, payload);
    }
    return await parseBody(response);
  }

  /** GET 请求，支持查询参数对象。 */
  function get(path, params) {
    return request("GET", path + buildQuery(params));
  }
  /** POST 请求，body 会被 JSON 序列化。 */
  function post(path, body) {
    return request("POST", path, body);
  }
  /** PUT 请求，body 会被 JSON 序列化。 */
  function put(path, body) {
    return request("PUT", path, body);
  }
  /** DELETE 请求。 */
  function del(path) {
    return request("DELETE", path);
  }

  /**
   * 解析单个 SSE 帧（`event:` / `data:` 行，空行分隔）。
   * @param {string} raw 单帧文本（不含结尾空行）
   * @param {{onMeta?:Function,onDelta?:Function,onDone?:Function,onError?:Function}} handlers
   */
  function dispatchSseFrame(raw, handlers) {
    if (!raw) {
      return;
    }
    var eventName = "message";
    var dataLines = [];
    var lines = raw.split("\n");
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (!line || line.charAt(0) === ":") {
        continue; // 空行或注释行
      }
      if (line.indexOf("event:") === 0) {
        eventName = line.slice(6).trim();
      } else if (line.indexOf("data:") === 0) {
        var value = line.slice(5);
        if (value.charAt(0) === " ") {
          value = value.slice(1);
        }
        dataLines.push(value);
      }
    }

    var dataText = dataLines.join("\n");
    var data = null;
    if (dataText) {
      try {
        data = JSON.parse(dataText);
      } catch (err) {
        data = { raw: dataText };
      }
    }

    if (eventName === "meta" && handlers.onMeta) {
      handlers.onMeta(data);
    } else if (eventName === "delta" && handlers.onDelta) {
      handlers.onDelta(data || {});
    } else if (eventName === "done" && handlers.onDone) {
      handlers.onDone(data || {});
    } else if (eventName === "error" && handlers.onError) {
      handlers.onError(data || {});
    }
  }

  /**
   * POST 流式请求并按契约解析 SSE（meta/delta/done/error）。
   * @param {string} path
   * @param {*} body
   * @param {{onMeta?:Function,onDelta?:Function,onDone?:Function,onError?:Function}} [handlers]
   * @returns {Promise<void>}
   */
  async function stream(path, body, handlers) {
    handlers = handlers || {};

    var response;
    try {
      response = await global.fetch(BASE + path, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
          "X-User-Id": userId(),
        },
        body: JSON.stringify(body === undefined || body === null ? {} : body),
      });
    } catch (networkError) {
      if (handlers.onError) {
        handlers.onError({
          code: "NETWORK_ERROR",
          message: (networkError && networkError.message) || "网络请求失败",
        });
        return;
      }
      throw networkError;
    }

    if (!response.ok) {
      var payload = await parseBody(response);
      var error = normalizeError(response.status, response.statusText, payload);
      if (handlers.onError) {
        handlers.onError(error);
        return;
      }
      throw error;
    }

    if (!response.body || typeof response.body.getReader !== "function") {
      if (handlers.onError) {
        handlers.onError({
          code: "STREAM_UNSUPPORTED",
          message: "当前浏览器不支持流式读取响应",
        });
      }
      return;
    }

    var reader = response.body.getReader();
    var decoder = new TextDecoder("utf-8");
    var buffer = "";

    try {
      for (;;) {
        var chunk = await reader.read();
        if (chunk.done) {
          break;
        }
        buffer += decoder.decode(chunk.value, { stream: true });
        buffer = buffer.replace(/\r\n/g, "\n");
        var boundary = buffer.indexOf("\n\n");
        while (boundary !== -1) {
          dispatchSseFrame(buffer.slice(0, boundary), handlers);
          buffer = buffer.slice(boundary + 2);
          boundary = buffer.indexOf("\n\n");
        }
      }
      buffer += decoder.decode();
      buffer = buffer.replace(/\r\n/g, "\n");
      if (buffer.trim()) {
        dispatchSseFrame(buffer, handlers);
      }
    } catch (streamError) {
      if (handlers.onError) {
        handlers.onError({
          code: "STREAM_ERROR",
          message: (streamError && streamError.message) || "流式读取失败",
        });
      }
    } finally {
      try {
        reader.releaseLock();
      } catch (err) {
        /* ignore */
      }
    }
  }

  global.API = {
    base: BASE,
    userId: userId,
    get: get,
    post: post,
    put: put,
    del: del,
    stream: stream,
  };
})(window);