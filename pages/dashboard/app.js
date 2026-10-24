/* 人味层 · 设置面板
 * 走 AstrBot 的插件页 bridge：AstrBotPluginPage.apiGet/apiPost
 * 后端路由：/api/plug/astrbot_plugin_humanlike/page/*
 */
(() => {
  "use strict";

  const PLUGIN = "astrbot_plugin_humanlike";
  const PAGE = "page";
  const SHARP_EDGE = 0.6;

  // ── 演示数据：没有 bridge 时（例如直接开文件、或外面嵌的不是面板）也能看 ──
  const DEMO = {
    config: {
      enable: true,
      groups: "111111111, 222222222, 333333333, 555555555",
      private_enable: true,
      private_users: "",
      prompt_extra: "",
      persona_name: "",
      style: { max_chars: 20, target_chars: 10, split_prob: 0.4 },
      timing: {
        enable: true, fast_reply_prob: 0.2, settle_min: 0.3, settle_max: 1.8,
        long_pause_prob: 0.12, long_pause_max: 5, max_delay: 12,
        night_slowdown: true, typing_indicator: true,
      },
      tools: { guide: true, reply_quote: true, at_user: true },
      mood: {
        energy_baseline: 0.42, spirits_baseline: 0.56, temper_baseline: 0.68,
        edge: 0.3, temper_floor: 0.15, decay_per_hour: 1.0,
        react_scale: 1.0, annoy_step: 0.15,
      },
      guards: { hide_errors: true, scrub_images: true },
    },
    meta: {
      version: "0.8.0",
      axes: [
        { key: "energy", low: "懒散", high: "活力", baseline: 0.42,
          bands: [[0.22, "懒得动", "", ""], [0.34, "有点没劲", "", ""], [0.62, "", "", ""],
                  [0.80, "有点来劲", "", ""], [2, "来劲了", "", ""]] },
        { key: "spirits", low: "伤心", high: "开心", baseline: 0.56,
          bands: [[0.24, "低落", "", ""], [0.42, "有点闷", "", ""], [0.66, "", "", ""],
                  [0.82, "心情不错", "", ""], [2, "挺高兴", "", ""]] },
        { key: "temper", low: "生气", high: "温柔", baseline: 0.68,
          bands: [[0.28, "在生气", "", ""], [0.48, "不太耐烦", "", ""], [0.78, "", "", ""],
                  [0.90, "脾气好", "", ""], [2, "很温柔", "", ""]] },
      ],
    },
    sessions: [
      { umo: "demo:GroupMessage:111111111", kind: "group", name: "111111111",
        emo: { energy: 0.38, spirits: 0.72, temper: 0.78 }, labels: ["心情不错", "脾气好"] },
      { umo: "demo:GroupMessage:222222222", kind: "group", name: "222222222",
        emo: { energy: 0.66, spirits: 0.55, temper: 0.52 }, labels: ["有点来劲"] },
      { umo: "demo:FriendMessage:100000001", kind: "private", name: "100000001",
        emo: { energy: 0.22, spirits: 0.44, temper: 0.26 }, labels: ["懒得动", "不太耐烦"] },
    ],
  };

  const state = { cfg: null, draft: null, meta: null, sessions: [], demo: false, tab: "scope" };

  // ── 小工具 ────────────────────────────────────────────
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const clone = (v) => JSON.parse(JSON.stringify(v));
  const getPath = (obj, path) =>
    String(path).split(".").reduce((acc, k) => (acc == null ? acc : acc[k]), obj);
  const setPath = (obj, path, val) => {
    const keys = String(path).split(".");
    const last = keys.pop();
    let cur = obj;
    for (const k of keys) {
      if (typeof cur[k] !== "object" || cur[k] === null) cur[k] = {};
      cur = cur[k];
    }
    cur[last] = val;
  };
  const clamp01 = (v) => Math.max(0, Math.min(1, v));

  function toast(text, bad = false) {
    const el = $("#toast");
    el.textContent = text;
    el.classList.toggle("is-bad", bad);
    el.classList.add("is-on");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove("is-on"), 2400);
  }

  function unwrap(res) {
    if (res && typeof res === "object" && "status" in res) {
      if (res.status === "error") throw new Error(res.message || "请求失败");
      return res.data !== undefined ? res.data : res;
    }
    return res || {};
  }

  // ── 后端接口 ──────────────────────────────────────────
  const hasBridge = () => Boolean(window.AstrBotPluginPage);

  async function apiGet(path, params = {}) {
    if (!hasBridge()) return demoReply(path, params);
    return unwrap(await window.AstrBotPluginPage.apiGet(`${PAGE}/${path}`, params));
  }
  async function apiPost(path, body = {}) {
    if (!hasBridge()) return demoReply(path, body);
    return unwrap(await window.AstrBotPluginPage.apiPost(`${PAGE}/${path}`, body));
  }

  function demoReply(path, payload) {
    if (path === "preview") return previewLocally(payload);
    if (path === "state") return { sessions: DEMO.sessions };
    if (path === "config" && payload && payload.config) {
      return { config: payload.config, meta: DEMO.meta, sessions: DEMO.sessions };
    }
    return { config: DEMO.config, meta: DEMO.meta, sessions: DEMO.sessions };
  }

  // ── 预览（演示模式下本地算，正式版走后端） ────────────
  function previewLocally({ energy, spirits, temper, edge }) {
    const values = { energy, spirits, temper };
    const labels = [];
    for (const axis of state.meta.axes) {
      const v = Number(values[axis.key]);
      let band = axis.bands.find((b) => v < Number(b[0])) || axis.bands[axis.bands.length - 1];
      if (!band[1]) continue;
      labels.push(band[1]);
      const tip = band[2];
      if (tip) labels.tips = true;
    }
    const head = labels.length
      ? `现在状态：${labels.join("、")}。`
      : "现在状态：跟平时一样，不特别高兴也不特别烦。";
    return { line: head + "（演示数据，真实文案由插件生成）", target: 10 };
  }

  // ── 渲染：导航 / 徽标 ─────────────────────────────────
  const ICONS = {
    scope: '<path d="M12 3v18M3 12h18" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><circle cx="12" cy="12" r="3.2" stroke="currentColor" stroke-width="1.7"/>',
    prompt: '<path d="M5 5h14M5 12h9M5 19h6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    mood: '<path d="M12 20c4.4 0 8-3.6 8-8s-3.6-8-8-8-8 3.6-8 8 3.6 8 8 8z" stroke="currentColor" stroke-width="1.7"/><path d="M8.6 10.4h.01M15.4 10.4h.01" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/><path d="M9 14.6c.9.8 1.9 1.2 3 1.2s2.1-.4 3-1.2" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    rhythm: '<path d="M4 14h3l2-8 3 14 2.5-9 1.8 5H20" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>',
    state: '<path d="M4 18V9M10 18V5M16 18v-6M22 18h-2" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>',
    about: '<circle cx="12" cy="12" r="8.2" stroke="currentColor" stroke-width="1.7"/><path d="M12 11v5.4" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/><circle cx="12" cy="7.9" r="1.1" fill="currentColor"/>',
  };
  function paintIcons() {
    for (const el of $$(".ico[data-ico]")) {
      const path = ICONS[el.dataset.ico];
      if (!path) continue;
      el.innerHTML = `<svg viewBox="0 0 24 24" fill="none">${path}</svg>`;
    }
  }

  const TAB_META = {
    scope: ["生效范围", "决定 bot 在哪些群、哪些私聊里“有人的样子”。"],
    prompt: ["提示词", "不改代码就能调语气——这里的话优先级最高。"],
    mood: ["情绪", "活力、情绪、脾气三条轴；基线决定“平时是什么样”。"],
    rhythm: ["节奏", "完整回复到手之后，决定这条是秒回还是停一下。"],
    state: ["实时状态", "每个会话此刻的三条轴，以及一键安抚。"],
    about: ["关于", "这个插件做了什么，以及去哪看代码。"],
  };

  function setTab(name) {
    if (!TAB_META[name]) name = "scope";
    state.tab = name;
    for (const btn of $$(".nav-item")) btn.classList.toggle("is-active", btn.dataset.tab === name);
    for (const panel of $$(".tab")) panel.classList.toggle("is-active", panel.dataset.panel === name);
    $("#pageTitle").textContent = TAB_META[name][0];
    $("#pageSub").textContent = TAB_META[name][1];
    if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
    if (name === "state") refreshState();
  }

  function paintPills() {
    const cfg = state.draft || {};
    const groups = splitIds(cfg.groups);
    const priv = cfg.private_enable ? (splitIds(cfg.private_users).length ? "私聊·白名单" : "私聊·全部") : "私聊关";
    const items = [
      { text: cfg.enable ? "已启用" : "已停用", cls: cfg.enable ? "is-on" : "is-off" },
      { text: `群 ${groups.length || "全部"}`, cls: groups.length ? "is-accent" : "is-off" },
      { text: priv, cls: cfg.private_enable ? "is-accent" : "is-off" },
      { text: `v${state.meta?.version || "–"}`, cls: "" },
    ];
    $("#statusPills").innerHTML = items
      .map((it) => `<span class="pill ${it.cls}"><i></i>${it.text}</span>`)
      .join("");
  }

  // ── 标签输入 ──────────────────────────────────────────
  function splitIds(raw) {
    return String(raw || "")
      .split(/[,，\s]+/)
      .map((s) => s.trim())
      .filter(Boolean);
  }

  function paintChips(key) {
    const box = $(`.chips[data-chips="${key}"]`);
    if (!box) return;
    const ids = splitIds(getPath(state.draft, key));
    box.innerHTML = "";
    for (const id of ids) {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.innerHTML = `<span></span><button type="button" aria-label="移除">×</button>`;
      chip.firstChild.textContent = id;
      chip.querySelector("button").addEventListener("click", () => {
        const rest = splitIds(getPath(state.draft, key)).filter((x) => x !== id);
        setPath(state.draft, key, rest.join(","));
        paintChips(key);
        onDraftChanged();
      });
      box.appendChild(chip);
    }
    const input = document.createElement("input");
    input.className = "chip-input";
    input.placeholder = box.dataset.placeholder || "回车添加";
    input.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== "," && e.key !== "，") return;
      e.preventDefault();
      const vals = splitIds(input.value);
      if (!vals.length) return;
      const cur = splitIds(getPath(state.draft, key));
      for (const v of vals) if (!cur.includes(v)) cur.push(v);
      setPath(state.draft, key, cur.join(","));
      input.value = "";
      paintChips(key);
      onDraftChanged();
      $(`.chips[data-chips="${key}"] .chip-input`)?.focus();
    });
    box.appendChild(input);
    if (key === "groups") $("#groupCount").textContent = `${ids.length} 个群`;
  }

  // ── 滑块 / 开关 ───────────────────────────────────────
  function paintRangeFill(el) {
    const min = Number(el.min || 0);
    const max = Number(el.max || 1);
    const v = Number(el.value);
    const p = max > min ? (v - min) / (max - min) : 0;
    el.style.setProperty("--p", clamp01(p).toFixed(3));
  }

  function paintControl(el) {
    const path = el.dataset.path;
    const val = getPath(state.draft, path);
    if (el.type === "checkbox") {
      el.checked = Boolean(val);
      // 同时挂一份 class：不依赖 :checked 伪类，样式和状态永远一致
      const track = el.nextElementSibling;
      if (track && track.classList.contains("track")) {
        track.classList.toggle("is-on", el.checked);
      }
    } else if (val !== undefined && val !== null) {
      el.value = val;
    }
    if (el.type === "range") {
      paintRangeFill(el);
      const out = $(`output[data-out="${path}"]`);
      if (out) {
        const step = String(el.step || "1");
        const digits = step.includes(".") ? step.split(".")[1].length : 0;
        out.textContent = Number(el.value).toFixed(digits);
      }
    }
    if (el.tagName === "TEXTAREA") {
      $("#extraCount").textContent = `${String(el.value || "").length} 字`;
    }
  }

  function paintAllControls() {
    for (const el of $$("[data-path]")) paintControl(el);
    paintChips("groups");
    paintChips("private_users");
    paintAxes();
    paintPills();
  }

  // ── 情绪轴 ────────────────────────────────────────────
  function axisSpec(key) {
    return (state.meta?.axes || []).find((a) => a.key === key);
  }

  function bandOf(axis, value) {
    const bands = axis?.bands || [];
    const idx = bands.findIndex((b) => value < Number(b[0]));
    return idx < 0 ? bands.length - 1 : idx;
  }

  function paintAxes() {
    for (const block of $$(".axis[data-axis]")) {
      const key = block.dataset.axis;
      const axis = axisSpec(key);
      if (!axis) continue;
      const path = `mood.${key}_baseline`;
      const val = Number(getPath(state.draft, path) ?? axis.baseline ?? 0.5);
      const idx = bandOf(axis, val);
      const band = axis.bands[idx] || [];
      const label = band[1] || "中性";

      $$(".gauge-seg", block).forEach((seg, i) => seg.classList.toggle("is-on", i === idx));
      $(".gauge-mark", block).style.left = `${clamp01(val) * 100}%`;

      const chip = $(".axis-band", block);
      chip.textContent = label;
      chip.classList.toggle("is-bad", idx === 0);
      chip.classList.toggle("is-low", idx === 1);
      chip.classList.toggle("is-good", idx >= 3);
      $("[data-val]", block).textContent = val.toFixed(2);
    }
  }

  let previewTimer = null;
  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(refreshPreview, 140);
  }

  async function refreshPreview() {
    const mood = state.draft.mood || {};
    const payload = {
      energy: Number(mood.energy_baseline ?? 0.42),
      spirits: Number(mood.spirits_baseline ?? 0.56),
      temper: Number(mood.temper_baseline ?? 0.68),
      edge: Number(mood.edge ?? 0.3),
    };
    try {
      const res = await apiGet("preview", payload);
      if (res && res.line) {
        $("#previewLine").textContent = res.line;
        $("#previewTarget").textContent = res.target ?? "–";
      }
    } catch (e) {
      $("#previewLine").textContent = "预览失败：" + e.message;
    }
  }

  // ── 实时状态 ──────────────────────────────────────────
  function relTime(ts) {
    const mins = Math.max(0, Math.round((Date.now() / 1000 - Number(ts)) / 60));
    if (mins < 1) return "刚刚";
    if (mins < 60) return `${mins} 分钟前`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours} 小时前`;
    return `${Math.round(hours / 24)} 天前`;
  }

  function paintSessions(list) {
    const box = $("#sessions");
    if (!list || !list.length) {
      box.innerHTML = '<div class="empty">还没有会话记录。等群里有人说句话就会出现。</div>';
      return;
    }
    box.innerHTML = "";
    for (const s of list) {
      const kind = s.kind === "private" ? "private" : "group";
      const title = kind === "group" ? `群 ${s.name}` : `私聊 ${s.name}`;
      const bands = ["energy", "spirits", "temper"]
        .map((k) => {
          const v = Number(s.emo?.[k] ?? 0);
          const name = { energy: "活力", spirits: "情绪", temper: "脾气" }[k];
          return `<div class="bar-item" data-k="${k}"><span>${name} ${v.toFixed(2)}</span>
            <div class="bar"><i style="width:${(clamp01(v) * 100).toFixed(0)}%"></i></div></div>`;
        })
        .join("");
      const labels = (s.labels || []).join(" · ") || "没什么特别";
      const ago = s.last_reply_at ? ` · 上次回复 ${relTime(s.last_reply_at)}` : "";
      const row = document.createElement("div");
      row.className = "session";
      row.innerHTML = `
        <div class="session-main">
          <div class="session-name">${title}<span class="tag ${kind === "group" ? "is-group" : ""}">${
        kind === "group" ? "群聊" : "私聊"
      }</span></div>
          <div class="session-sub">${labels}${ago}</div>
        </div>
        <div class="bars">${bands}</div>
        <div class="session-actions">
          <button class="mini-btn" data-act="soothe">哄一下</button>
          <button class="mini-btn" data-act="reset">回到基线</button>
        </div>`;
      row.querySelector('[data-act="soothe"]').addEventListener("click", () =>
        sessionAction(s.umo, "soothe")
      );
      row.querySelector('[data-act="reset"]').addEventListener("click", () =>
        sessionAction(s.umo, "reset")
      );
      box.appendChild(row);
    }
  }

  async function refreshState() {
    try {
      const res = await apiGet("state");
      state.sessions = res.sessions || [];
      paintSessions(state.sessions);
      const now = new Date();
      $("#stateStamp").textContent = `更新于 ${String(now.getHours()).padStart(2, "0")}:${String(
        now.getMinutes()
      ).padStart(2, "0")}:${String(now.getSeconds()).padStart(2, "0")}`;
    } catch (e) {
      $("#sessions").innerHTML = `<div class="empty">读不到状态：${e.message}</div>`;
    }
  }

  async function sessionAction(umo, action) {
    try {
      await apiPost("state", { umo, action });
      toast(action === "soothe" ? "已哄一下" : "已回到基线");
      refreshState();
    } catch (e) {
      toast("操作失败：" + e.message, true);
    }
  }

  // ── 脏标记 / 保存 ─────────────────────────────────────
  function isDirty() {
    return JSON.stringify(state.draft) !== JSON.stringify(state.cfg);
  }

  function onDraftChanged() {
    paintPills();
    const dirty = isDirty();
    $("#saveBtn").disabled = !dirty;
    $("#discardBtn").disabled = !dirty;
    const hint = $("#saveHint");
    hint.textContent = dirty ? "有未保存的修改" : "已保存";
    hint.classList.toggle("is-dirty", dirty);
    hint.classList.remove("is-done");
    schedulePreview();
  }

  async function save() {
    $("#saveBtn").disabled = true;
    $("#saveBtn").textContent = "保存中…";
    try {
      const res = await apiPost("config", { config: state.draft });
      state.cfg = clone(res.config || state.draft);
      state.draft = clone(state.cfg);
      paintAllControls();
      const hint = $("#saveHint");
      hint.textContent = "已保存并生效";
      hint.classList.add("is-done");
      hint.classList.remove("is-dirty");
      toast("已保存，下一句话就按新的来");
      for (const w of res.warnings || []) toast(w, true);
      refreshPreview();
    } catch (e) {
      toast("保存失败：" + e.message, true);
      $("#saveHint").textContent = "保存失败";
    } finally {
      $("#saveBtn").textContent = "保存并生效";
      $("#saveBtn").disabled = !isDirty();
      $("#discardBtn").disabled = !isDirty();
    }
  }

  function discard() {
    state.draft = clone(state.cfg);
    paintAllControls();
    $("#saveHint").textContent = "已保存";
    $("#saveHint").classList.remove("is-dirty", "is-done");
    $("#saveBtn").disabled = true;
    $("#discardBtn").disabled = true;
    refreshPreview();
  }

  // ── 启动 ──────────────────────────────────────────────
  function bindEvents() {
    for (const btn of $$(".nav-item")) {
      btn.addEventListener("click", () => setTab(btn.dataset.tab));
    }
    for (const el of $$("[data-path]")) {
      const evt = el.type === "range" || el.type === "checkbox" ? "input" : "input";
      el.addEventListener(evt, () => {
        let val;
        if (el.type === "checkbox") val = el.checked;
        else if (el.type === "range") val = Number(el.value);
        else val = el.value;
        setPath(state.draft, el.dataset.path, val);
        paintControl(el);
        paintAxes();
        onDraftChanged();
      });
    }
    for (const btn of $$("[data-example]")) {
      btn.addEventListener("click", () => {
        const ta = $('textarea[data-path="prompt_extra"]');
        const insert = btn.dataset.example;
        const cur = ta.value.trim();
        ta.value = cur ? `${cur}\n${insert}` : insert;
        ta.dispatchEvent(new Event("input"));
        ta.focus();
      });
    }
    for (const link of $$("[data-goto]")) {
      link.addEventListener("click", (e) => {
        e.preventDefault();
        setTab(link.dataset.goto);
      });
    }
    $("#saveBtn").addEventListener("click", save);
    $("#discardBtn").addEventListener("click", discard);
    $("#refreshState").addEventListener("click", refreshState);
    window.addEventListener("hashchange", () => setTab(location.hash.slice(1)));
    document.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        if (!isDirty()) return;
        save();
      }
    });
  }

  async function boot() {
    paintIcons();
    bindEvents();

    const bridge = hasBridge();
    state.demo = !bridge;
    const page = window.AstrBotPluginPage;

    if (state.demo) {
      state.cfg = clone(DEMO.config);
      state.draft = clone(DEMO.config);
      state.meta = clone(DEMO.meta);
      state.sessions = DEMO.sessions;
      for (const el of $$("[data-bind='version']")) el.textContent = DEMO.meta.version;
      $("#railHint").textContent = "界面预览 · 未连接面板";
      $("#railHint").classList.add("is-bad");
    }

    setTab(location.hash.slice(1) || "scope");
    paintAllControls();
    paintSessions(state.sessions);
    refreshPreview();

    if (state.demo) return;

    try {
      await page.ready();
      const ctx = page.getContext();
      if (ctx && typeof ctx.isDark === "boolean") {
        document.documentElement.setAttribute("data-theme", ctx.isDark ? "dark" : "light");
      }
      const res = await apiGet("config");
      state.cfg = clone(res.config || {});
      state.draft = clone(res.config || {});
      state.meta = res.meta || {};
      state.sessions = res.sessions || [];
      for (const el of $$("[data-bind='version']")) el.textContent = state.meta.version || "–";
      paintAllControls();
      paintSessions(state.sessions);
      refreshPreview();
      $("#railHint").textContent = "已连接 · 改动即时生效";
      $("#railHint").classList.add("is-ok");
    } catch (e) {
      $("#railHint").textContent = "读取配置失败：" + e.message;
      $("#railHint").classList.add("is-bad");
      toast("读取配置失败：" + e.message, true);
    }

    // 面板切深浅色时跟着变
    if (page && page.onContext) {
      page.onContext((ctx) => {
        if (ctx && typeof ctx.isDark === "boolean") {
          document.documentElement.setAttribute("data-theme", ctx.isDark ? "dark" : "light");
        }
      });
    }

    setInterval(() => {
      if (state.tab === "state" && !state.demo) refreshState();
    }, 15000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
