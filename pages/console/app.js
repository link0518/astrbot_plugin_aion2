/* AION2 查询插件的控制台。
   页面跑在 AstrBot WebUI 的受限 iframe 里，读写一律经由 window.AstrBotPluginPage，
   不直接访问 cookie、localStorage 或父页面 DOM。 */

const bridge = window.AstrBotPluginPage;

const view = document.getElementById("view");
const nav = document.getElementById("nav");
const savebar = document.getElementById("savebar");
const saveInfo = document.getElementById("saveInfo");
const saveBtn = document.getElementById("saveBtn");
const discardBtn = document.getElementById("discardBtn");
const toastEl = document.getElementById("toast");
const regionBadge = document.getElementById("regionBadge");
const clockEl = document.getElementById("clock");

const app = {
  tab: "overview",
  groups: [],
  fieldByKey: new Map(),
  state: null,
  subscriptions: [],
  schedule: null,
  day: "today",
  selftest: null,
  dirty: new Map(),
};

const rowRefs = new Map();

/* --------------------------------------------------------------- 工具 */

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

let toastTimer = 0;

function toast(message, isError = false) {
  toastEl.textContent = message;
  toastEl.classList.toggle("err", Boolean(isError));
  toastEl.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove("show"), 2800);
}

/** 调一次后端接口，失败时提示并返回 null。 */
async function api(method, endpoint, payload) {
  try {
    return method === "GET"
      ? await bridge.apiGet(endpoint, payload)
      : await bridge.apiPost(endpoint, payload);
  } catch (error) {
    toast(error && error.message ? error.message : "请求失败", true);
    return null;
  }
}

function card(title, desc, body, actions = []) {
  const head = el("div", { class: "card-head" }, [
    el("h2", { text: title }),
    el("span", { class: "spacer" }),
    ...actions,
  ]);
  const parts = [head];
  if (desc) parts.push(el("p", { class: "desc", text: desc }));
  parts.push(...[].concat(body));
  return el("div", { class: "card" }, parts);
}

function stat(key, value, note) {
  return el("div", { class: "stat" }, [
    el("div", { class: "k", text: key }),
    el("div", { class: "v" }, [String(value), note ? el("small", { text: note }) : null]),
  ]);
}

function badge(text, tone = "") {
  return el("span", { class: `badge ${tone}`.trim(), text });
}

/* ----------------------------------------------------------- 运行状态 */

