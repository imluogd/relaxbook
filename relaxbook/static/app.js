const $ = (id) => document.getElementById(id);
const state = { book: null, chapters: [], selected: 0, dirty: false, job: null, jobSnapshot: null, voices: [] };
const statuses = { queued: "排队中", parsing: "正在提取正文", ready: "已导入", running: "正在生成",
  pausing: "即将暂停", paused: "已暂停", packaging: "正在封装 M4B", complete: "已完成",
  failed: "失败", pending: "待生成" };

function element(tag, value, parent) {
  const item = document.createElement(tag);
  item.textContent = value;
  if (parent) parent.append(item);
  return item;
}

function notice(message) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}

async function api(path, method = "GET", data) {
  const opts = { method, cache: "no-store" };
  if (data instanceof FormData) opts.body = data;
  else if (data) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(data);
  }
  const result = await fetch("/api/" + path, opts);
  if (!result.ok) {
    const error = await result.json().catch(() => ({}));
    throw Error(typeof error.detail === "string" ? error.detail : "操作失败，请检查输入。");
  }
  return result.json();
}

function action(button, perform) {
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    notice("");
    try { await perform(); } catch (error) { notice(error.message); }
    finally { button.disabled = button.id === "save" && !state.dirty; }
  });
}

function time(ms) {
  if (ms == null) return "—";
  const value = Math.round(ms / 1000);
  const hours = Math.floor(value / 3600);
  const minutes = String(Math.floor(value / 60) % 60).padStart(2, "0");
  const seconds = String(value % 60).padStart(2, "0");
  return (hours ? hours + ":" : "") + minutes + ":" + seconds;
}
function size(bytes) {
  if (bytes == null) return "—";
  return bytes >= 1048576 ? (bytes / 1048576).toFixed(2) + " MB" : (bytes / 1024).toFixed(1) + " KB";
}

async function library() {
  const books = await api("books");
  $("library").replaceChildren();
  for (const book of books) {
    const button = element("button", book.title, $("library"));
    button.setAttribute("aria-current", String(state.book?.id === book.id));
    action(button, async () => {
      if (!state.dirty || confirm("放弃尚未保存的章节修改？")) await openBook(book.id);
    });
  }
  return books;
}

function chapterList() {
  $("chapters").replaceChildren();
  state.chapters.forEach((chapter, i) => {
    const button = element("button", (i + 1) + " · " + (chapter.title || "未命名章节") +
      (chapter.skip ? "（跳过）" : ""), $("chapters"));
    button.setAttribute("aria-current", String(i === state.selected));
    action(button, async () => selectChapter(i));
  });
}

async function chapterText(chapter) {
  if (chapter.text == null) {
    const record = await api("books/" + state.book.id + "/chapters/" + chapter.id +
      "?revision=" + encodeURIComponent(state.book.revision));
    chapter.text = record.text;
  }
  return chapter.text;
}

async function selectChapter(index) {
  const selected = state.chapters[index];
  if (!selected) return;
  const text = await chapterText(selected);
  if (state.chapters[index] !== selected) return;
  state.selected = index;
  $("chapter-title").value = selected.title;
  $("chapter-source").textContent = selected.source;
  $("chapter-text").value = text;
  $("skip").checked = selected.skip;
  $("sample").value = text.slice(0, 180);
  chapterList();
}

function dirty() { state.dirty = true; $("save").disabled = false; }

async function save() {
  if (!state.dirty) return;
  await Promise.all(state.chapters.map(chapterText));
  const result = await api("books/" + state.book.id + "/revision", "PUT",
    { revision: state.book.revision, chapters: state.chapters });
  state.book.revision = result.revision;
  state.chapters.forEach((c, i) => c.id = String(i + 1));
  state.dirty = false;
  $("save").disabled = true;
}

async function openBook(id) {
  const data = await api("books/" + id);
  state.book = data;
  state.job = null;
  state.dirty = false;
  $("save").disabled = true;
  $("empty").hidden = true;
  $("book").hidden = false;
  $("title").textContent = data.title;
  $("author").textContent = data.author || "";
  $("ready").hidden = data.status !== "ready";
  $("import-status").textContent = data.status === "ready" ? "" :
    (statuses[data.status] || data.status) + (data.error ? " · " + data.error : "");
  if (data.status === "ready") {
    $("warnings").hidden = !data.warnings.length;
    $("warning-list").replaceChildren();
    for (const warning of data.warnings) element("li", warning, $("warning-list"));
    state.chapters = data.chapters;
    $("samples").replaceChildren();
    await selectChapter(0);
    await listJobs();
  }
  await library();
}

