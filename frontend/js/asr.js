/**
 * asr.js — 前端语音采集与 WAV 封装（契约 C-ASR-UI@1，plan 2.2.7）。
 *
 * 唯一写入者：T4。职责限定在「音频 → WAV → 上传」这条纯技术链路，
 * 不含任何 UI 文案与降级编排（那部分在 chat.js 中）。
 *
 * 对外导出（挂载到 `window.ALA.asr`，与 `window.ALA.voice`（chat.js）区分）：
 *
 *   ALA.asr.isSupported()            -> boolean      // 是否具备 getUserMedia + AudioContext
 *   ALA.asr.isRecording()            -> boolean      // 当前是否正在采集
 *   ALA.asr.startCapture()           -> Promise<void>
 *   ALA.asr.stopCapture()            -> Promise<{blob: Blob, durationMs: number, sampleRate: number}>
 *   ALA.asr.transcribeViaBackend(wavBlob) -> Promise<string>   // POST /api/asr
 *   ALA.asr.buildWavBlob(floatChunks, sourceSampleRate) -> Blob  // 便于验证/调试
 *   ALA.asr.encodeWavPcm16(samplesFloat32, sampleRate) -> Uint8Array
 *   ALA.asr.resampleTo16k(samplesFloat32, sourceSampleRate) -> Float32Array
 *
 * 技术约定（严格对照 C-ASR-UI@1）：
 *  - 采集：`navigator.mediaDevices.getUserMedia({audio: true})` + `AudioContext({sampleRate: 16000})`。
 *  - 采集节点使用 ScriptProcessorNode（无需外部 worklet 模块，符合「离线优先、无 CDN」）；
 *    其输出接入 gain=0 的 GainNode 再连 destination，避免麦克风声音回放造成回声。
 *  - 若 AudioContext 实际采样率非 16000，则线性插值重采样到 16kHz。
 *  - 封装为标准 44 字节 WAV 头 + 单声道 / PCM16 / 16kHz 数据，Blob 类型固定 `audio/wav`。
 *  - 上传：`POST /api/asr`，`multipart/form-data` 字段名 `audio`、文件名 `recording.wav`、
 *    携带 `X-User-Id`；**非流式**（不使用 EventSource / SSE）。
 *
 * 风格：ES5（IIFE + var + function，无箭头函数 / 模板字符串 / let / const）。
 * 依赖：`/static/js/api.js`（读取 `window.API.userId()`，本文件不修改 api.js）。
 */