function renderOverview() {
  const s = app.state;
  if (!s) return el("div", { class: "empty", text: "没有读到运行状态。" });
  const push = s.push;
  const frag = document.createDocumentFragment();

  frag.append(
    card("运行状态", `读取于 ${s.now}`, [
      el("div", { class: "grid" }, [
        stat("查询区域", s.region.label, s.region.key),
        stat("活动提醒", push.enabled ? "已开启" : "已关闭", `提前 ${push.lead} 分钟`),
        stat("订阅会话", push.subscribers, `今日已推 ${push.sent} 条`),
        stat(
          "结果缓存",
          s.cache.entries,
          `TTL ${s.cache.ttl} 秒 · 译名 ${s.cache.glossary} 条`,
        ),
      ]),
      el("div", { class: "grid", style: "margin-top:10px" }, [
        el("div", { class: "stat" }, [
          el("div", { class: "k", text: "后台提醒循环" }),
          el("div", { class: "v" }, [
            push.running ? badge("运行中", "ok") : badge("未运行", "warn"),
          ]),
        ]),
        el("div", { class: "stat" }, [
          el("div", { class: "k", text: "静默时段" }),
          el("div", { class: "v" }, [
            push.quiet || "无",
            push.quiet && push.quietNow ? el("small", { text: "当前静默中" }) : null,
          ]),
        ]),
        el("div", { class: "stat" }, [
          el("div", { class: "k", text: "提醒内容" }),
          el("div", { class: "v" }, [
            push.kinds.length ? push.kinds.join("、") : badge("未选择", "warn"),
          ]),
        ]),
      ]),
    ])
  );

  const next = s.next.map((item) =>
    el("div", { class: "item" }, [
      el("b", { text: item.name }),
      document.createTextNode(` ${item.clock} `),
      el("small", { text: item.countdown }),
    ])
  );
  frag.append(
    card("接下来的活动", "时区为北京时间，与查询区域无关。", [
      el("div", { class: "rift-list" }, next.length ? next : [el("span", { text: "暂无" })]),
    ])
  );

  frag.append(
    card(
      "维护工具",
      "自检会按当前配置真实请求一次上游；清空缓存后下一次查询会重新取数。",
      [
        el("div", { class: "action-row" }, [
          el("button", { class: "btn", text: "连通性自检", onclick: runSelftest }),
          el("button", {
            class: "btn",
            text: "清空结果缓存",
            onclick: () => clearCache("result"),
          }),
          el("button", {
            class: "btn",
            text: "清空译名缓存",
            onclick: () => clearCache("glossary"),
          }),
        ]),
        selftestNote(),
      ]
    )
  );

  const rows = app.subscriptions.map((row) =>
    el("div", { class: "row" }, [
      el("div", { class: "who" }, [
        el("b", { text: row.label }),
        el("span", { text: row.target + (row.since ? ` · ${row.since} 订阅` : "") }),
      ]),
      el("button", {
        class: "btn tiny",
        text: "测试",
        onclick: () => testSubscription(row.target),
      }),
      el("button", {
        class: "btn tiny",
        text: "移除",
        onclick: () => removeSubscription(row.target),
      }),
    ])
  );
  frag.append(
    card(
      "提醒订阅",
      "只有订阅过的会话才会收到开场提醒，在群里发送「活动 订阅」即可加入。",
      rows.length
        ? el("div", { class: "rows" }, rows)
        : el("div", { class: "empty", text: "还没有会话订阅活动提醒。" })
    )
  );

  return frag;
}

function selftestNote() {
  const result = app.selftest;
  if (!result) return null;
  if (result.ok) {
    return el("div", {
      class: "note ok",
      text: `自检通过：${result.region} 取到 ${result.servers} 个服务器，耗时 ${result.ms} 毫秒。`,
    });
  }
  return el("div", {
    class: "note err",
    text: `自检失败：${result.hint || "未知原因"}${result.detail ? `（${result.detail}）` : ""}`,
  });
}

/* ------------------------------------------------------------- 配置项 */

function currentValue(key) {
  if (app.dirty.has(key)) return app.dirty.get(key);
  const item = app.fieldByKey.get(key);
  return item ? item.value : undefined;
}

function isEnabled(item) {
  return (item.depends || []).every((key) => currentValue(key) !== false);
}

function sameValue(item, a, b) {
  if (item.type === "int" || item.type === "float") return Number(a) === Number(b);
  if (item.type === "bool") return Boolean(a) === Boolean(b);
  return String(a ?? "") === String(b ?? "");
}

function buildControl(item, value, onChange) {
  if (item.type === "bool") {
    const input = el("input", { type: "checkbox" });
    input.checked = Boolean(value);
    input.addEventListener("change", () => onChange(input.checked));
    return el("label", { class: "switch" }, [input, el("span", { class: "track" })]);
  }
  if (item.control === "select") {
    const select = el("select");
    for (const option of item.options || []) {
      const node = el("option", { value: option.value, text: option.label });
      node.selected = String(option.value) === String(value);
      select.append(node);
    }
    select.addEventListener("change", () => onChange(select.value));
    return select;
  }
  if (item.control === "clock") {
    const input = el("input", { type: "text", placeholder: "HH:MM", maxlength: "5" });
    input.value = value ?? "";
    input.addEventListener("change", () => onChange(input.value.trim()));
    return el("div", { class: "ctl-inline" }, [
      input,
      el("span", { class: "unit", text: "留空不静默" }),
    ]);
  }
  if (item.type === "int" || item.type === "float") {
    const input = el("input", {
      type: "number",
      min: item.min,
      max: item.max,
      step: item.step || (item.type === "int" ? 1 : 0.1),
    });
    input.value = value ?? "";
    input.addEventListener("change", () => {
      onChange(input.value === "" ? NaN : Number(input.value));
    });
    return el("div", { class: "ctl-inline" }, [
      input,
      item.unit ? el("span", { class: "unit", text: item.unit }) : null,
    ]);
  }
  const input = el("input", { type: "text" });
  input.value = value ?? "";
  input.addEventListener("change", () => onChange(input.value));
  return input;
}