async function listJobs(preferred) {
  const book = await api("books/" + state.book.id);
  if (book.id !== state.book.id) return;
  $("jobs").replaceChildren();
  for (const job of book.jobs) {
    const option = element("option", job.filename, $("jobs"));
    option.value = job.id;
  }
  state.job = preferred || book.jobs[0]?.id || null;
  if (state.job) $("jobs").value = state.job;
  await refreshJob();
}

function download(label, url, parent, primary = false) {
  const link = element("a", label, parent);
  link.href = url;
  link.className = primary ? "download primary" : "download";
  return link;
}

async function refreshJob() {
  const id = state.job;
  const result = id ? await api("jobs/" + id) : null;
  if (id !== state.job) return;
  const snapshot = JSON.stringify(result);
  if (state.jobSnapshot === snapshot) return;
  state.jobSnapshot = snapshot;
  $("job-download").replaceChildren();
  $("result-rows").replaceChildren();
  $("job-table").hidden = !result;
  if (!result) {
    $("job-status").textContent = "书籍转换后，结果会显示在这里。";
    return;
  }
  $("job-status").textContent = [statuses[result.status],
    result.completed_chapters + "/" + result.planned_chapters + " 章",
    result.done + "/" + result.total + " 段",
    result.voice.label, result.current, result.duration_ms != null ? "总时长 " + time(result.duration_ms) : null,
    result.error].filter(Boolean).join(" · ");
  const controls = $("job-download");
  if (result.artifact) {
    download("下载有章节的 M4B", "/api/artifacts/" + id + "/m4b", controls, true);
    element("span", result.artifact.name + " · " + size(result.artifact.bytes), controls);
  }
  const buttons = { queued: ["pause", "暂停"], running: ["pause", "暂停"],
    paused: ["resume", "继续"], failed: ["retry", "重试"] };
  if (buttons[result.status]) {
    const [command, label] = buttons[result.status];
    const button = element("button", label, controls);
    action(button, async () => { await api("jobs/" + id + "/" + command, "POST"); await refreshJob(); });
  }
  if (result.status === "complete") {
    if (result.archive_status === "complete") download("下载 MP3 打包文件", "/api/artifacts/" + id + "/zip", controls);
    else if (result.archive_status === "queued") element("span", "MP3 打包中", controls);
    else {
      if (result.archive_error) element("span", result.archive_error, controls);
      const zip = element("button", "打包分章 MP3", controls);
      action(zip, async () => { await api("jobs/" + id + "/zip", "POST"); await refreshJob(); });
    }
  }
  for (const chapter of result.chapters) {
    const row = element("tr", "", $("result-rows"));
    element("td", chapter.index + " · " + chapter.title, row);
    const cell = element("td", "", row);
    if (chapter.artifact) download(chapter.filename, "/api/artifacts/" + id + "/" + chapter.id, cell);
    else cell.textContent = chapter.filename;
    element("td", chapter.start_ms == null ? "—" : time(chapter.start_ms) + "–" + time(chapter.end_ms), row);
    element("td", time(chapter.start_ms == null ? chapter.artifact?.duration_ms : chapter.end_ms - chapter.start_ms), row);
    element("td", size(chapter.artifact?.bytes), row);
    element("td", chapter.error || statuses[chapter.status], row);
  }
}

$("upload").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.dirty && !confirm("放弃尚未保存的章节修改？")) return;
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  notice("");
  try {
    const data = new FormData();
    data.set("file", $("source").files[0]);
    const result = await api("books", "POST", data);
    $("source").value = "";
    await openBook(result.book_id);
  } catch (error) { notice(error.message); }
  finally { button.disabled = false; }
});

