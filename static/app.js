"use strict";
const tg = window.Telegram?.WebApp;
const view = document.getElementById("view");
const drafts = {};
let S = null, tab = "swap", adminTab = "orders", openOrder = null, busy = false;
let skew = 0, signature = "", requestKey = newKey(), version = 0;

function newKey() { return crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random(); }

// ---------- помощники: только textContent, никакого innerHTML ----------
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  el.append(...children.flat().filter((c) => c !== null && c !== false && c !== undefined));
  return el;
}
const card = (...c) => h("div", { class: "card" }, ...c);
const muted = (t) => h("div", { class: "muted" }, t);
const title = (t) => h("h2", {}, t);
const kv = (k, v) => h("div", { class: "kv" }, h("span", {}, k), h("span", {}, String(v)));
const pill = (t, c = "") => h("span", { class: "pill " + c }, t);
const btn = (text, cls, fn, extra = {}) => h("button", { class: cls, type: "button", onclick: fn, ...extra }, text);

function fmt(units) {
  const sign = units < 0 ? "-" : "", n = Math.abs(units), frac = n % 100;
  const whole = String(Math.floor(n / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  return sign + whole + (frac ? "," + String(frac).padStart(2, "0") : "");
}
function parseUnits(text) {
  const t = String(text || "").replace(/\s/g, "").replace(",", ".");
  if (!/^\d{1,10}(\.\d{1,2})?$/.test(t)) return null;
  const [a, b = ""] = t.split(".");
  return Number(a) * 100 + Number(b.padEnd(2, "0"));
}
const when = (ts) => new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
const who = (u) => (u.username ? "@" + u.username : u.name || "ID " + u.id);

function field(key, label, attrs = {}, initial = "") {
  if (!(key in drafts) && initial !== "") attrs.value = initial;
  const input = h("input", { "data-key": key, autocomplete: "off", ...attrs });
  if (key in drafts) input.value = drafts[key];
  input.addEventListener("input", () => { drafts[key] = input.value; input.dispatchEvent(new CustomEvent("draft")); });
  return h("label", {}, label, input);
}
const val = (key, fallback = "") => (key in drafts ? drafts[key] : fallback);

function toast(text, error = false) {
  const t = document.getElementById("toast");
  t.textContent = text; t.className = error ? "err" : ""; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.hidden = true), 4500);
}
function ask(text) {
  return new Promise((resolve) => {
    if (tg?.initData && tg.isVersionAtLeast?.("6.2")) tg.showConfirm(text.slice(0, 256), resolve);
    else resolve(window.confirm(text));
  });
}