function fieldRow(item) {
  const label = el("div", { class: "label", text: item.label });
  const ctl = el("div", { class: "ctl" });
  const row = el("div", { class: "field" }, [
    el("div", { class: "meta" }, [
      label,
      item.hint ? el("div", { class: "tip", text: item.hint }) : null,
    ]),
    ctl,
  ]);

  const value = currentValue(item.key);
  const control = buildControl(item, value, (next) => {
    if (sameValue(item, next, item.value)) app.dirty.delete(item.key);
    else app.dirty.set(item.key, next);
    refreshRowState();
  });
  ctl.append(control);
  rowRefs.set(item.key, { row, label, ctl, item });
  return row;
}

function refreshRowState() {
  for (const { row, label, ctl, item } of rowRefs.values()) {
    const dirty = app.dirty.has(item.key);
    label.querySelector(".tag-changed")?.remove();
    if (dirty) label.append(el("span", { class: "tag-changed", text: "已改" }));
    row.classList.toggle("disabled", !isEnabled(item));
    for (const node of ctl.querySelectorAll("input, select")) {
      node.classList.toggle("dirty", dirty);
    }
  }
  const count = app.dirty.size;
  // 有改动就一直显示，切到别的页也不会让人误以为已经保存
  savebar.classList.toggle("show", count > 0);
  saveInfo.innerHTML = count
    ? `有 <b>${count}</b> 项改动待保存${app.tab === "config" ? "" : "（在配置项页）"}`
    : "";
}

function renderConfig() {
  const frag = document.createDocumentFragment();
  if (!app.groups.length) {
    frag.append(
      card("配置项", "", [
        el("div", {
          class: "empty",
          text: "没有读到可配置项，请确认插件目录下的 _conf_schema.json 存在。",
        }),
      ])
    );
    return frag;
  }
  for (const group of app.groups) {
    frag.append(
      card(
        group.label,
        group.hint,
        group.fields.map(fieldRow),
        [
          el("button", {
            class: "btn tiny ghost",
            text: "本组恢复默认",
            onclick: () => resetKeys(group.fields.map((f) => f.key)),
          }),
        ]
      )
    );
  }
  frag.append(
    el("div", {
      class: "note",
      text:
        "改动保存后立即生效：查询区域、限流与缓存会重建连接；提醒相关项会同步到后台循环。" +
        "未保存就离开页面会丢失改动。",
    })
  );
  return frag;
}

/* ----------------------------------------------------------- 时刻表 */

