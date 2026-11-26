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
const PAY_STATUS = { sent: ["Ждём ответ", "amber"], received: ["Пришло", "green"], disputed: ["Не пришло", "red"], void: ["Отменена", ""] };
const DEP_STATUS = { queued: ["В очереди", "blue"], processing: ["Проверяется", "amber"], credited: ["Зачислен", "green"], failed: ["Не принят", "red"], review: ["На проверке", "amber"] };

function swapScreen() {
  const s = S.settings, rate = Number(s.rate), bal = S.user.balance;
  const out = h("div", { class: "big" });
  const hint = h("div", { class: "muted" });
  const submit = btn("Обменять", "primary", createOrder, { id: "swap-btn" });
  const amount = field("usd", `Сколько долларов обменять (от ${fmt(+s.min_usd)} $)`, { inputmode: "decimal", placeholder: "25" });
  function recalc() {
    const usd = parseUnits(val("usd"));
    out.textContent = (usd && rate ? fmt(Math.floor((usd * rate) / 100)) : "0") + " " + s.currency;
    let problem = "";
    if (!S.open) problem = S.closed_reason;
    else if (usd === null) problem = val("usd") ? "Введите число" : "";
    else if (usd < +s.min_usd) problem = `Минимальный ордер ${fmt(+s.min_usd)} $`;
    else if (usd > +s.max_usd) problem = `Максимальный ордер ${fmt(+s.max_usd)} $`;
    else if (usd > bal) problem = "Недостаточно средств, пополните баланс чеком";
    hint.textContent = problem;
    submit.disabled = !S.open || !usd || !!problem;
  }
  amount.querySelector("input").addEventListener("draft", recalc);
  recalc();
  return [
    h("div", { class: "card hero" }, muted("Ваш баланс"), h("div", { class: "big" }, fmt(bal) + " $"),
      h("div", { class: "between" }, muted(rate ? `Курс: 1 $ = ${fmt(rate)} ${s.currency}` : "Курс не задан"),
        btn("Пополнить", "ghost", () => go("deposit")))),
    !S.open && card(h("div", { class: "between" }, title("Обмен закрыт"), pill("🔴", "red")), muted(S.closed_reason)),
    card(title("Обмен"), amount,
      btn("Весь баланс", "ghost", () => { drafts.usd = (bal / 100).toFixed(2); render(); }),
      h("div", { class: "result" }, muted("Вы получите"), out),
      field("phone", "Номер телефона", { type: "tel", placeholder: "+7 900 000-00-00", maxlength: 24 }),
      field("bank", "Банк", { placeholder: "Название банка", maxlength: 60 }),
      field("fio", "ФИО получателя", { placeholder: "Иванов Иван Иванович", maxlength: 100 }),
      hint, submit),
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
  return [
    card(title("Пополнить чеком"),
      muted("Создайте чек в @send (CryptoBot) или @xrocket в USDT без пароля и без условий, затем вставьте ссылку."),
      field("link", "Ссылка на чек", { placeholder: "https://t.me/send?start=CQ…", inputmode: "url" }),
      !S.deposits_ready && muted("🔴 Приём чеков временно недоступен."),
      btn("Проверить и зачислить", "primary", async () => {
        if (await act("deposit", { link: val("link") }, "Чек принят в проверку")) { delete drafts.link; render(); }
      }, { disabled: !S.deposits_ready })),
    card(title("Мои чеки"), S.deposits.length ? S.deposits.map((d) =>
      h("div", { class: "kv" }, h("span", {}, `#${d.id} · ${d.provider === "xrocket" ? "xRocket" : "CryptoBot"} · ${when(d.created)}`),
        h("span", {}, pill(DEP_STATUS[d.status][0] + (d.status === "credited" ? ` +${fmt(d.usd)} $` : ""), DEP_STATUS[d.status][1]),
          d.status === "failed" && d.note ? muted(d.note) : null))) : muted("Пока пусто")),
  ];
}

function orderList(orders, mode) {
  if (!orders.length) return [card(muted("Заявок пока нет"))];
  return orders.map((o) => h("div", { class: "card item", onclick: () => { openOrder = { id: o.id, mode }; render(); scrollTo(0, 0); } },
    h("div", { class: "between" }, title(`#${o.id}` + (mode === "staff" ? " · " + who(o.client) : "")), pill(...ORDER_STATUS[o.status])),
    h("div", { class: "between" }, h("div", { class: "mid" }, `${fmt(o.total)} ${o.currency}`), muted(`${fmt(o.usd)} $ · ${when(o.created)}`)),
    o.payments.some((p) => p.status === "disputed") && o.status === "active" ? pill("Спор: не пришло", "red") : null));
}

function orderScreen(o, mode) {
  const staff = mode === "staff" && o.manages;
  const active = o.status === "active";
  const left = o.total - o.given;
  const owner = S.user.role === "owner";
  const pending = o.payments.filter((p) => p.status === "sent" || p.status === "disputed");
  const out = [
    btn("← Назад", "ghost", () => { openOrder = null; render(); }),
    card(h("div", { class: "between" }, title(`Заявка #${o.id}`), pill(...ORDER_STATUS[o.status])),
      h("div", { class: "big" }, `${fmt(o.total)} ${o.currency}`),
      muted(`${fmt(o.usd)} $ по курсу ${fmt(o.rate)}`),
      staff ? kv("Клиент", `${who(o.client)} (${o.client.id})`) : null,
      kv("Телефон", o.phone), kv("Банк", o.bank), kv("ФИО", o.fio),
      kv("Выдано", fmt(o.given)), kv("Подтверждено", fmt(o.received)),
      staff && o.manager ? kv("Менеджер", who(o.manager)) : null,
      o.status === "cancelled" ? kv("Возвращено на баланс", fmt(o.refunded) + " $") : null),
  ];
  for (const p of o.payments) {
    const open = active && (p.status === "sent" || p.status === "disputed");
    out.push(card(
      h("div", { class: "between" }, h("div", { class: "mid" }, `${fmt(p.amount)} ${o.currency}`), pill(...PAY_STATUS[p.status])),
      open && p.status === "sent" ? h("div", { class: "between" }, muted("Время на проверку"), h("span", { class: "timer", "data-deadline": p.deadline })) : null,
      p.status === "disputed" && open && !staff ? [
        muted("Ваш код проверки"), h("div", { class: "code" }, p.video_code),
        muted("Запишите видео экрана одним дублем и отправьте его в чат с ботом:"),
        h("ol", { class: "steps" }, ...["Покажите этот код.", "Откройте многозадачность и из неё перейдите в банк.",
          "Покажите номер телефона и ФИО в профиле.", "Откройте платежи и историю операций.", "Несколько раз обновите историю."].map((t) => h("li", {}, t))),
        muted("⚠️ Не показывайте пароли, коды из СМС, полный номер карты и CVV."),
        p.video_received ? pill("Видео получено", "green") : btn("Перейти в чат с ботом", "blue", () => tg?.close()),
      ] : null,
      p.status === "disputed" && staff ? [kv("Код на видео", p.video_code), kv("Видео", p.video_received ? "получено, смотрите в чате бота" : "ещё не прислано")] : null,
      open && o.mine && !staff ? h("div", { class: "row" },
        btn("✅ Пришло", "green", async () => { if (await ask(`Деньги ${fmt(p.amount)} ${o.currency} точно на счёте? Отменить ответ нельзя.`)) act("payment.received", { id: p.id }, "Подтверждено"); }),
        p.status === "sent" ? btn("❌ Не пришло", "red", () => act("payment.not_received", { id: p.id })) : null) : null,
      open && staff && (p.status === "sent" || owner) ? btn("Отменить эту выплату", "ghost", async () => {
        if (await ask("Отменять выплату можно, только если деньги НЕ отправлялись. Отменить?")) act("payment.void", { id: p.id }, "Выплата отменена");
      }) : null));
  }
  if (staff && o.status === "new") {
    out.push(h("div", { class: "row" },
      btn("✅ Принять", "green", () => act("order.accept", { id: o.id }, "Заявка принята")),
      btn("Отклонить", "red", async () => { if (await ask("Отклонить и вернуть деньги клиенту?")) act("order.cancel", { id: o.id }, "Отклонена"); })));
  }
  if (staff && active) {
    const key = "pay" + o.id;
    if (left > 0 && !pending.length) out.push(card(title("Дать сумму"),
      field(key, `Остаток: ${fmt(left)} ${o.currency}`, { inputmode: "decimal", placeholder: String(Math.floor(left / 100)) }),
      btn("💸 Отправил, запустить таймер", "blue", async () => {
        const units = parseUnits(val(key));
        if (!units) return toast("Ошибка: введите число, например 30000", true);
        if (units > left) return toast(`Ошибка: слишком большая сумма. Остаток ${fmt(left)} ${o.currency}, вы ввели ${fmt(units)}`, true);
        if (await ask(`Выдать ${fmt(units)} ${o.currency}?\n${o.bank}, ${o.phone}\n${o.fio}`))
          if (await act("order.pay", { id: o.id, amount: val(key) }, "Клиенту отправлен запрос")) { delete drafts[key]; render(); }
      })));
    out.push(card(
      btn("✅ Завершить заявку", "green", () => act("order.complete", { id: o.id }, "Завершена")),
      btn("Отменить и вернуть остаток", "ghost", async () => { if (await ask("Отменить заявку? Невыплаченная часть вернётся клиенту.")) act("order.cancel", { id: o.id }, "Отменена"); }),
      !o.given ? btn("Вернуть в общую очередь", "ghost", () => act("order.release", { id: o.id }, "Возвращена")) : null,
      owner ? h("div", { class: "row" },
        btn("Владелец: закрыть принудительно", "ghost", async () => { if (await ask("Закрыть как выполненную? Деньги клиенту НЕ вернутся.")) act("order.complete", { id: o.id, force: true }, "Закрыта"); }),
        btn("Владелец: отменить принудительно", "red", async () => { if (await ask("Отменить? Всё, что клиент не подтвердил, вернётся ему на баланс.")) act("order.cancel", { id: o.id, force: true }, "Отменена"); })) : null));
  }
  if (!staff && o.mine && o.status === "new") out.push(btn("Отменить заявку", "ghost", async () => {
    if (await ask("Отменить заявку и вернуть деньги на баланс?")) act("order.cancel_own", { id: o.id }, "Отменена");
  }));
  const canChat = (o.status === "new" && !staff) || (active && (staff || o.mine));
  const key = "msg" + o.id;
  const send = async () => { if (val(key).trim() && await act("order.message", { id: o.id, text: val(key) })) { delete drafts[key]; render(); } };
  out.push(card(title("Переписка"),
    h("div", { class: "chat" }, o.messages.length ? o.messages.map((m) =>
      h("div", { class: "msg" + (Boolean(m.from_staff) === staff ? " own" : "") }, m.text, muted((m.from_staff ? "Менеджер" : "Клиент") + " · " + when(m.created) + (m.kind !== "text" ? " · файл в чате бота" : "")))) : muted("Сообщений нет")),
    canChat ? [field(key, "Сообщение", { maxlength: 2000, placeholder: "Напишите…" }), btn("Отправить", "primary", send)] : null));
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
    card(title("Курс и лимиты"),
      f("rate", `Курс: сколько ${s.currency} за 1 $`, { inputmode: "decimal" }),
      h("div", { class: "grid" }, f("currency", "Валюта выплаты", { maxlength: 8 }), f("timer_min", "Таймер, минут", { inputmode: "numeric" }),
        f("min_usd", "Мин. ордер, $", { inputmode: "decimal" }), f("max_usd", "Макс. ордер, $", { inputmode: "decimal" })),
      owner ? f("auto_credit_max", "Чеки дороже этой суммы — на ручную проверку, $", { inputmode: "decimal" }) : null,
      btn("Сохранить", "primary", () => save(["rate", "currency", "timer_min", "min_usd", "max_usd", ...(owner ? ["auto_credit_max"] : [])]))),
    card(title("Часы работы"), box("accepting", "Принимать заявки"), box("hours_enabled", "Ограничить по расписанию"),
      h("div", { class: "grid" }, f("hours_from", "С", { type: "time" }), f("hours_to", "До", { type: "time" })),
      f("tz_offset", "Часовой пояс, UTC± (Москва 3, Якутск 9)", { inputmode: "text" }),
      h("div", { class: "tabs" }, ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"].map((name, i) => {
        const d = String(i + 1);
        return btn(name, days.includes(d) ? "on" : "", () => {
          drafts.set_work_days = days.includes(d) ? days.replace(d, "") : days + d; render();
        });
      })),
      btn("Сохранить", "primary", () => save(["accepting", "hours_enabled", "hours_from", "hours_to", "tz_offset", "work_days"]))),
  ];
}