(function (global) {
  "use strict";

  var TARGET_SAMPLE_RATE = 16000;
  var WAV_MIME_TYPE = "audio/wav";
  var ASR_ENDPOINT = "/api/asr";
  var AUDIO_FIELD_NAME = "audio";
  var AUDIO_FILE_NAME = "recording.wav";
  var USER_ID_STORAGE_KEY = "ala_user_id";
  var WAV_HEADER_BYTES = 44;

  //: 浏览器 ScriptProcessor 的缓冲区长度：4096 帧 @16kHz ≈ 256ms 一块，兼顾延迟与开销。
  var SCRIPT_PROCESSOR_BUFFER_SIZE = 4096;
  //: 单次录音的本地安全上限（秒）：超过后自动截断停止，避免长时录音占满内存。
  var MAX_CAPTURE_SECONDS = 120;

  // ---------------------------------------------------------------------------
  // 能力探测
  // ---------------------------------------------------------------------------

  /** 是否具备麦克风采集能力（getUserMedia 存在且处于安全上下文）。 */
  function hasMediaDevices() {
    return !!(
      global.navigator &&
      global.navigator.mediaDevices &&
      typeof global.navigator.mediaDevices.getUserMedia === "function"
    );
  }

  /** 是否具备 Web Audio（AudioContext 或 webkit 前缀版本）。 */
  function hasAudioContext() {
    return typeof (global.AudioContext || global.webkitAudioContext) === "function";
  }

  /** 录音链路整体可用性：采集 + Web Audio + Blob 三者齐备。 */
  function isSupported() {
    return hasMediaDevices() && hasAudioContext() && typeof global.Blob === "function";
  }

  // ---------------------------------------------------------------------------
  // 采集状态
  // ---------------------------------------------------------------------------

  var captureState = {
    recording: false,
    stream: null,
    context: null,
    sourceNode: null,
    processorNode: null,
    silentGainNode: null,
    floatChunks: [],
    sampleRate: TARGET_SAMPLE_RATE,
    startedAtMs: 0,
  };

  function isRecording() {
    return captureState.recording === true;
  }

  /** 将 getUserMedia / Web Audio 的原生异常转为可读中文错误对象。 */
  function normalizeCaptureError(nativeError) {
    var name = (nativeError && nativeError.name) || "";
    var message;
    if (name === "NotAllowedError" || name === "PermissionDeniedError") {
      message = "麦克风权限被拒绝，请在浏览器地址栏允许录音后重试";
    } else if (name === "NotFoundError" || name === "DevicesNotFoundError") {
      message = "未检测到可用的麦克风设备";
    } else if (name === "NotReadableError" || name === "TrackStartError") {
      message = "麦克风被其他应用占用，请关闭占用程序后重试";
    } else if (name === "SecurityError") {
      message = "当前页面不是安全上下文（需 https 或 localhost），无法使用麦克风";
    } else if (name === "OverconstrainedError") {
      message = "麦克风不支持所需的音频参数";
    } else {
      message = (nativeError && nativeError.message) || "无法启动麦克风采集";
    }
    return {
      code: name || "CAPTURE_FAILED",
      message: message,
    };
  }

  /**
   * 创建 AudioContext，优先请求 16kHz；
   * 部分浏览器忽略或不支持 sampleRate 选项时回退到默认采样率（后续重采样兜底）。
   */
  function createAudioContext() {
    var ContextCtor = global.AudioContext || global.webkitAudioContext;
    var context;
    try {
      context = new ContextCtor({ sampleRate: TARGET_SAMPLE_RATE });
    } catch (unsupportedOptionError) {
      context = new ContextCtor();
    }
    return context;
  }

  /** 复制一块输入采样，避免 ScriptProcessor 复用 inputBuffer 造成数据被覆盖。 */
  function copyChannelSamples(inputBuffer) {
    var samples = inputBuffer.getChannelData(0);
    var copy = new global.Float32Array(samples.length);
    copy.set(samples);
    return copy;
  }

  /**
   * 启动麦克风采集。
   * @returns {Promise<void>} 成功进入录音状态时 resolve；失败 reject `{code, message}`
   */
  function startCapture() {
    return new Promise(function (resolve, reject) {
      if (captureState.recording) {
        resolve();
        return;
      }
      if (!isSupported()) {
        reject({
          code: "CAPTURE_UNSUPPORTED",
          message: "当前浏览器不支持麦克风录音（需 getUserMedia 与 Web Audio）",
        });
        return;
      }

      global.navigator.mediaDevices.getUserMedia({ audio: true }).then(
        function (stream) {
          var context;
          var sourceNode;
          var processorNode;
          var silentGainNode;
          try {
            context = createAudioContext();
            sourceNode = context.createMediaStreamSource(stream);
            processorNode = context.createScriptProcessor(
              SCRIPT_PROCESSOR_BUFFER_SIZE,
              1, // 单声道输入
              1  // 单声道输出
            );
            // 静音增益：保证节点图被浏览器持续驱动，同时不把麦克风声音放出来。
            silentGainNode = context.createGain();
            silentGainNode.gain.value = 0;
          } catch (setupError) {
            stopStreamTracks(stream);
            closeContextQuietly(context);
            reject(normalizeCaptureError(setupError));
            return;
          }

          processorNode.onaudioprocess = function (audioEvent) {
            if (!captureState.recording) {
              return;
            }
            captureState.floatChunks.push(copyChannelSamples(audioEvent.inputBuffer));
            var elapsedMs = captureState.startedAtMs
              ? Date.now() - captureState.startedAtMs
              : 0;
            if (elapsedMs > MAX_CAPTURE_SECONDS * 1000) {
              // 到达本地安全上限：停止继续累积（由上层 UI 决定何时取走音频）。
              captureState.recording = false;
            }
          };

          sourceNode.connect(processorNode);
          processorNode.connect(silentGainNode);
          silentGainNode.connect(context.destination);

          captureState.stream = stream;
          captureState.context = context;
          captureState.sourceNode = sourceNode;
          captureState.processorNode = processorNode;
          captureState.silentGainNode = silentGainNode;
          captureState.floatChunks = [];
          captureState.sampleRate = context.sampleRate || TARGET_SAMPLE_RATE;
          captureState.startedAtMs = Date.now();
          captureState.recording = true;

          resolve();
        },
        function (mediaError) {
          reject(normalizeCaptureError(mediaError));
        }
      );
    });
  }

  function stopStreamTracks(stream) {
    if (!stream || typeof stream.getTracks !== "function") {
      return;
    }
    var tracks = stream.getTracks();
    for (var i = 0; i < tracks.length; i++) {
      try {
        tracks[i].stop();
      } catch (stopError) {
        /* 单个轨道停止失败不影响整体释放 */
      }
    }
  }

  /** 关闭 AudioContext（同步吞掉所有异常；close 的异步 rejection 也被吞掉）。 */
  function closeContextQuietly(context) {
    if (!context || context.state === "closed") {
      return;
    }
    try {
      var closeResult = context.close();
      if (closeResult && typeof closeResult.then === "function") {
        closeResult.then(null, function () {
          /* ignore */
        });
      }
    } catch (contextError) {
      /* ignore */
    }
  }

  /**
   * 释放采集资源（节点断开、轨道停止、上下文关闭），返回本次采集的数据快照。
   * 该函数为同步实现，所有异常均被吞掉，调用方可安全地在任意时机执行。
   */
  function releaseCapture() {
    var snapshot = {
      floatChunks: captureState.floatChunks,
      sampleRate: captureState.sampleRate,
      durationMs: captureState.startedAtMs ? Date.now() - captureState.startedAtMs : 0,
    };

    captureState.recording = false;

    if (captureState.processorNode) {
      try {
        captureState.processorNode.onaudioprocess = null;
        captureState.processorNode.disconnect();
      } catch (processorError) {
        /* ignore */
      }
    }
    if (captureState.sourceNode) {
      try {
        captureState.sourceNode.disconnect();
      } catch (sourceError) {
        /* ignore */
      }
    }
    if (captureState.silentGainNode) {
      try {
        captureState.silentGainNode.disconnect();
      } catch (gainError) {
        /* ignore */
      }
    }
    stopStreamTracks(captureState.stream);
    closeContextQuietly(captureState.context);

    captureState.stream = null;
    captureState.context = null;
    captureState.sourceNode = null;
    captureState.processorNode = null;
    captureState.silentGainNode = null;
    captureState.floatChunks = [];
    captureState.sampleRate = TARGET_SAMPLE_RATE;
    captureState.startedAtMs = 0;

    return snapshot;
  }

  /**
   * 停止麦克风采集并生成 WAV。
   * @returns {Promise<{blob: Blob, durationMs: number, sampleRate: number}>}
   */
  function stopCapture() {
    return new Promise(function (resolve, reject) {
      var wasRecording = captureState.recording;
      var snapshot = releaseCapture();
      if (!wasRecording && (!snapshot.floatChunks || !snapshot.floatChunks.length)) {
        reject({ code: "CAPTURE_NOT_RECORDING", message: "当前没有进行中的录音" });
        return;
      }
      var blob;
      try {
        blob = buildWavBlob(snapshot.floatChunks, snapshot.sampleRate);
      } catch (encodeError) {
        reject({ code: "WAV_ENCODE_FAILED", message: "录音数据封装为 WAV 失败" });
        return;
      }
      var durationMs = snapshot.durationMs;
      if (!durationMs) {
        // 兜底：按 16kHz 单声道采样数估算时长。
        durationMs = Math.round((blob.size - WAV_HEADER_BYTES) / 2 / TARGET_SAMPLE_RATE * 1000);
      }
      resolve({
        blob: blob,
        durationMs: durationMs,
        sampleRate: TARGET_SAMPLE_RATE,
      });
    });
  }

  // ---------------------------------------------------------------------------
  // 重采样 / PCM16 / WAV 封装
  // ---------------------------------------------------------------------------

  /** 将若干 Float32Array 片段拼接为一个连续数组。 */
  function mergeFloatChunks(floatChunks) {
    var totalLength = 0;
    var i;
    for (i = 0; i < floatChunks.length; i++) {
      totalLength += floatChunks[i].length;
    }
    var merged = new global.Float32Array(totalLength);
    var offset = 0;
    for (i = 0; i < floatChunks.length; i++) {
      merged.set(floatChunks[i], offset);
      offset += floatChunks[i].length;
    }
    return merged;
  }

  /**
   * 线性插值重采样到 16kHz。
   * 之所以用线性插值：语音识别对带限要求不高，线性插值实现简单、无外部依赖且无相位失真。
   * @param {Float32Array} samples
   * @param {number} sourceSampleRate
   * @returns {Float32Array}
   */
  function resampleTo16k(samples, sourceSampleRate) {
    var fromRate = Number(sourceSampleRate) || TARGET_SAMPLE_RATE;
    if (!samples || samples.length === 0 || fromRate === TARGET_SAMPLE_RATE) {
      return samples;
    }
    var ratio = fromRate / TARGET_SAMPLE_RATE;
    var outLength = Math.max(1, Math.round(samples.length / ratio));
    var resampled = new global.Float32Array(outLength);
    for (var i = 0; i < outLength; i++) {
      var position = i * ratio;
      var lowerIndex = Math.floor(position);
      var fraction = position - lowerIndex;
      var lower = lowerIndex < samples.length ? samples[lowerIndex] : 0;
      var upper = lowerIndex + 1 < samples.length ? samples[lowerIndex + 1] : lower;
      resampled[i] = lower + (upper - lower) * fraction;
    }
    return resampled;
  }

  function writeAscii(view, offset, text) {
    for (var i = 0; i < text.length; i++) {
      view.setUint8(offset + i, text.charCodeAt(i));
    }
  }

  /**
   * 将 [-1, 1] 的浮点采样编码为标准 44 字节头的 WAV（PCM16 / 单声道 / 指定采样率）。
   * @param {Float32Array} samples
   * @param {number} sampleRate
   * @returns {Uint8Array}
   */
  function encodeWavPcm16(samples, sampleRate) {
    var rate = Number(sampleRate) || TARGET_SAMPLE_RATE;
    var sampleCount = samples ? samples.length : 0;
    var dataBytes = sampleCount * 2;
    var buffer = new global.ArrayBuffer(WAV_HEADER_BYTES + dataBytes);
    var view = new global.DataView(buffer);

    writeAscii(view, 0, "RIFF");
    view.setUint32(4, 36 + dataBytes, true); // 文件长度 - 8
    writeAscii(view, 8, "WAVE");

    writeAscii(view, 12, "fmt ");
    view.setUint32(16, 16, true); // PCM 子块大小
    view.setUint16(20, 1, true); // audioFormat = 1 (PCM)
    view.setUint16(22, 1, true); // 声道数 = 1（单声道）
    view.setUint32(24, rate, true); // 采样率
    view.setUint32(28, rate * 2, true); // byteRate = rate * channels * bits/8
    view.setUint16(32, 2, true); // blockAlign = channels * bits/8
    view.setUint16(34, 16, true); // bitsPerSample

    writeAscii(view, 36, "data");
    view.setUint32(40, dataBytes, true);

    var offset = WAV_HEADER_BYTES;
    for (var i = 0; i < sampleCount; i++) {
      var sample = samples[i];
      if (sample > 1) {
        sample = 1;
      } else if (sample < -1) {
        sample = -1;
      }
      view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
      offset += 2;
    }
    return new global.Uint8Array(buffer);
  }

  /**
   * 由浮点采样片段生成 `audio/wav` Blob（内部完成拼接 → 重采样 → PCM16 → 44 字节头封装）。
   * @param {Float32Array[]} floatChunks
   * @param {number} sourceSampleRate
   * @returns {Blob}
   */
  function buildWavBlob(floatChunks, sourceSampleRate) {
    var merged = mergeFloatChunks(floatChunks || []);
    var resampled = resampleTo16k(merged, sourceSampleRate);
    var wavBytes = encodeWavPcm16(resampled, TARGET_SAMPLE_RATE);
    return new global.Blob([wavBytes], { type: WAV_MIME_TYPE });
  }

  // ---------------------------------------------------------------------------
  // 上传到后端 ASR
  // ---------------------------------------------------------------------------

  /** 读取本地用户标识：优先复用 api.js，其次直接读 localStorage（不修改 api.js）。 */
  function currentUserId() {
    if (global.API && typeof global.API.userId === "function") {
      try {
        var fromApi = global.API.userId();
        if (typeof fromApi === "string" && fromApi.trim()) {
          return fromApi.trim();
        }
      } catch (apiError) {
        /* 回退到 localStorage 直读 */
      }
    }
    try {
      var stored = global.localStorage.getItem(USER_ID_STORAGE_KEY);
      if (typeof stored === "string" && stored.trim()) {
        return stored.trim();
      }
    } catch (storageError) {
      /* 隐私模式等场景：无标识时后端会返回 MISSING_USER_ID，由上层提示 */
    }
    return "";
  }

  /**
   * 上传 WAV 到 `POST /api/asr`（multipart/form-data，字段 `audio`，非流式）。
   * @param {Blob} wavBlob
   * @returns {Promise<string>} 识别文本（无语音时为空串）
   */
  function transcribeViaBackend(wavBlob) {
    return new Promise(function (resolve, reject) {
      if (!wavBlob || typeof wavBlob.size !== "number" || wavBlob.size <= WAV_HEADER_BYTES) {
        reject({ code: "ASR_EMPTY_AUDIO", message: "没有可上传的录音内容，请重新录音" });
        return;
      }
      if (typeof global.FormData !== "function") {
        reject({ code: "ASR_UNSUPPORTED", message: "当前浏览器不支持文件上传" });
        return;
      }

      var formData = new global.FormData();
      // 显式指定文件名，保证后端按 `.wav` 处理；MIME 由 Blob 自带 audio/wav。
      formData.append(AUDIO_FIELD_NAME, wavBlob, AUDIO_FILE_NAME);

      // 注意：multipart 的 Content-Type（含 boundary）必须由浏览器自动生成，不能手工设置。
      var requestHeaders = {
        Accept: "application/json",
        "X-User-Id": currentUserId(),
      };

      global.fetch(ASR_ENDPOINT, {
        method: "POST",
        headers: requestHeaders,
        body: formData,
      }).then(
        function (response) {
          response.text().then(
            function (rawText) {
              var payload = null;
              if (rawText) {
                try {
                  payload = JSON.parse(rawText);
                } catch (parseError) {
                  payload = null;
                }
              }
              if (!response.ok) {
                reject({
                  code: (payload && payload.code) || "HTTP_" + response.status,
                  message:
                    (payload && payload.message) ||
                    response.statusText ||
                    "语音识别请求失败",
                  status: response.status,
                  detail: payload && payload.detail,
                });
                return;
              }
              if (!payload || typeof payload.text !== "string") {
                reject({
                  code: "ASR_BAD_RESPONSE",
                  message: "语音识别返回结果格式异常",
                  status: response.status,
                });
                return;
              }
              resolve(payload.text);
            },
            function (readError) {
              reject({
                code: "ASR_BAD_RESPONSE",
                message: "读取语音识别响应失败",
                status: response.status,
              });
            }
          );
        },
        function (networkError) {
          reject({
            code: "NETWORK_ERROR",
            message: (networkError && networkError.message) || "无法连接语音识别服务",
          });
        }
      );
    });
  }

  // ---------------------------------------------------------------------------
  // 导出
  // ---------------------------------------------------------------------------

  global.ALA = global.ALA || {};
  global.ALA.asr = {
    targetSampleRate: TARGET_SAMPLE_RATE,
    wavMimeType: WAV_MIME_TYPE,
    wavHeaderBytes: WAV_HEADER_BYTES,
    endpoint: ASR_ENDPOINT,
    isSupported: isSupported,
    isRecording: isRecording,
    startCapture: startCapture,
    stopCapture: stopCapture,
    buildWavBlob: buildWavBlob,
    encodeWavPcm16: encodeWavPcm16,
    resampleTo16k: resampleTo16k,
    transcribeViaBackend: transcribeViaBackend,
  };
})(window);