function renderSchedule() {
  const s = app.schedule;
  const frag = document.createDocumentFragment();
  const switcher = el("div", { class: "seg" }, [
    el("button", {
      text: "今日",
      "aria-pressed": String(app.day === "today"),
      onclick: () => loadSchedule("today"),
    }),
    el("button", {
      text: "明日",
      "aria-pressed": String(app.day === "tomorrow"),
      onclick: () => loadSchedule("tomorrow"),
    }),
  ]);

  if (!s) {
    frag.append(card("活动时刻表", "", [el("div", { class: "empty", text: "没有读到时刻表。" })]));
    return frag;
  }

  const upcoming = s.next.map((item) =>
    el("div", { class: "item" }, [
      el("b", { text: item.name }),
      document.createTextNode(` ${item.clock} `),
      el("small", { text: item.countdown }),
    ])
  );
  const rifts = s.rifts.map((item) =>
    el("div", { class: `item ${item.state}`.trim(), text: item.clock })
  );

  frag.append(
    card(`${s.label}活动（${s.day} ${s.weekday}）`, "时间轴按北京时间展开，与查询区域无关。", [
      el("div", { class: "card-head", style: "margin-bottom:10px" }, [switcher]),
      upcoming.length
        ? el("div", { class: "rift-list", style: "margin-bottom:14px" }, upcoming)
        : null,
      timelineBlock(s),
      el("div", { class: "legend" }, [
        el("span", {}, [el("i", { style: "background:var(--rift)" }), "时空裂隙"]),
        el("span", {}, [el("i", { style: "background:var(--game)" }), "小游戏窗口"]),
        el("span", { text: `次元入侵 每小时 :${s.invasionMinute}` }),
      ]),
    ])
  );

  frag.append(
    card("时空裂隙与重置", `每天 ${s.rifts.length} 场，每 3 小时一次。`, [
      el("div", { class: "rift-list", style: "margin-bottom:12px" }, rifts),
      el("div", { class: "legend" }, [
        el("span", { text: `每日重置 ${s.dailyReset}` }),
        el("span", { text: `每周重置 ${s.weeklyReset}` }),
        s.isWeeklyResetDay ? badge("今天就是每周重置日", "accent") : null,
      ]),
    ])
  );

  frag.append(
    card(
      "小游戏组别",
      "每小时 :15 与 :45 各开一场，两组交替；游戏名官方没有中文来源，保留原文。",
      s.sets.map((set) =>
        el("div", { style: "display:flex;gap:10px;align-items:baseline;margin-bottom:8px" }, [
          el("span", { class: "badge", text: set.label }),
          el("div", { class: "chips" }, set.games.map((g) => el("span", { class: "chip", text: g }))),
        ])
      )
    )
  );

  frag.append(el("div", { class: "note", text: s.note }));
  return frag;
}

function timelineBlock(schedule) {
  const columns = schedule.timeline.map((cell) => {
    const bar = el("div", { class: "bar" });
    if (cell.rift) {
      const mark = el("div", { class: `mark rift ${cell.rift_state}`.trim() });
      mark.style.top = "5px";
      bar.append(mark);
    }
    for (const window_ of cell.windows) {
      const mark = el("div", { class: `mark ${window_.state}`.trim() });
      mark.style.left = `calc(${window_.left} - 3px)`;
      mark.style.top = "30px";
      mark.style.width = "6px";
      mark.style.height = "12px";
      bar.append(mark);
    }
    return el("div", { class: `col ${cell.rift ? "peak" : ""}`.trim() }, [
      bar,
      el("div", { class: "hour", text: cell.hour }),
    ]);
  });
  return el("div", { class: "tl-scroll" }, el("div", { class: "tl" }, columns));
}

/* --------------------------------------------------------------- 动作 */

async function runSelftest() {
  const result = await api("POST", "selftest", {});
  if (!result) return;
  app.selftest = result;
  if (result.state) app.state = result.state;
  render();
  toast(result.ok ? `自检通过，耗时 ${result.ms} 毫秒` : "自检失败", !result.ok);
}

async function clearCache(scope) {
  const result = await api("POST", "cache/clear", { scope });
  if (!result) return;
  if (result.state) app.state = result.state;
  render();
  toast(scope === "glossary" ? "译名缓存已清空" : `已清空 ${result.removed} 条结果缓存`);
}

async function removeSubscription(target) {
  const result = await api("POST", "subscriptions/remove", { target });
  if (!result) return;
  app.subscriptions = result.subscriptions || [];
  if (result.state) app.state = result.state;
  render();
  toast("已移除该订阅");
}

async function testSubscription(target) {
  const result = await api("POST", "subscriptions/test", { target });
  if (result) toast("测试消息已发出");
}

