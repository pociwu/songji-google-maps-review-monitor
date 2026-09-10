(() => {
  const form = document.querySelector("#add-shop-form");
  if (!form) return;
  const urlInput = document.querySelector("#new-shop-url");
  const nameInput = document.querySelector("#new-shop-name");
  const preview = document.querySelector("#new-shop-preview");
  const status = document.querySelector("#new-shop-status");
  const resolveButton = document.querySelector("#resolve-shop-button");
  const addButton = document.querySelector("#add-shop-button");
  let token = "";
  let generation = 0;
  let timer;
  let saving = false;
  const api = async (path, body) => {
    const response = await fetch(path, {
      method: "POST", headers: {"Content-Type": "application/json", "X-Requested-With": "SongjiPortal"},
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "欄位格式不正確，請檢查後重試。");
    return result;
  };
  const resolve = async () => {
    clearTimeout(timer);
    if (saving || !urlInput.reportValidity()) return;
    const current = ++generation;
    token = "";
    preview.hidden = true;
    addButton.disabled = true;
    resolveButton.disabled = true;
    status.textContent = "正在解析 Google 分享網址…";
    try {
      const result = await api("/api/shops/resolve", {url: urlInput.value.trim()});
      if (current !== generation) return;
      token = result.token;
      nameInput.value = result.name;
      nameInput.required = true;
      document.querySelector("#new-shop-link").href = result.url;
      document.querySelector("#new-shop-note").textContent = result.note || "已取得店家網址，請確認店名後新增。";
      preview.hidden = false;
      addButton.disabled = false;
      status.textContent = "網址已解析，請確認店家。";
    } catch (error) {
      if (current === generation) status.textContent = error.message || "解析失敗，請稍後重試。";
    } finally {
      if (current === generation) resolveButton.disabled = false;
    }
  };
  urlInput.addEventListener("input", () => {
    ++generation;
    token = "";
    preview.hidden = true;
    nameInput.required = false;
    addButton.disabled = true;
    resolveButton.disabled = false;
    status.textContent = "";
    clearTimeout(timer);
    if (urlInput.value.trim() && urlInput.validity.valid) timer = setTimeout(resolve, 700);
  });
  resolveButton.addEventListener("click", resolve);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (saving) return;
    if (!token) { await resolve(); return; }
    if (!nameInput.reportValidity()) return;
    saving = true;
    addButton.disabled = true;
    urlInput.disabled = true;
    resolveButton.disabled = true;
    status.textContent = "正在儲存監控店家…";
    try {
      const result = await api("/api/shops", {name: nameInput.value.trim(), token});
      try { sessionStorage.setItem("songji.portal.shop-added", result.message); } catch {}
      window.location.assign("/");
    } catch (error) {
      status.textContent = error.message || "儲存失敗，請稍後重試。";
      saving = false;
      addButton.disabled = false;
      urlInput.disabled = false;
      resolveButton.disabled = false;
    }
  });
  try {
    const message = sessionStorage.getItem("songji.portal.shop-added");
    if (message) {
      document.querySelector("#add-shop-panel").open = true;
      status.textContent = message;
      sessionStorage.removeItem("songji.portal.shop-added");
    }
  } catch {}
})();

