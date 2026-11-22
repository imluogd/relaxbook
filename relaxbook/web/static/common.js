/* RelaxBook 前端公共工具：接口请求、状态文案、时间与大小格式化。 */
(function () {
  "use strict";

  const STATE_LABELS = {
    parsing: "解析中",
    ready: "待核对",
    failed: "导入失败",
    queued: "排队中",
    synthesizing: "合成中",
    packaging: "封装中",
    done: "已完成",
    paused: "已暂停",
    pending: "等待中",
  };

  async function apiFetch(url, options) {
    const response = await fetch(url, options);
    let data = null;
    try {
      data = await response.json();
    } catch (err) {
      data = null;
    }
    if (!response.ok) {
      let detail = data && data.detail;
      if (detail && typeof detail !== "string") {
        detail = JSON.stringify(detail);
      }
      const error = new Error(detail || ("请求失败（HTTP " + response.status + "）"));
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function stateLabel(state) {
    return STATE_LABELS[state] || state || "";
  }

  /* 毫秒四舍五入到秒后显示为 mm:ss。 */
  function formatMs(ms) {
    if (ms === null || ms === undefined || ms === "" || isNaN(Number(ms))) {
      return "--";
    }
    const total = Math.round(Number(ms) / 1000);
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    return String(minutes).padStart(2, "0") + ":" + String(seconds).padStart(2, "0");
  }

  /* 字节数按 1 位小数的 MB 显示。 */
  function formatMB(bytes) {
    if (bytes === null || bytes === undefined || bytes === "" || isNaN(Number(bytes))) {
      return "--";
    }
    return (Number(bytes) / (1024 * 1024)).toFixed(1) + " MB";
  }

  function sleep(ms) {
    return new Promise(function (resolve) {
      window.setTimeout(resolve, ms);
    });
  }

  window.RelaxBook = {
    STATE_LABELS: STATE_LABELS,
    apiFetch: apiFetch,
    stateLabel: stateLabel,
    formatMs: formatMs,
    formatMB: formatMB,
    sleep: sleep,
  };

  window.apiFetch = apiFetch;
  window.stateLabel = stateLabel;
  window.formatMs = formatMs;
  window.formatMB = formatMB;
  window.sleep = sleep;
})();