async function save() {
  if (!app.dirty.size) return;
  const values = {};
  for (const [key, value] of app.dirty) {
    const item = app.fieldByKey.get(key);
    if (item.type !== "bool" && typeof value === "number" && !Number.isFinite(value)) {
      toast(`「${item.label}」不是有效的数字`, true);
      return;
    }
    values[key] = value;
  }
  saveBtn.disabled = true;
  const result = await api("POST", "config", { values });
  saveBtn.disabled = false;
  if (!result) return;
  app.dirty.clear();
  app.groups = result.groups || app.groups;
  app.fieldByKey = fieldIndex(app.groups);
  if (result.state) app.state = result.state;
  render();
  toast(
    result.changed && result.changed.length
      ? `已保存并生效：${result.changed.length} 项`
      : "没有实际变化"
  );
  if (result.changed && result.changed.includes("free_trigger")) {
    toast("触发方式已变更，无需重启插件");
  }
}

async function resetKeys(keys) {
  const result = await api("POST", "reset", { keys });
  if (!result) return;
  app.dirty.clear();
  app.groups = result.groups || app.groups;
  app.fieldByKey = fieldIndex(app.groups);
  if (result.state) app.state = result.state;
  render();
  toast(`已恢复 ${result.reset.length} 项为默认值`);
}

async function loadSchedule(day) {
  const result = await api("GET", "schedule", { day });
  if (!result) return;
  app.day = day;
  app.schedule = result;
  render();
}

/* --------------------------------------------------------------- 渲染 */

function fieldIndex(groups) {
  const index = new Map();
  for (const group of groups) for (const field of group.fields) index.set(field.key, field);
  return index;
}

/** 顶部只显示到分钟，完整时间留在状态卡片里。 */
function shortClock(text) {
  const match = /^\d{4}-(\d{2})-(\d{2})[ T](\d{2}:\d{2})/.exec(text || "");
  return match ? `${match[1]}-${match[2]} ${match[3]}` : text || "";
}

function render() {
  rowRefs.clear();
  const viewNode =
    app.tab === "config"
      ? renderConfig()
      : app.tab === "schedule"
        ? renderSchedule()
        : renderOverview();
  view.replaceChildren(viewNode);
  if (app.state) {
    clockEl.textContent = shortClock(app.state.now);
    clockEl.title = app.state.now;
    regionBadge.hidden = false;
    regionBadge.textContent = app.state.region.label;
  }
  refreshRowState();
}

function applyOverview(data) {
  app.groups = data.groups || [];
  app.fieldByKey = fieldIndex(app.groups);
  app.state = data.state || null;
  app.subscriptions = data.subscriptions || [];
  app.schedule = data.schedule || null;
  app.day = "today";
  app.dirty.clear();
}

function switchTab(tab) {
  app.tab = tab;
  for (const button of nav.querySelectorAll("button")) {
    button.setAttribute("aria-current", String(button.dataset.tab === tab));
  }
  render();
}

/** 定时只刷新顶部状态，不碰正在编辑的表单。 */
async function tick() {
  if (!bridge || app.tab !== "overview") return;
  let result = null;
  try {
    result = await bridge.apiGet("state");
  } catch (error) {
    return;
  }
  if (!result || !result.state) return;
  app.state = result.state;
  render();
}

async function boot() {
  if (!bridge) {
    view.replaceChildren(
      card("无法加载", "", [
        el("div", {
          class: "empty",
          text: "没有找到 AstrBot 的页面通信接口，请从插件详情页打开本页面。",
        }),
      ])
    );
    return;
  }

  const context = await bridge.ready();
  if (context && context.pageTitle) document.title = context.pageTitle;

  nav.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (button) switchTab(button.dataset.tab);
  });
  saveBtn.addEventListener("click", save);
  discardBtn.addEventListener("click", () => {
    app.dirty.clear();
    render();
    toast("已放弃改动");
  });

  const data = await api("GET", "overview", {});
  if (!data) return;
  applyOverview(data);
  render();
  setInterval(tick, 60000);
}

boot();