function adminsScreen() {
  return [
    card(title("Администраторы"), S.admin.admins.map((a) => h("div", { class: "between kv" },
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
  if (owner) tabs.push(["review", `Чеки${S.admin.review.length ? " (" + S.admin.review.length + ")" : ""}`], ["admins", "Админы"], ["account", "Аккаунт"], ["balance", "Балансы"]);
  if (!tabs.some(([k]) => k === adminTab)) adminTab = "orders";
  const screens = { orders: () => orderList(S.admin.orders, "staff"), settings: settingsScreen, review: reviewScreen, admins: adminsScreen, account: accountScreen, balance: balanceScreen };
  return [h("div", { class: "tabs" }, tabs.map(([k, name]) => btn(name, k === adminTab ? "on" : "", () => { adminTab = k; render(); }))), ...screens[adminTab]()];
}

function go(name) { tab = name; openOrder = null; render(); scrollTo(0, 0); }

function render() {
  const focus = document.activeElement?.dataset?.key, pos = focus ? document.activeElement.selectionStart : null;
  const status = document.getElementById("status");
  status.textContent = S.open ? "● Работаем" : "● Закрыто"; status.className = "pill " + (S.open ? "green" : "red");
  if (tab === "admin" && !S.admin) tab = "swap";
  let content;
  const list = openOrder && (openOrder.mode === "staff" ? S.admin?.orders : S.orders);
  const order = list?.find((o) => o.id === openOrder.id);
  if (order) content = orderScreen(order, openOrder.mode);
  else {
    openOrder = null;
    content = { swap: swapScreen, deposit: depositScreen, orders: () => orderList(S.orders, "client"), admin: adminScreen }[tab]();
  }
  view.replaceChildren(...content.flat().filter(Boolean));
  const nav = document.getElementById("nav");
  const items = [["swap", "⇄", "Обмен"], ["deposit", "＋", "Пополнить"], ["orders", "☰", "Заявки"]];
  if (S.admin) items.push(["admin", "⚙", "Админ"]);
  nav.hidden = false;
  nav.replaceChildren(...items.map(([k, icon, name]) => btn(`${icon} ${name}`, k === tab ? "on" : "", () => go(k))));
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
}

tg?.ready(); tg?.expand();
try { tg?.setHeaderColor?.("#0c1014"); tg?.setBackgroundColor?.("#0c1014"); } catch { /* старый клиент */ }
const wanted = Number(new URLSearchParams(location.search).get("order"));
refresh().then(() => {
  if (!wanted || !S) return;
  const staff = S.admin?.orders.find((o) => o.id === wanted && !o.mine);
  tab = staff ? "admin" : "orders"; openOrder = { id: wanted, mode: staff ? "staff" : "client" }; render();
});
setInterval(tick, 1000);
setInterval(() => { if (!document.hidden && !busy) refresh(); }, 4000);
