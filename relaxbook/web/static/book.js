/* 目录与正文页：核对/编辑章节、多音色试听、开始整书生成。 */
(function () {
  "use strict";

  const BOOK_ID = window.BOOK_ID;
  const $ = function (id) { return document.getElementById(id); };

  let chapters = [];
  let currentIndex = -1;
  let baseRevisionId = null;
  let dirty = false;
  let voices = [];
  let playing = false;

  function setDirty(value) {
    dirty = value;
    $("save-status").textContent = value ? "有未保存的修改" : "";
  }

  function chapterTitle(chapter) {
    return (chapter.title || "未命名章节") + (chapter.read ? "" : "（已跳过）");
  }

  function renderChapters() {
    const list = $("chapter-list");
    list.innerHTML = "";
    chapters.forEach(function (chapter, index) {
      const item = document.createElement("li");
      item.textContent = (index + 1) + ". " + (chapter.title || "未命名章节") +
        (chapter.read ? "" : "（跳过）");
      if (index === currentIndex) item.classList.add("selected");
      if (!chapter.read) item.classList.add("skipped");
      item.addEventListener("click", function () { selectChapter(index); });
      list.appendChild(item);
    });
  }

  async function ensureChapterText(chapter) {
    if (chapter.text !== undefined && chapter.text !== null) return chapter.text;
    if (!chapter.id) return "";
    const data = await apiFetch("/api/books/" + BOOK_ID + "/chapters/" + chapter.id);
    chapter.text = (data.chapter && data.chapter.text) || "";
    return chapter.text;
  }

  function saveCurrentText() {
    if (currentIndex >= 0 && currentIndex < chapters.length) {
      chapters[currentIndex].text = $("chapter-text").value;
    }
  }

  async function selectChapter(index) {
    if (index === currentIndex) return;
    saveCurrentText();
    currentIndex = index;
    const chapter = chapters[index];
    if (!chapter) return;
    renderChapters();
    $("chapter-heading").textContent = chapterTitle(chapter);
    const warnings = (chapter.warnings || []).slice();
    if (chapter.locator && Object.keys(chapter.locator).length > 0) {
      warnings.push("来源位置：" + JSON.stringify(chapter.locator));
    }
    $("chapter-warning").textContent = warnings.join("；");
    $("chapter-text").disabled = true;
    $("chapter-text").value = "正在载入正文…";
    try {
      $("chapter-text").value = (await ensureChapterText(chapter)) || "";
    } catch (err) {
      $("chapter-text").value = "";
      $("chapter-warning").textContent = "读取正文失败：" + err.message;
    } finally {
      $("chapter-text").disabled = false;
    }
    if (!$("preview-text").value.trim()) {
      $("preview-text").value = ($("chapter-text").value || "").trim().slice(0, 160);
    }
  }

  async function loadBook() {
    let data;
    try {
      data = await apiFetch("/api/books/" + BOOK_ID);
    } catch (err) {
      alert("读取书籍失败：" + err.message);
      return;
    }
    $("book-title").textContent = data.book.title || "未命名书籍";
    $("book-format").textContent = data.book.format || "";
    $("book-status-label").textContent = stateLabel(data.book.status);
    $("book-warnings").textContent = (data.warnings || []).length + " 条导入提示";
    $("book-warning-text").textContent = (data.warnings || []).join("；") ||
      (data.book.status === "failed" ? (data.book.error || "") : "");
    baseRevisionId = data.revision ? data.revision.id : null;
    chapters = (data.chapters || []).map(function (item) {
      return {
        id: item.id,
        idx: item.idx,
        title: item.title,
        read: !!item.read,
        warnings: item.warnings || [],
        locator: item.locator || {},
        text: undefined,
      };
    });
    currentIndex = -1;
    renderChapters();
    if (chapters.length) {
      await selectChapter(0);
    } else {
      $("chapter-heading").textContent = "正文（暂无章节，等待解析）";
      $("chapter-text").value = "";
    }
  }

  async function pollBook() {
    try {
      const data = await apiFetch("/api/books/" + BOOK_ID);
      $("book-status-label").textContent = stateLabel(data.book.status);
      $("book-warnings").textContent = (data.warnings || []).length + " 条导入提示";
      if (data.revision && !baseRevisionId) {
        await loadBook();
        return;
      }
      if (data.book.status === "failed") {
        $("book-warning-text").textContent = data.book.error || "导入失败";
      }
    } catch (err) {
      /* 轮询失败静默，下个周期再试 */
    }
  }

  async function handleAction(action) {
    if (currentIndex < 0 || !chapters.length) return;
    saveCurrentText();
    const chapter = chapters[currentIndex];
    if (action === "rename") {
      const name = window.prompt("新的章名", chapter.title || "");
      if (name === null) return;
      chapter.title = name.trim() || "未命名章节";
      $("chapter-heading").textContent = chapterTitle(chapter);
      setDirty(true);
      renderChapters();
    } else if (action === "up") {
      if (currentIndex === 0) { alert("已经是第一章"); return; }
      const previous = chapters[currentIndex - 1];
      chapters[currentIndex - 1] = chapter;
      chapters[currentIndex] = previous;
      currentIndex -= 1;
      setDirty(true);
      renderChapters();
      $("chapter-heading").textContent = chapterTitle(chapter);
    } else if (action === "down") {
      if (currentIndex >= chapters.length - 1) { alert("已经是最后一章"); return; }
      const next = chapters[currentIndex + 1];
      chapters[currentIndex + 1] = chapter;
      chapters[currentIndex] = next;
      currentIndex += 1;
      setDirty(true);
      renderChapters();
      $("chapter-heading").textContent = chapterTitle(chapter);
    } else if (action === "split") {
      const area = $("chapter-text");
      const position = area.selectionStart;
      const full = area.value;
      if (position <= 0 || position >= full.length) {
        alert("请先在正文中把光标放到要拆分的位置");
        return;
      }
      chapter.text = full.slice(0, position).replace(/\s+$/, "");
      const newChapter = {
        id: null,
        idx: null,
        title: (chapter.title || "未命名章节") + "（续）",
        read: true,
        warnings: [],
        locator: {},
        text: full.slice(position).replace(/^\s+/, ""),
      };
      chapters.splice(currentIndex + 1, 0, newChapter);
      setDirty(true);
      currentIndex += 1;
      renderChapters();
      $("chapter-heading").textContent = chapterTitle(newChapter);
      area.value = newChapter.text;
    } else if (action === "merge") {
      if (currentIndex >= chapters.length - 1) {
        alert("已经是最后一章，无法与下一章合并");
        return;
      }
      const next = chapters[currentIndex + 1];
      try {
        await ensureChapterText(next);
      } catch (err) {
        alert("读取下一章正文失败：" + err.message);
        return;
      }
      chapter.text = (chapter.text || "").replace(/\s+$/, "") + "\n\n" +
        (next.text || "").replace(/^\s+/, "");
      chapters.splice(currentIndex + 1, 1);
      $("chapter-text").value = chapter.text;
      setDirty(true);
      renderChapters();
    } else if (action === "toggle-read") {
      chapter.read = !chapter.read;
      $("chapter-heading").textContent = chapterTitle(chapter);
      setDirty(true);
      renderChapters();
    }
  }

  async function collectChapters() {
    saveCurrentText();
    const payload = [];
    for (let i = 0; i < chapters.length; i++) {
      const chapter = chapters[i];
      if (chapter.text === undefined) {
        try {
          await ensureChapterText(chapter);
        } catch (err) {
          throw new Error("读取《" + (chapter.title || "未命名章节") + "》正文失败：" + err.message);
        }
      }
      payload.push({
        title: chapter.title,
        text: chapter.text || "",
        read: !!chapter.read,
        source_locator: chapter.locator || null,
      });
    }
    return payload;
  }

  async function saveRevision() {
    if (!baseRevisionId) {
      alert("书籍尚未解析完成，暂时无法保存编辑版本");
      return;
    }
    const button = $("btn-save");
    button.disabled = true;
    $("save-status").textContent = "正在保存…";
    try {
      const payload = await collectChapters();
      const data = await apiFetch("/api/books/" + BOOK_ID + "/revision", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_revision_id: baseRevisionId,
          note: $("edit-note").value || "",
          chapters: payload,
        }),
      });
      baseRevisionId = data.revision_id;
      dirty = false;
      await loadBook();
      $("save-status").textContent = "已保存新编辑版本 #" + data.revision_id;
    } catch (err) {
      if (err.status === 409) {
        alert("版本已过期，请刷新后重试");
        await loadBook();
      } else {
        alert("保存失败：" + err.message);
        $("save-status").textContent = "保存失败：" + err.message;
      }
    } finally {
      button.disabled = false;
    }
  }

  async function loadVoices() {
    let data;
    try {
      data = await apiFetch("/api/voices");
    } catch (err) {
      $("voice-list").textContent = "加载音色失败：" + err.message;
      return;
    }
    voices = (data.voices || []).filter(function (voice) { return voice && voice.id; });
    const container = $("voice-list");
    const select = $("job-voice");
    container.innerHTML = "";
    select.innerHTML = "";
    if (!voices.length) {
      container.textContent = "没有可用音色，请检查语音引擎配置（edge 引擎需要联网）。";
      return;
    }
    const engines = data.engines || [];
    if (!engines.some(function (engine) { return engine.available; })) {
      container.appendChild(document.createTextNode("提示：可用音色不足（建议至少 3 个中文 + 1 个英文）。"));
    }
    voices.forEach(function (voice, index) {
      const label = document.createElement("label");
      label.className = "voice-item";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = voice.id;
      checkbox.dataset.voice = voice.id;
      if (index === 0) checkbox.checked = true;
      label.appendChild(checkbox);
      const gender = voice.gender === "Male" ? "男" : (voice.gender === "Female" ? "女" : "");
      label.appendChild(document.createTextNode(
        " " + (voice.name || voice.id) + "（" + (voice.language || "") +
        (gender ? "，" + gender : "") + "）"
      ));
      container.appendChild(label);

      const option = document.createElement("option");
      option.value = voice.id;
      option.textContent = (voice.name || voice.id) + "（" + (voice.language || "") + "）";
      select.appendChild(option);
    });
  }

  function checkedVoices() {
    return Array.prototype.slice.call(
      document.querySelectorAll("#voice-list input[type=checkbox]:checked")
    ).map(function (checkbox) { return checkbox.dataset.voice; });
  }

  function voiceName(voiceId) {
    const found = voices.filter(function (voice) { return voice.id === voiceId; })[0];
    return found ? (found.name || found.id) : voiceId;
  }

  async function requestPreview(text, voice) {
    const created = await apiFetch("/api/previews", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text, voice: voice, rate: $("rate").value || "+0%" }),
    });
    const previewId = created.preview_id;
    for (let i = 0; i < 240; i++) {
      const status = await apiFetch("/api/previews/" + previewId);
      if (status.status === "ready" && status.audio_url) return status.audio_url;
      if (status.status === "failed") throw new Error(status.error || "试听合成失败");
      await sleep(500);
    }
    throw new Error("试听合成超时");
  }

  function playUrl(url, label) {
    return new Promise(function (resolve) {
      const audio = new window.Audio(url);
      audio.addEventListener("ended", resolve);
      audio.addEventListener("error", function () {
        alert(label + " 播放失败");
        resolve();
      });
      audio.play().catch(function () {
        alert(label + " 无法播放（浏览器可能禁止自动播放，请再点一次）");
        resolve();
      });
    });
  }

  async function playAll() {
    if (playing) return;
    const text = ($("preview-text").value || "").trim();
    if (!text) { alert("请先输入或选择试听文本"); return; }
    const chosen = checkedVoices();
    if (!chosen.length) { alert("请至少勾选一个音色"); return; }
    playing = true;
    const button = $("btn-play-all");
    button.disabled = true;
    try {
      for (let i = 0; i < chosen.length; i++) {
        const voice = chosen[i];
        const name = voiceName(voice);
        $("preview-status").textContent =
          "正在合成：" + name + "（" + (i + 1) + "/" + chosen.length + "）";
        const url = await requestPreview(text, voice);
        $("preview-status").textContent = "正在播放：" + name;
        await playUrl(url, name);
      }
      $("preview-status").textContent = "试听完成";
    } catch (err) {
      $("preview-status").textContent = "试听失败：" + err.message;
    } finally {
      playing = false;
      button.disabled = false;
    }
  }

  async function generate() {
    if (!baseRevisionId) { alert("书籍尚未解析完成，请稍后再试"); return; }
    if (dirty) { alert("有未保存的修改，请先点“保存新编辑版本”再开始生成"); return; }
    const voice = $("job-voice").value;
    if (!voice) { alert("请选择整书音色"); return; }
    $("generate-status").textContent = "正在创建任务…";
    try {
      const data = await apiFetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          book_id: BOOK_ID,
          revision_id: baseRevisionId,
          voice: voice,
          rate: $("rate").value || "+0%",
        }),
      });
      $("generate-status").textContent = "任务已创建，正在跳转…";
      window.location.href = "/jobs/" + data.job_id;
    } catch (err) {
      $("generate-status").textContent = "创建任务失败：" + err.message;
    }
  }

  $("chapter-text").addEventListener("input", function () {
    if (currentIndex >= 0) {
      saveCurrentText();
      setDirty(true);
    }
  });
  $("btn-save").addEventListener("click", saveRevision);
  $("btn-play-all").addEventListener("click", playAll);
  $("btn-generate").addEventListener("click", generate);
  Array.prototype.forEach.call(
    document.querySelectorAll(".chapter-actions button"),
    function (button) {
      button.addEventListener("click", function () { handleAction(button.dataset.action); });
    }
  );

  loadVoices();
  loadBook();
  window.setInterval(pollBook, 2000);
})();