(() => {
  const grid = document.querySelector("#shop-grid");
  if (!grid) return;

  const storageKey = "songji.portal.shop-order.v1";
  const cards = () => [...grid.querySelectorAll("[data-shop-key]")];

  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || "[]");
    const byKey = new Map(cards().map((card) => [card.dataset.shopKey, card]));
    saved.forEach((key) => {
      const card = byKey.get(key);
      if (card) grid.append(card);
    });
  } catch {
    localStorage.removeItem(storageKey);
  }

  const saveOrder = () => {
    localStorage.setItem(storageKey, JSON.stringify(cards().map((card) => card.dataset.shopKey)));
  };

  let draggedCard = null;
  let activeHandle = null;

  grid.addEventListener("pointerdown", (event) => {
    const handle = event.target.closest(".drag-handle");
    if (!handle) return;
    draggedCard = handle.closest("[data-shop-key]");
    activeHandle = handle;
    handle.setPointerCapture(event.pointerId);
    draggedCard.classList.add("is-dragging");
    event.preventDefault();
  });

  grid.addEventListener("pointermove", (event) => {
    if (!draggedCard) return;
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-shop-key]");
    if (!target || target === draggedCard || target.parentElement !== grid) return;

    const rect = target.getBoundingClientRect();
    const singleColumn = grid.clientWidth < rect.width * 1.5;
    const before = singleColumn
      ? event.clientY < rect.top + rect.height / 2
      : event.clientX < rect.left + rect.width / 2;
    grid.insertBefore(draggedCard, before ? target : target.nextSibling);
  });

  const finishDrag = () => {
    if (!draggedCard) return;
    draggedCard.classList.remove("is-dragging");
    draggedCard = null;
    activeHandle = null;
    saveOrder();
  };

  grid.addEventListener("pointerup", finishDrag);
  grid.addEventListener("pointercancel", finishDrag);

  grid.addEventListener("keydown", (event) => {
    const handle = event.target.closest(".drag-handle");
    if (!handle || !["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) return;
    const card = handle.closest("[data-shop-key]");
    const previous = card.previousElementSibling;
    const next = card.nextElementSibling;
    if (["ArrowUp", "ArrowLeft"].includes(event.key) && previous?.matches("[data-shop-key]")) {
      grid.insertBefore(card, previous);
    } else if (["ArrowDown", "ArrowRight"].includes(event.key) && next?.matches("[data-shop-key]")) {
      grid.insertBefore(next, card);
    } else {
      return;
    }
    event.preventDefault();
    saveOrder();
    handle.focus();
  });
})();

(() => {
  const controls = [
    {
      toggle: document.querySelector("#similar-review-toggle"),
      storageKey: "songji.portal.show-similar-reviews.v1",
      hiddenClass: "hide-highly-similar",
    },
    {
      toggle: document.querySelector("#suspected-review-toggle"),
      storageKey: "songji.portal.show-suspected-reviews.v1",
      hiddenClass: "hide-suspected",
    },
  ];

  controls.forEach((control) => {
    let visible = true;
    try {
      const saved = localStorage.getItem(control.storageKey);
      visible = saved === null ? true : saved === "true";
    } catch {
      visible = true;
    }
    document.documentElement.classList.toggle(control.hiddenClass, !visible);
    if (!control.toggle) return;
    control.toggle.checked = visible;
    control.toggle.addEventListener("change", () => {
      document.documentElement.classList.toggle(
        control.hiddenClass,
        !control.toggle.checked
      );
      try {
        localStorage.setItem(control.storageKey, String(control.toggle.checked));
      } catch {
        // The control still works for this page when storage is unavailable.
      }
    });
  });
})();

(() => {
  const container = document.querySelector("#analysis-progress");
  if (!container) return;
  const stage = document.querySelector("#analysis-stage");
  const meter = document.querySelector("#analysis-meter");
  const percent = document.querySelector("#analysis-percent");
  let wasRunning = false;

  const refresh = async () => {
    try {
      const response = await fetch("/api/analysis-status", { cache: "no-store" });
      const value = await response.json();
      const running = value.status === "running";
      container.hidden = !running;
      if (running) {
        wasRunning = true;
        stage.textContent = `${value.stage}（${value.processed}/${value.total}）`;
        meter.value = value.percent;
        percent.textContent = `${value.percent}%`;
      } else if (wasRunning && value.status === "completed") {
        window.location.reload();
        return;
      }
    } catch {
      container.hidden = true;
    }
    window.setTimeout(refresh, 5000);
  };
  refresh();
})();