// ---------- API ----------
async function api(path, body) {
  const res = await fetch("/api/" + path, {
    method: body ? "POST" : "GET",
    headers: { "X-Init-Data": tg?.initData || "", "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({ error: "Сервер не отвечает" }));
  if (!res.ok) { const e = new Error(data.error || "Ошибка"); e.status = res.status; throw e; }
  return data;
}
function apply(state) {
  S = state; skew = state.now - Date.now() / 1000;
  const sig = JSON.stringify({ ...state, now: 0 });
  if (sig !== signature) { signature = sig; render(); }
}
async function act(name, body, success) {
  if (busy) return null;
  busy = true; version++;
  document.body.style.cursor = "progress";
  try {
    const data = await api(name, body);
    if (success) toast(success);
    tg?.HapticFeedback?.notificationOccurred?.("success");
    signature = ""; apply(data.state);
    return data;
  } catch (e) {
    if (e.message === "RATE_CONFIRM") {
      busy = false;
      if (await ask("Курс меняется больше чем на 20%. Точно сохранить?"))
        return act(name, { ...body, values: { ...body.values, confirm_rate: true } }, success);
      return null;
    }
    toast(e.message, true);
    tg?.HapticFeedback?.notificationOccurred?.("error");
    refresh();
    return null;
  } finally { busy = false; document.body.style.cursor = ""; }
}
async function refresh() {
  const v = version;
  try {
    const state = await api("state");
    // Не перерисовываем форму, пока в ней печатают (кроме чата: там нужны новые сообщения).
    const key = document.activeElement?.dataset?.key;
    if (S && key && !key.startsWith("msg")) return;
    if (v === version) apply(state);  // не перетираем более свежий ответ действия
  } catch (e) {
    if (!S) view.replaceChildren(card(title("Не удалось открыть"), muted(e.message)));
  }
}

// ---------- экраны ----------
const ORDER_STATUS = { new: ["Ждёт менеджера", "blue"], active: ["В работе", "amber"], done: ["Завершена", "green"], cancelled: ["Отменена", "red"] };
const PAY_STATUS = { sent: ["Ждём ответ", "amber"], received: ["Пришло", "green"], disputed: ["Спор", "red"], lost: ["Не дошла", "red"], void: ["Отменена", ""] };
const DEP_STATUS = { queued: ["В очереди", "blue"], processing: ["Проверяется", "amber"], credited: ["Зачислен", "green"], failed: ["Не принят", "red"], review: ["На проверке", "amber"] };

const ICONS = {
  swap: "M7 4 3 8l4 4M3 8h14M17 20l4-4-4-4M21 16H7",
  plus: "M12 5v14M5 12h14",
  list: "M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01",
  gear: "M4 7h10M18 7h2M4 17h2M10 17h10M16 4.5v5M8 14.5v5",
  gift: "M4 11h16v9H4zM3 7h18v4H3zM12 7v13M12 7C9 7 7 6 7 4.5S9.5 2.5 12 7c2.5-4.5 5-4 5-2.5S15 7 12 7z",
  empty: "M4 13l2.5-8h11L20 13v6H4zM4 13h5l1 2h4l1-2h5",
};
function icon(name) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("class", "icon"); svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(NS, "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}
const empty = (text) => h("div", { class: "empty" }, icon("empty"), muted(text));
function bar(parts, cls = "bar") {
  const el = h("div", { class: cls });
  for (const [share, color] of parts) {
    const seg = h("i", { class: color });
    seg.style.width = Math.max(0, Math.min(100, share * 100)) + "%";
    el.append(seg);
  }
  return el;
}
function copyRow(label, value) {
  return h("button", { class: "copy", type: "button", onclick: async () => {
    try { await navigator.clipboard.writeText(value); toast("Скопировано: " + label.toLowerCase()); }
    catch { toast("Не удалось скопировать, выделите вручную", true); }
  } }, h("span", {}, label), h("b", {}, value), h("em", {}, "копировать"));
}

function swapScreen() {
  const s = S.settings, rate = Number(s.rate), bal = S.user.balance, min = +s.min_usd;
  const out = h("div", { class: "big accent" });
  const hint = h("div", { class: "hint" });
  const submit = btn("Обменять", "primary wide", createOrder, { id: "swap-btn" });
  const amount = field("usd", "Отдаёте, $", { inputmode: "decimal", placeholder: "0", class: "amount" });
  function recalc() {
    const usd = parseUnits(val("usd"));
    out.textContent = (usd && rate ? fmt(Math.floor((usd * rate) / 100)) : "0") + " " + s.currency;
    let problem = "";
    if (!S.open) problem = S.closed_reason;
    else if (usd === null) problem = val("usd") ? "Введите число" : "";
    else if (usd < min) problem = `Минимальный ордер ${fmt(min)} $`;
    else if (usd > +s.max_usd) problem = `Максимальный ордер ${fmt(+s.max_usd)} $`;
    else if (usd > bal) problem = "Недостаточно средств, пополните баланс чеком";
    hint.textContent = problem;
    submit.disabled = !S.open || !usd || !!problem;
  }
  amount.querySelector("input").addEventListener("draft", recalc);
  recalc();
  const chip = (text, units) => btn(text, "chip", () => { drafts.usd = String(units / 100); render(); });
  return [
    h("section", { class: "hero" }, h("div", { class: "muted" }, "💎 Ваш баланс"),
      h("div", { class: "huge" }, fmt(bal), h("small", {}, " $")),
      h("div", { class: "between" }, h("span", { class: "rate" }, rate ? `1 $ = ${fmt(rate)} ${s.currency}` : "Курс не задан"),
        btn("＋ Пополнить", "light", () => go("deposit")))),
    !S.open && h("div", { class: "notice" }, h("b", {}, "Обмен сейчас закрыт"), muted(S.closed_reason)),
    card(amount,
      h("div", { class: "chips" }, chip(`${fmt(min)} $`, min), chip("50 $", 5000), chip("100 $", 10000), chip("Всё", bal)),
      h("div", { class: "result" }, muted("Вы получите"), out)),
    card(title("🏦 Куда перевести"),
      field("phone", "Номер телефона", { type: "tel", placeholder: "+7 900 000-00-00", maxlength: 24 }),
      field("bank", "Банк", { placeholder: "Название банка", maxlength: 60 }),
      field("fio", "ФИО получателя", { placeholder: "Иванов Иван Иванович", maxlength: 100 })),
    hint, submit,
  ];
}
async function createOrder() {
  const rate = S.settings.rate, usd = parseUnits(val("usd"));
  const total = fmt(Math.floor((usd * rate) / 100));
  const ok = await ask(`Обменять ${fmt(usd)} $ → ${total} ${S.settings.currency}\n${val("bank")}, ${val("phone")}\n${val("fio")}`);
  if (!ok) return;
  const data = await act("order.create", { usd: val("usd"), phone: val("phone"), bank: val("bank"), fio: val("fio"), rate, key: requestKey }, "Заявка отправлена");
  if (data) { delete drafts.usd; requestKey = newKey(); tab = "orders"; openOrder = { id: data.order_id, mode: "client" }; render(); }
}

function depositScreen() {
  const step = (n, text) => h("div", { class: "step" }, h("b", {}, n), h("span", {}, text));
  return [
    card(title("🧾 Пополнить чеком"),
      step("1", "Создайте чек в @send или @xrocket в USDT"),
      step("2", "Без пароля, подписок и других условий"),
      step("3", "Вставьте ссылку — деньги придут на баланс"),
      field("link", "Ссылка на чек", { placeholder: "https://t.me/send?start=CQ…", inputmode: "url" }),
      !S.deposits_ready && h("div", { class: "notice" }, "Приём чеков временно недоступен"),
      btn("Проверить и зачислить", "primary wide", async () => {
        if (await act("deposit", { link: val("link") }, "Чек принят в проверку")) { delete drafts.link; render(); }
      }, { disabled: !S.deposits_ready })),
    card(title("📂 Мои чеки"), S.deposits.length ? S.deposits.map((d) =>
      h("div", { class: "line" }, h("div", {}, h("b", {}, d.provider === "xrocket" ? "xRocket" : "CryptoBot"), muted(`#${d.id} · ${when(d.created)}`),
        d.status === "failed" && d.note ? muted(d.note) : null),
        pill(DEP_STATUS[d.status][0] + (d.status === "credited" ? ` +${fmt(d.usd)} $` : ""), DEP_STATUS[d.status][1]))) : empty("Здесь появятся ваши чеки")),
  ];
}

function bonusScreen() {
  const r = S.referral;
  const step = (e, text) => h("div", { class: "step" }, h("b", {}, e), h("span", {}, text));
  return [
    h("section", { class: "hero" }, h("div", { class: "muted" }, "🎁 Реферальная программа"),
      h("div", { class: "huge" }, fmt(r.percent), h("small", {}, " %")),
      h("div", {}, "с каждого обмена приглашённого — на ваш баланс")),
    h("div", { class: "grid" }, h("div", { class: "card center" }, h("div", { class: "mid" }, String(r.invited)), muted("👥 Приглашено")),
      h("div", { class: "card center" }, h("div", { class: "mid accent" }, fmt(r.earned) + " $"), muted("💸 Заработано"))),
    card(title("🔗 Ваша ссылка"), r.link ? [copyRow("Ссылка", r.link),
      btn("📨 Поделиться", "primary wide", () => {
        const url = "https://t.me/share/url?url=" + encodeURIComponent(r.link) + "&text=" + encodeURIComponent("💎 Обмен чеков в TwaragaSwap");
        if (tg?.openTelegramLink) tg.openTelegramLink(url); else window.open(url, "_blank", "noopener");
      })] : muted("Ссылка появится через минуту")),
    card(title("✨ Как это работает"), step("1", "Отправьте ссылку другу"), step("2", "Он впервые запускает бота по ней"), step("3", "С каждого его обмена вы получаете бонус")),
  ];
}

function broadcastScreen() {
  const b = S.admin.broadcast, running = b?.status === "running";
  const input = h("textarea", { "data-key": "bc_text", rows: 7, maxlength: 3500, placeholder: "🔥 Новый курс уже в приложении!" });
  input.value = val("bc_text");
  input.addEventListener("input", () => (drafts.bc_text = input.value));
  const check = h("input", { type: "checkbox" });
  check.checked = val("bc_button", true);
  check.addEventListener("change", () => (drafts.bc_button = check.checked));
  const body = () => ({ text: val("bc_text"), button: val("bc_button", true) });
  return [
    card(title("📣 Новая рассылка"), h("label", {}, "Текст сообщения", input),
      h("label", { class: "check" }, check, "Кнопка «Открыть TwaragaSwap»"),
      btn("👀 Отправить себе для проверки", "ghost", () => act("admin.broadcast_test", body(), "Отправлено вам в бот")),
      btn("🚀 Разослать всем", "primary wide", async () => {
        if (await ask(`Отправить сообщение всем пользователям (${S.admin.totals.users})?`))
          if (await act("admin.broadcast", body(), "Рассылка запущена")) { delete drafts.bc_text; render(); }
      }, { disabled: running }),
      muted("Фото или видео: отправьте боту команду /broadcast.")),
    b ? card(h("div", { class: "between" }, title(`Рассылка #${b.id}`), pill({ running: "Идёт", done: "Завершена", stopped: "Остановлена" }[b.status], running ? "amber" : "green")),
      bar([[b.total ? (b.sent + b.failed) / b.total : 1, "green"]]),
      kv("✅ Доставлено", b.sent), kv("🚫 Не доставлено", b.failed), kv("👥 Получателей", b.total),
      running ? btn("Остановить", "red", () => act("admin.broadcast_stop", {}, "Остановлена")) : null) : null,
  ];
}

function referralAdmin() {
  const s = S.admin.settings, on = s.ref_enabled === "1";
  return [card(h("div", { class: "between" }, title("🎁 Реферальная программа"), pill(on ? "Включена" : "Выключена", on ? "green" : "red")),
    muted("Когда выключена, раздел полностью скрыт у пользователей, бонусы не начисляются и новые приглашения не засчитываются. Статистика сохраняется."),
    btn(on ? "Выключить" : "Включить", on ? "red" : "green", () => act("admin.settings", { values: { ref_enabled: !on } }, on ? "Выключена" : "Включена")),
    field("ref_percent", "Процент с обмена приглашённого (0,01–10)", { inputmode: "decimal" }, String(s.ref_percent / 100)),
    btn("Сохранить процент", "primary", async () => {
      if (await act("admin.settings", { values: { ref_percent: val("ref_percent", String(s.ref_percent / 100)) } }, "Сохранено")) { delete drafts.ref_percent; signature = ""; render(); }
    }))];
}

function emojiCard() {
  const name = S.admin.settings.emoji_pack;
  return card(title("✨ Премиум эмодзи в боте"),
    name ? [kv("Набор", name), kv("Эмодзи в наборе", S.admin.emoji_count)] : muted("Сейчас используются обычные эмодзи."),
    field("emoji_link", "Ссылка на набор эмодзи", { placeholder: "t.me/addemoji/название" }),
    muted("Нужен Telegram Premium у владельца бота. Заменяются эмодзи, которые есть в наборе."),
    btn("Применить набор", "primary", async () => {
      if (!val("emoji_link").trim()) return toast("Вставьте ссылку на набор", true);
      const data = await act("admin.emoji", { link: val("emoji_link") }, "Набор подключён");
      if (data) { delete drafts.emoji_link; render(); }
    }),
    name ? btn("Вернуть обычные", "ghost", () => act("admin.emoji", { link: "" }, "Готово")) : null);
}

function orderBar(o) {
  return bar([[o.received / o.total, "green"], [(o.given - o.received) / o.total, "amber"]]);
}
function orderList(orders, mode) {
  if (!orders.length) return [card(empty("Заявок пока нет"))];
  return orders.map((o) => h("div", { class: "card item " + o.status, onclick: () => { openOrder = { id: o.id, mode }; render(); scrollTo(0, 0); } },
    h("div", { class: "between" }, h("b", {}, `#${o.id}` + (mode === "staff" ? " · " + who(o.client) : "")), pill(...ORDER_STATUS[o.status])),
    h("div", { class: "between" }, h("div", { class: "mid" }, `${fmt(o.total)} ${o.currency}`), muted(`${fmt(o.usd)} $ · ${when(o.created)}`)),
    o.status === "active" ? [orderBar(o), h("div", { class: "left-line" }, `🧮 Остаток: ${fmt(o.left)} ${o.currency}`)] : null,
    o.refunded ? muted(`↩ Возвращено на баланс ${fmt(o.refunded)} $`) : null,
    o.payments.some((p) => p.status === "disputed") && o.status === "active" ? pill("Спор: не пришло", "red") : null));
}

function steps(o) {
  const at = { new: 0, active: o.given ? 2 : 1, done: 3 }[o.status];
  if (at === undefined) return null;
  return h("div", { class: "track" }, ["Создана", "Принята", "Выплата", "Готово"].map((name, i) =>
    h("div", { class: i <= at && (i !== 2 || o.given) ? "on" : "" }, h("i", {}), h("span", {}, name))));
}

function orderScreen(o, mode) {
  const staff = mode === "staff" && o.manages;
  const active = o.status === "active";
  const left = o.left;
  const out = [
    btn("‹ Назад", "back", () => { openOrder = null; render(); }),
    h("section", { class: "hero " + o.status }, h("div", { class: "between" }, h("b", {}, `Заявка #${o.id}`), pill(...ORDER_STATUS[o.status])),
      h("div", { class: "huge" }, fmt(o.total), h("small", {}, " " + o.currency)),
      muted(`${fmt(o.usd)} $ по курсу ${fmt(o.rate)}`),
      orderBar(o),
      h("div", { class: "between" }, muted(`Подтверждено ${fmt(o.received)}`), muted(`Выдано ${fmt(o.given)}`)),
      h("div", { class: "refund between" }, h("span", {}, o.status === "done" || o.status === "cancelled" ? "🧮 Не выдано" : "🧮 Остаток по заявке"), h("span", {}, `${fmt(left)} ${o.currency}`)),
      o.refunded ? h("div", { class: "refund" }, `↩ Возвращено на баланс: ${fmt(o.refunded)} $`) : null,
      steps(o)),
  ];
  for (const p of o.payments) {
    const open = active && (p.status === "sent" || p.status === "disputed");
    const timeBar = h("div", { class: "bar thin", "data-bar": p.deadline, "data-span": p.deadline - p.created }, h("i", { class: "amber" }));
    const status = p.status === "sent" && o.status === "done" ? ["Засчитана без ответа", "blue"] : PAY_STATUS[p.status];
    out.push(h("div", { class: "card pay " + p.status },
      h("div", { class: "between" }, h("div", { class: "mid" }, `${fmt(p.amount)} ${o.currency}`), pill(...status)),
      muted(when(p.created)),
      open && p.status === "sent" ? [h("div", { class: "between" }, muted("Время на перевод"), h("span", { class: "timer", "data-deadline": p.deadline })), timeBar] : null,
      p.status === "disputed" && open && !staff ? [
        muted("Ваш код проверки"), h("div", { class: "code" }, [...p.video_code].map((d) => h("b", {}, d))),
        muted("Запишите видео экрана одним дублем и отправьте его в чат с ботом:"),
        h("ol", { class: "steps" }, ...["Покажите этот код.", "Откройте многозадачность и из неё перейдите в банк.",
          "Покажите номер телефона и ФИО в профиле.", "Откройте платежи и историю операций.", "Несколько раз обновите историю."].map((t) => h("li", {}, t))),
        muted("⚠️ Не показывайте пароли, коды из СМС, полный номер карты и CVV."),
        p.video_received ? pill("Видео получено", "green") : btn("Перейти в чат с ботом", "blue", () => tg?.close()),
      ] : null,
      p.status === "disputed" && staff ? [kv("Код на видео", p.video_code), kv("Видео", p.video_received ? "получено, смотрите в чате бота" : "ещё не прислано")] : null,
      open && o.mine && !staff ? h("div", { class: "row" },
        btn("✓ Пришло", "green", async () => { if (await ask(`Деньги ${fmt(p.amount)} ${o.currency} точно на счёте? Отменить ответ нельзя.`)) act("payment.received", { id: p.id }, "Подтверждено"); }),
        p.status === "sent" ? btn("✕ Не пришло", "red", () => act("payment.not_received", { id: p.id }), { "data-unlock": p.deadline }) : null) : null,
      open && staff && p.status === "disputed" ? [
        btn("✕ Деньги не пришли", "red wide", async () => { if (await ask(`Подтвердить: ${fmt(p.amount)} ${o.currency} НЕ пришли? Сумма вернётся в остаток заявки.`)) act("payment.lost", { id: p.id }, "Спор решён"); }),
        btn("✓ Деньги пришли", "green wide", async () => { if (await ask(`Подтвердить: ${fmt(p.amount)} ${o.currency} пришли? Выплата будет засчитана.`)) act("payment.came", { id: p.id }, "Спор решён"); })] : null,
      open && staff && p.status === "sent" ? btn("Отменить эту выплату", "ghost", async () => {
        if (await ask("Отменять выплату можно, только если деньги НЕ отправлялись. Отменить?")) act("payment.void", { id: p.id }, "Выплата отменена");
      }) : null));
  }
  out.push(card(title("📱 Реквизиты"),
    staff ? [copyRow("Телефон", o.phone), copyRow("Банк", o.bank), copyRow("ФИО", o.fio)] : [kv("Телефон", o.phone), kv("Банк", o.bank), kv("ФИО", o.fio)],
    staff ? kv("Клиент", `${who(o.client)} (${o.client.id})`) : null,
    staff && o.manager ? kv("Менеджер", who(o.manager)) : null));
  if (staff && o.status === "new") {
    out.push(h("div", { class: "row" },
      btn("✓ Принять", "green", () => act("order.accept", { id: o.id }, "Заявка принята")),
      btn("✕ Отклонить", "red", async () => { if (await ask("Отклонить и вернуть деньги клиенту?")) act("order.cancel", { id: o.id }, "Отклонена"); })));
  }
  if (staff && active) {
    const key = "pay" + o.id;
    if (left > 0) out.push(card(title("💸 Дать сумму"),
      field(key, `Остаток: ${fmt(left)} ${o.currency}`, { inputmode: "decimal", placeholder: String(Math.floor(left / 100)), class: "amount" }),
      btn("💸 Отправил, запустить таймер", "blue wide", async () => {
        const units = parseUnits(val(key));
        if (!units) return toast("Ошибка: введите число, например 30000", true);
        if (units > left) return toast(`Ошибка: слишком большая сумма. Остаток ${fmt(left)} ${o.currency}, вы ввели ${fmt(units)}`, true);
        if (await ask(`Выдать ${fmt(units)} ${o.currency}?\n${o.bank}, ${o.phone}\n${o.fio}`))
          if (await act("order.pay", { id: o.id, amount: val(key) }, "Клиенту отправлен запрос")) { delete drafts[key]; render(); }
      })));
    const f = o.finish;
    out.push(card(title("🏁 Завершение"),
      f.blocked ? muted(f.blocked) : [kv("Выплачено", `${fmt(f.paid)} ${o.currency}`), kv("Вернётся клиенту на баланс", `${fmt(f.refund)} $`)],
      btn("🏁 Завершить заявку", "green wide", async () => {
        if (f.blocked) return toast(f.blocked, true);
        if (await ask(`Завершить заявку #${o.id}?\nВыплачено: ${fmt(f.paid)} ${o.currency}\nНа баланс клиента вернётся: ${fmt(f.refund)} $`))
          act("order.complete", { id: o.id, expect: f.refund }, "Заявка завершена");
      }),
      !o.given ? btn("Вернуть в общую очередь", "ghost", () => act("order.release", { id: o.id }, "Возвращена")) : null));
  }
  if (!staff && o.mine && o.status === "new") out.push(btn("Отменить заявку", "ghost", async () => {
    if (await ask("Отменить заявку и вернуть деньги на баланс?")) act("order.cancel_own", { id: o.id }, "Отменена");
  }));
  const canChat = (o.status === "new" && !staff) || (active && (staff || o.mine));
  const key = "msg" + o.id;
  const send = async () => { if (val(key).trim() && await act("order.message", { id: o.id, text: val(key) })) { delete drafts[key]; render(); } };
  out.push(card(title("💬 Переписка"),
    h("div", { class: "chat" }, o.messages.length ? o.messages.map((m) =>
      m.kind === "system" ? h("div", { class: "msg system" }, "⚖️ " + m.text, muted(when(m.created))) :
      h("div", { class: "msg" + (Boolean(m.from_staff) === staff ? " own" : "") }, (m.has_file ? "📎 " : "") + m.text, muted((m.from_staff ? "Менеджер" : "Клиент") + " · " + when(m.created)))) : muted("Сообщений пока нет")),
    o.messages.some((m) => m.has_file) ? btn("📎 Прислать вложения в бот", "ghost", () => act("order.files", { id: o.id }, "Отправил в чат с ботом")) : null,
    canChat ? h("div", { class: "composer" }, field(key, "", { maxlength: 2000, placeholder: "Сообщение…", "aria-label": "Сообщение" }), btn("➤", "primary", send, { "aria-label": "Отправить" })) : null));
  return out;
}

function settingsScreen() {
  const s = S.admin.settings;
  const owner = S.user.role === "owner";
  const money = ["rate", "min_usd", "max_usd", "auto_credit_max"];
  const base = (k) => (money.includes(k) ? (s[k] / 100).toString() : s[k]);
  const box = (key, label) => {
    const input = h("input", { type: "checkbox" });
    input.checked = val("set_" + key, s[key]) === "1";
    input.addEventListener("change", () => (drafts["set_" + key] = input.checked ? "1" : "0"));
    return h("label", { class: "check" }, input, label);
  };
  const f = (key, label, attrs = {}) => field("set_" + key, label, attrs, base(key));
  const days = val("set_work_days", s.work_days);
  const save = async (keys) => {
    const values = {};
    for (const k of keys) values[k] = val("set_" + k, base(k));
    if (await act("admin.settings", { values }, "Сохранено")) { for (const k of keys) delete drafts["set_" + k]; signature = ""; render(); }
  };
  return [
    card(title("💱 Курс и лимиты"),
      f("rate", `Курс: сколько ${s.currency} за 1 $`, { inputmode: "decimal" }),
      h("div", { class: "grid" }, f("currency", "Валюта выплаты", { maxlength: 8 }), f("timer_min", "Таймер, минут", { inputmode: "numeric" }),
        f("grace_min", "Ответ после таймера, мин", { inputmode: "numeric" }),
        f("min_usd", "Мин. ордер, $", { inputmode: "decimal" }), f("max_usd", "Макс. ордер, $", { inputmode: "decimal" })),
      owner ? f("auto_credit_max", "Чеки дороже этой суммы — на ручную проверку, $", { inputmode: "decimal" }) : null,
      btn("Сохранить", "primary", () => save(["rate", "currency", "timer_min", "grace_min", "min_usd", "max_usd", ...(owner ? ["auto_credit_max"] : [])]))),
    card(title("🕘 Часы работы"), box("accepting", "Принимать заявки"), box("hours_enabled", "Ограничить по расписанию"),
      h("div", { class: "grid" }, f("hours_from", "С", { type: "time" }), f("hours_to", "До", { type: "time" })),
      f("tz_offset", "Часовой пояс, UTC± (Москва 3, Якутск 9)", { inputmode: "text" }),
      h("div", { class: "tabs" }, ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"].map((name, i) => {
        const d = String(i + 1);
        return btn(name, days.includes(d) ? "on" : "", () => {
          drafts.set_work_days = days.includes(d) ? days.replace(d, "") : days + d; render();
        });
      })),
      btn("Сохранить", "primary", () => save(["accepting", "hours_enabled", "hours_from", "hours_to", "tz_offset", "work_days"]))),
    owner ? emojiCard() : null,
  ];
}

function adminsScreen() {
  return [
    card(title("👑 Администраторы"), S.admin.admins.map((a) => h("div", { class: "between kv" },
      h("div", {}, "@" + a.username, muted(a.is_owner ? "владелец" : "админ"),
        muted(a.user_id ? `ID ${a.user_id} · ${a.name || ""}` : "ещё не заходил: пусть нажмёт /start")),
      a.is_owner ? null : btn("Убрать", "red", async () => { if (await ask(`Убрать @${a.username}?`)) act("admin.remove", { username: a.username }, "Удалён"); })))),
    card(title("Добавить по юзернейму"), field("newadmin", "Юзернейм", { placeholder: "@username" }),
      btn("Добавить", "primary", async () => { if (await act("admin.add", { username: val("newadmin") }, "Добавлен")) { delete drafts.newadmin; render(); } })),
  ];
}

function reviewScreen() {
  if (!S.admin.review.length) return [card(muted("Чеков на проверке нет"))];
  return S.admin.review.map((d) => card(
    h("div", { class: "between" }, title(`Чек #${d.id}`), pill(d.provider, "amber")),
    kv("Клиент", who({ ...d, id: d.user_id })), kv("Причина", d.note || "—"), kv("Прочитано", d.asset ? `${d.asset_amount} ${d.asset}` : "—"),
    muted("Ответ бота: " + (d.reply || "нет")),
    muted("Сверьте с историей в кошельке принимающего аккаунта."),
    field("dep" + d.id, "Зачислить, $", { inputmode: "decimal" }),
    h("div", { class: "row" },
      btn("Зачислить", "green", async () => { if (await ask(`Зачислить ${val("dep" + d.id)} $?`)) act("admin.deposit", { id: d.id, decision: "credit", usd: val("dep" + d.id) }, "Зачислено"); }),
      btn("Отклонить", "red", async () => { if (await ask("Отклонить чек?")) act("admin.deposit", { id: d.id, decision: "reject" }, "Отклонён"); }))));
}

function accountScreen() {
  const a = S.admin.account;
  const blocks = [card(h("div", { class: "between" }, title("Аккаунт для чеков"), pill(a.connected ? "Подключён" : "Не подключён", a.connected ? "green" : "red")),
    a.connected ? [kv("Аккаунт", a.name), btn("Отключить", "red", async () => { if (await ask("Выйти из аккаунта? Приём чеков остановится.")) act("account.logout", {}, "Отключён"); })] : null,
    a.error ? muted(a.error) : null,
    muted("Используйте отдельный аккаунт. Заранее запустите на нём @send и @xrocket и выберите русский или английский язык."))];
  if (a.pending === "password") blocks.push(card(title("Облачный пароль (2FA)"), field("tgpass", "Пароль", { type: "password" }),
    btn("Войти", "primary", async () => { if (await act("account.sign_in", { password: val("tgpass") })) { delete drafts.tgpass; render(); } })));
  else if (a.pending === "code") blocks.push(card(title("Код из Telegram"), field("tgcode", "Код", { inputmode: "numeric" }),
    btn("Подтвердить", "primary", async () => { if (await act("account.sign_in", { code: val("tgcode") })) { delete drafts.tgcode; render(); } })));
  if (!a.connected) blocks.push(card(title(a.pending ? "Начать заново" : "Подключить"),
    muted("api_id и api_hash берутся на my.telegram.org → API development tools." + (a.has_api ? " Уже заданы в переменных, поля можно не заполнять." : "")),
    field("api_id", "api_id", { inputmode: "numeric" }), field("api_hash", "api_hash", { maxlength: 32 }),
    field("tgphone", "Телефон аккаунта", { type: "tel", placeholder: "+7…" }),
    btn("Получить код", "primary", () => act("account.send_code", { api_id: val("api_id"), api_hash: val("api_hash"), phone: val("tgphone") }, "Код отправлен"))));
  return blocks;
}

function balanceScreen() {
  const t = S.admin.totals;
  return [card(title("Балансы клиентов"), kv("Пользователей", t.users), kv("Сумма на балансах", fmt(t.balance) + " $")),
    card(title("Изменить баланс"), field("bal_user", "ID или @username"), field("bal_delta", "Сумма, $ (минус — списать)", { placeholder: "10 или -10" }),
      field("bal_reason", "Причина", { maxlength: 200 }),
      btn("Применить", "primary", async () => {
        if (!await ask(`Изменить баланс ${val("bal_user")} на ${val("bal_delta")} $?`)) return;
        if (await act("admin.balance", { user: val("bal_user"), delta: val("bal_delta"), reason: val("bal_reason") }, "Готово")) {
          delete drafts.bal_user; delete drafts.bal_delta; delete drafts.bal_reason; render();
        }
      }))];
}

function adminScreen() {
  const owner = S.user.role === "owner";
  const tabs = [["orders", "Заявки"], ["settings", "Курс и часы"]];
  if (owner) tabs.push(["review", `Чеки${S.admin.review.length ? " (" + S.admin.review.length + ")" : ""}`], ["broadcast", "Рассылка"], ["referral", "Рефералка"], ["admins", "Админы"], ["account", "Аккаунт"], ["balance", "Балансы"]);
  if (!tabs.some(([k]) => k === adminTab)) adminTab = "orders";
  const screens = { orders: () => orderList(S.admin.orders, "staff"), settings: settingsScreen, review: reviewScreen, broadcast: broadcastScreen, referral: referralAdmin, admins: adminsScreen, account: accountScreen, balance: balanceScreen };
  return [h("div", { class: "tabs" }, tabs.map(([k, name]) => btn(name, k === adminTab ? "on" : "", () => { adminTab = k; render(); }))), ...screens[adminTab]()];
}

function go(name) { tab = name; openOrder = null; render(); scrollTo(0, 0); }

function render() {
  const focus = document.activeElement?.dataset?.key, pos = focus ? document.activeElement.selectionStart : null;
  const status = document.getElementById("status");
  status.textContent = S.open ? "Работаем" : "Закрыто"; status.className = "pill live " + (S.open ? "green" : "red");
  if (tab === "admin" && !S.admin) tab = "swap";
  if (tab === "bonus" && !S.referral) tab = "swap";
  let content;
  const list = openOrder && (openOrder.mode === "staff" ? S.admin?.orders : S.orders);
  const order = list?.find((o) => o.id === openOrder.id);
  if (order) content = orderScreen(order, openOrder.mode);
  else {
    openOrder = null;
    content = { swap: swapScreen, deposit: depositScreen, bonus: bonusScreen, orders: () => orderList(S.orders, "client"), admin: adminScreen }[tab]();
  }
  view.replaceChildren(...content.flat().filter(Boolean));
  // Анимация только при смене экрана, а не при каждом обновлении данных.
  const screen = order ? "order" + order.id : tab + (tab === "admin" ? adminTab : "");
  if (screen !== render.screen) {
    render.screen = screen;
    view.classList.remove("enter"); void view.offsetWidth; view.classList.add("enter");
  }
  const nav = document.getElementById("nav");
  const items = [["swap", "swap", "Обмен"], ["deposit", "plus", "Пополнить"], ["orders", "list", "Заявки"]];
  if (S.referral) items.push(["bonus", "gift", "Бонусы"]);
  if (S.admin) items.push(["admin", "gear", "Админ"]);
  nav.hidden = false;
  nav.replaceChildren(...items.map(([k, glyph, name]) => h("button", { type: "button", class: k === tab ? "on" : "", onclick: () => go(k) }, icon(glyph), h("span", {}, name))));
  if (focus) {
    const el = view.querySelector(`[data-key="${CSS.escape(focus)}"]`);
    if (el) { el.focus({ preventScroll: true }); try { el.setSelectionRange(pos, pos); } catch { /* тип поля без выделения */ } }
  }
  tick();
}
function tick() {
  for (const el of document.querySelectorAll("[data-deadline]")) {
    const left = Math.max(0, Math.round(el.dataset.deadline - (Date.now() / 1000 + skew)));
    el.textContent = left ? `${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}` : "время вышло";
  }
  for (const el of document.querySelectorAll("[data-unlock]")) {
    const left = Math.max(0, Math.ceil(el.dataset.unlock - (Date.now() / 1000 + skew)));
    el.disabled = left > 0;
    el.textContent = left ? `Не пришло · ${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}` : "✕ Не пришло";
  }
  for (const el of document.querySelectorAll("[data-bar]")) {
    const left = el.dataset.bar - (Date.now() / 1000 + skew);
    el.firstChild.style.width = Math.max(0, Math.min(100, (left / el.dataset.span) * 100)) + "%";
  }
}

tg?.ready(); tg?.expand();
try { tg?.setHeaderColor?.("#030807"); tg?.setBackgroundColor?.("#030807"); } catch { /* старый клиент */ }
const wanted = Number(new URLSearchParams(location.search).get("order"));
refresh().then(() => {
  if (!wanted || !S) return;
  const staff = S.admin?.orders.find((o) => o.id === wanted && !o.mine);
  tab = staff ? "admin" : "orders"; openOrder = { id: wanted, mode: staff ? "staff" : "client" }; render();
});
setInterval(tick, 1000);
setInterval(() => { if (!document.hidden && !busy) refresh(); }, 4000);