$("chapter-title").addEventListener("input", () => {
  state.chapters[state.selected].title = $("chapter-title").value;
  dirty();
  chapterList();
});
$("chapter-text").addEventListener("input", () => {
  state.chapters[state.selected].text = $("chapter-text").value;
  dirty();
});
$("skip").addEventListener("change", () => {
  state.chapters[state.selected].skip = $("skip").checked;
  dirty();
  chapterList();
});
for (const [id, delta] of [["up", -1], ["down", 1]]) {
  action($(id), async () => {
    const i = state.selected;
    if (!state.chapters[i + delta]) return;
    [state.chapters[i], state.chapters[i + delta]] = [state.chapters[i + delta], state.chapters[i]];
    dirty();
    await selectChapter(i + delta);
  });
}
action($("split"), async () => {
  const chapter = state.chapters[state.selected];
  const offset = $("chapter-text").selectionStart;
  const left = chapter.text.slice(0, offset).trim(), right = chapter.text.slice(offset).trim();
  if (!left || !right) throw Error("请将光标放在正文中间再拆分。");
  chapter.text = left;
  state.chapters.splice(state.selected + 1, 0, { ...chapter, text: right, title: chapter.title + "（续）" });
  dirty();
  await selectChapter(state.selected + 1);
});
action($("merge"), async () => {
  const next = state.chapters[state.selected + 1];
  if (!next) throw Error("当前章后没有可合并的章节。");
  const current = state.chapters[state.selected];
  current.text += "\n\n" + await chapterText(next);
  current.source += " · " + next.source;
  state.chapters.splice(state.selected + 1, 1);
  dirty();
  await selectChapter(state.selected);
});
action($("save"), save);
action($("preview"), async () => {
  const selected = Array.from($("voice-options").querySelectorAll("input:checked"));
  if (!selected.length) throw Error("请至少选择一个试听音色。");
  $("samples").replaceChildren();
  await Promise.all(selected.map(async (input) => {
    const item = element("div", "", $("samples"));
    item.className = "sample";
    element("strong", input.parentElement.textContent, item);
    const status = element("span", "准备中", item);
    try {
      const request = await api("previews", "POST", { voice: Number(input.value),
        speed: Number($("speed").value), text: $("sample").value });
      status.dataset.preview = request.id;
    } catch (error) { status.textContent = error.message; }
  }));
});
action($("generate"), async () => {
  await save();
  const result = await api("jobs", "POST", { book_id: state.book.id, revision: state.book.revision,
    voice: Number($("voice").value), speed: Number($("speed").value), request_id: crypto.randomUUID() });
  await listJobs(result.job_id);
});
$("jobs").addEventListener("change", async () => {
  state.job = $("jobs").value;
  try { await refreshJob(); } catch (error) { notice(error.message); }
});

async function poll() {
  try {
    if (state.book && ["queued", "parsing"].includes(state.book.status)) {
      const id = state.book.id;
      const data = await api("books/" + id);
      if (state.book.id === id && data.status !== state.book.status) await openBook(id);
    }
    for (const target of $("samples").querySelectorAll("[data-preview]")) {
      const result = await api("previews/" + target.dataset.preview);
      if (result.status === "complete") {
        const player = element("audio", "", target.parentElement);
        player.src = result.url;
        player.controls = true;
        player.preload = "none";
        target.remove();
      } else if (result.status === "failed") {
        target.textContent = result.error;
        delete target.dataset.preview;
      }
    }
    if (state.job) await refreshJob();
  } catch (error) { notice(error.message); }
  finally { setTimeout(poll, 2000); }
}

async function init() {
  try {
    state.voices = await api("voices");
    $("qwen-voice-notice").hidden = !state.voices.some((voice) => voice.engine === "qwen");
    $("qwen-local-notice").hidden = !state.voices.some((voice) => voice.engine === "qwen_local");
    const hasVoices = state.voices.length > 0;
    $("no-voices").hidden = hasVoices;
    for (const id of ["preview", "voice", "generate"]) $(id).disabled = !hasVoices;
    for (const [index, voice] of state.voices.entries()) {
      const option = element("option", voice.label, $("voice"));
      option.value = voice.id;
      const label = element("label", "", $("voice-options"));
      label.className = "check";
      const input = element("input", "", label);
      input.type = "checkbox";
      input.value = voice.id;
      input.checked = index === 0;
      label.append(voice.label);
    }
    await library();
  } catch (error) { notice(error.message); }
  poll();
}

init();
