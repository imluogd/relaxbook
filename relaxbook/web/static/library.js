/* 书库页：上传电子书 + 每 2 秒轮询书籍状态。 */
(function () {
  "use strict";

  const form = document.getElementById("upload-form");
  const fileInput = document.getElementById("file-input");
  const status = document.getElementById("upload-status");

  if (form) {
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      if (!fileInput || !fileInput.files || !fileInput.files.length) {
        status.textContent = "请先选择要导入的电子书文件。";
        return;
      }
      const data = new FormData();
      data.append("file", fileInput.files[0]);
      status.textContent = "正在上传…";
      try {
        const result = await apiFetch("/api/books", { method: "POST", body: data });
        if (result.duplicate) {
          status.textContent = "该文件已导入过，正在打开已有书籍…";
        } else {
          status.textContent = "上传成功（书籍 ID：" + result.book_id + "），正在后台解析…";
        }
        window.setTimeout(function () {
          window.location.href = "/books/" + result.book_id;
        }, result.duplicate ? 400 : 900);
      } catch (err) {
        status.textContent = "上传失败：" + err.message;
      }
    });
  }

  function findRow(bookId) {
    return document.querySelector('#books-table tbody tr[data-book-id="' + bookId + '"]');
  }

  function setField(row, field, text) {
    const cell = row.querySelector('[data-field="' + field + '"]');
    if (cell) cell.textContent = text;
  }

  async function pollBooks() {
    let data;
    try {
      data = await apiFetch("/api/books");
    } catch (err) {
      return; // 轮询失败静默，下个周期再试
    }
    const rows = document.querySelectorAll("#books-table tbody tr[data-book-id]");
    if (!data.books || rows.length !== data.books.length) {
      window.location.reload();
      return;
    }
    data.books.forEach(function (book) {
      const row = findRow(book.id);
      if (!row) {
        window.location.reload();
        return;
      }
      let label = stateLabel(book.status);
      if (book.status === "failed" && book.error) label += "：" + book.error;
      setField(row, "status", label);
      setField(row, "chapter_count", String(book.chapter_count || 0));
      setField(row, "updated_at", book.updated_at || "");
    });
  }

  window.setInterval(pollBooks, 2000);
})();
