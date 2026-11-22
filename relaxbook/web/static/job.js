/* 转换结果页：每 2 秒轮询任务状态，渲染进度、M4B 信息与章节表。 */
(function () {
  "use strict";

  const JOB_ID = window.JOB_ID;
  const $ = function (id) { return document.getElementById(id); };
  const ACTION_LABELS = { pause: "暂停", resume: "继续", retry: "重试" };

  function addCell(row, text) {
    const cell = document.createElement("td");
    cell.textContent = text;
    row.appendChild(cell);
    return cell;
  }

  function renderChapters(chapters) {
    const body = $("chapter-table").querySelector("tbody");
    body.innerHTML = "";
    if (!chapters.length) {
      const row = document.createElement("tr");
      row.className = "empty";
      const cell = addCell(row, "暂无章节");
      cell.colSpan = 8;
      body.appendChild(row);
      return;
    }
    chapters.forEach(function (chapter) {
      const row = document.createElement("tr");
      addCell(row, String(chapter.index));
      addCell(row, (chapter.title || "未命名章节") + (chapter.read ? "" : "（跳过）"));
      addCell(row, chapter.mp3_filename || "--");
      const hasM4b = chapter.m4b_start_ms !== null && chapter.m4b_start_ms !== undefined;
      addCell(
        row,
        hasM4b ? formatMs(chapter.m4b_start_ms) + " – " + formatMs(chapter.m4b_end_ms) : "--"
      );
      addCell(row, formatMs(chapter.mp3_duration_ms));
      addCell(row, formatMB(chapter.mp3_bytes));
      addCell(row, chapter.state === "done" ? "已完成" : "等待中");
      const download = addCell(row, "");
      if (chapter.mp3_artifact_id) {
        const link = document.createElement("a");
        link.href = "/api/artifacts/" + chapter.mp3_artifact_id;
        link.textContent = "下载 MP3";
        download.appendChild(link);
      } else {
        download.textContent = "--";
      }
      body.appendChild(row);
    });
  }

  function setButtons(state) {
    $("btn-pause").disabled = !(state === "queued" || state === "synthesizing");
    $("btn-resume").disabled = state !== "paused";
    $("btn-retry").disabled = state !== "failed";
  }

  function render(data) {
    const job = data.job || {};
    const chapters = data.chapters || [];
    const m4b = data.m4b;

    $("meta-title").textContent = (data.book && data.book.title) || "未命名书籍";
    $("meta-voice").textContent = job.voice || "";
    $("meta-rate").textContent = job.rate || "";
    $("meta-state").textContent = stateLabel(job.state);
    $("meta-chapters").textContent =
      (job.done_chapters || 0) + " / " + (job.total_chapters || 0);
    $("meta-segments").textContent =
      (job.done_segments || 0) + " / " + (job.total_segments || 0);
    $("meta-failed").textContent = String(job.failed_segments || 0);
    $("meta-duration").textContent = m4b ? formatMs(m4b.duration_ms) : "--";
    $("meta-filename").textContent = m4b ? m4b.filename : "--";
    $("meta-size").textContent = m4b ? formatMB(m4b.bytes) : "--";

    const total = job.total_segments || 0;
    const done = job.done_segments || 0;
    let percent = 0;
    if (total > 0) percent = Math.min(100, Math.round((done * 100) / total));
    else if (job.state === "done") percent = 100;
    $("progress-bar").style.width = percent + "%";
    $("progress-text").textContent = percent + "%";

    const m4bButton = $("btn-m4b");
    if (m4b && m4b.artifact_id) {
      m4bButton.href = "/api/artifacts/" + m4b.artifact_id;
      m4bButton.textContent = m4b.verified ? "下载有章节的 M4B" : "下载 M4B（未通过校验）";
      m4bButton.classList.remove("disabled");
      m4bButton.classList.add("primary");
    } else {
      m4bButton.href = "#";
      m4bButton.textContent = "M4B 尚未生成";
      m4bButton.classList.add("disabled");
      m4bButton.classList.remove("primary");
    }

    const hasMp3 = chapters.some(function (chapter) { return chapter.mp3_artifact_id; });
    $("btn-zip").style.display = hasMp3 ? "" : "none";

    $("job-error").textContent = job.error ? "错误信息：" + job.error : "";
    renderChapters(chapters);
    setButtons(job.state);
  }

  async function refresh() {
    try {
      const data = await apiFetch("/api/jobs/" + JOB_ID);
      render(data);
    } catch (err) {
      $("job-error").textContent = "读取任务失败：" + err.message;
    }
  }

  async function act(action) {
    $("action-status").textContent = "正在请求" + (ACTION_LABELS[action] || action) + "…";
    try {
      const data = await apiFetch("/api/jobs/" + JOB_ID + "/" + action, { method: "POST" });
      $("action-status").textContent =
        "已请求" + (ACTION_LABELS[action] || action) + "（当前状态：" + stateLabel(data.state) + "）";
      await refresh();
    } catch (err) {
      $("action-status").textContent = "操作失败：" + err.message;
    }
  }

  $("btn-pause").addEventListener("click", function () { act("pause"); });
  $("btn-resume").addEventListener("click", function () { act("resume"); });
  $("btn-retry").addEventListener("click", function () { act("retry"); });

  refresh();
  window.setInterval(refresh, 2000);
})();
