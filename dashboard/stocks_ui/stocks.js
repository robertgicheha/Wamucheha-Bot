/* Stocks & ETFs tab: screener list and single-security pages. Research only.
   Loaded after the console's own script and uses its api(), el(), toast() and LOADERS.
   All data is written with textContent / DOM nodes, never as HTML. */
(() => {
"use strict";

const REFRESH_MS = 60000, FILL_MS = 5000;
const S = {market: "nse", kind: "all", filter: "all", colTab: "overview", view: "table", q: "", sort: null,
  data: null, at: 0, req: 0, busy: false,
  sym: null, detail: null, dtab: "overview", period: null, style: "area", showMA: false, showVol: true,
  chart: null, quarterly: false, extra: {}, detailAt: 0, dreq: 0};

const root = $("#t-stocks");
const h = (tag, cls_, ...kids) => { const e = document.createElement(tag); if (cls_) e.className = cls_; kids.flat().forEach(k => { if (k !== null && k !== undefined && k !== false) e.append(k); }); return e; };
const btn = (text, cls_, onclick) => { const b = h("button", cls_, text); b.type = "button"; b.onclick = onclick; return b; };
const svg = (tag, attrs = {}) => { const e = document.createElementNS("http://www.w3.org/2000/svg", tag); Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v)); return e; };
const css = name => getComputedStyle(document.body).getPropertyValue(name).trim();
const safeUrl = u => /^https?:\/\//i.test(u || "") ? u : null;
const risk = () => { try { return localStorage.getItem("sxRisk") || "balanced"; } catch (e) { return "balanced"; } };

/* ---------- formatting ---------- */
const DASH = "—";
const has = v => v !== null && v !== undefined && v !== "" && !(typeof v === "number" && isNaN(v));
const fix = (v, d = 2) => has(v) ? Number(v).toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d}) : DASH;
const compact = v => has(v) ? new Intl.NumberFormat("en-US", {notation: "compact", maximumFractionDigits: 2}).format(v) : DASH;
const pct = (v, d = 2) => has(v) ? fix(v, d) + "%" : DASH;
const signed = (v, d = 2) => has(v) ? (v > 0 ? "+" : "") + fix(v, d) + "%" : DASH;
const tone = v => !has(v) || v === 0 ? "" : v > 0 ? "sx-up" : "sx-down";
const ratingTone = v => /buy/i.test(v || "") ? "sx-up" : /sell/i.test(v || "") ? "sx-down" : "sx-mute";
const withUnit = (text, unit) => text === DASH ? DASH : h("span", "", text, h("span", "sx-unit", unit));
const span = (text, cls_) => h("span", cls_, text);

const get = (r, path) => path.split(".").reduce((o, k) => o == null ? o : o[k], r);

/* ---------- logos and sparklines ---------- */
function logo(symbol, big) {
  const mono = () => { const m = h("span", "sx-logo mono" + (big ? " big" : ""), symbol.slice(0, big ? 2 : 1)); let n = 0; for (const c of symbol) n = (n * 31 + c.charCodeAt(0)) % 360; m.style.background = `hsl(${n} 45% 38%)`; return m; };
  if (S.market !== "nse") return mono();
  const img = h("img", "sx-logo" + (big ? " big" : ""));
  img.alt = ""; img.loading = "lazy"; img.src = "/stocks-assets/logos/" + encodeURIComponent(symbol);
  img.onerror = () => img.replaceWith(mono());
  return img;
}
function spark(values) {
  if (!values || values.length < 2) return DASH;
  const lo = Math.min(...values), range = (Math.max(...values) - lo) || 1, W = 90, H = 26;
  const s = svg("svg", {width: W, height: H, class: "sx-spark"});
  s.append(svg("polyline", {fill: "none", "stroke-width": 1.5, stroke: css(values[values.length - 1] >= values[0] ? "--ok" : "--bad"),
    points: values.map((v, i) => `${(i / (values.length - 1) * W).toFixed(1)},${(H - 2 - (v - lo) / range * (H - 4)).toFixed(1)}`).join(" ")}));
  return s;
}

/* ---------- table columns ---------- */
const C = (label, path, show, extra = {}) => ({label, path, show, ...extra});
const cur = r => r.currency;
const COL = {
  price: C("Price", "price", r => withUnit(fix(r.price), cur(r))),
  chg: C("Chg %", "change_pct", r => signed(r.change_pct), {tone: r => tone(r.change_pct)}),
  vol: C("Vol", "volume", r => compact(r.volume)),
  rel: C("Rel vol", "metrics.rel_vol", r => fix(r.metrics.rel_vol)),
  mcap: C("Mkt cap", "fund.market_cap", r => withUnit(compact(r.fund.market_cap || r.fund.aum), cur(r))),
  pe: C("P/E", "fund.pe", r => fix(r.fund.pe)),
  eps: C("EPS dil", "fund.eps", r => withUnit(fix(r.fund.eps), cur(r)), {sub: "TTM"}),
  epsg: C("EPS growth", "fund.eps_growth_pct", r => signed(r.fund.eps_growth_pct), {sub: "YoY", tone: r => tone(r.fund.eps_growth_pct)}),
  yld: C("Div yield %", "dividend.yield_pct", r => pct(get(r, "dividend.yield_pct")), {sub: "TTM"}),
  sector: C("Sector", "sector", r => r.sector, {left: true}),
  analyst: C("Analyst rating", "fund.analyst_rating", r => r.fund.analyst_rating || "– No rating", {left: true, tone: r => ratingTone(r.fund.analyst_rating)}),
};
const perf = (label, key) => C(label, "metrics.perf." + key, r => signed(r.metrics.perf[key]), {tone: r => tone(r.metrics.perf[key])});
const money = (label, key, sub = "TTM") => C(label, "fund." + key, r => withUnit(compact(r.fund[key]), cur(r)), {sub});
const ratio = (label, key) => C(label, "fund." + key, r => fix(r.fund[key]));
const percent = (label, key) => C(label, "fund." + key, r => pct(r.fund[key]));
const word = (label, path) => C(label, path, r => get(r, path) || DASH, {left: true, tone: r => ratingTone(get(r, path))});

const TABLES = [
  ["overview", "Overview", [perf("Perf % 1Y", "1y"), COL.price, COL.chg, COL.vol, COL.rel, COL.mcap, COL.pe, COL.eps, COL.epsg, COL.yld, COL.sector, COL.analyst]],
  ["performance", "Performance", [COL.price, COL.chg, perf("1W", "1w"), perf("1M", "1m"), perf("3M", "3m"), perf("6M", "6m"), perf("YTD", "ytd"), perf("1Y", "1y"), perf("5Y", "5y"), perf("All time", "all"), C("Volatility 1M", "metrics.volatility_1m", r => pct(r.metrics.volatility_1m)), C("Last 30 days", "metrics.perf.1m", r => spark(r.metrics.spark))]],
  ["technicals", "Technicals", [COL.price, COL.chg, word("Technical rating", "metrics.tech"), word("Moving averages", "metrics.tech_ma"), word("Oscillators", "metrics.tech_osc"), C("RSI (14)", "metrics.rsi", r => fix(r.metrics.rsi, 1)), COL.rel, C("52W high", "metrics.high_52w", r => fix(r.metrics.high_52w)), C("52W low", "metrics.low_52w", r => fix(r.metrics.low_52w)), C("Bot buy score", "analysis.buy_score", r => has(r.analysis.buy_score) ? r.analysis.buy_score : DASH)]],
  ["valuation", "Valuation", [COL.price, COL.mcap, COL.pe, ratio("Forward P/E", "forward_pe"), ratio("P/S", "ps"), ratio("P/B", "pb"), money("Enterprise value", "enterprise_value", "latest"), ratio("EV/EBITDA", "ev_ebitda"), ratio("Beta", "beta")]],
  ["dividends", "Dividends", [COL.price, COL.yld, C("Div per share", "dividend.annual_per_share", r => withUnit(fix(get(r, "dividend.annual_per_share"), 4), cur(r)), {sub: "TTM"}), percent("Payout ratio %", "payout_ratio_pct"), C("Last ex-date / book closure", "dividend.last_ex_date", r => get(r, "dividend.last_ex_date") || get(r, "dividend.book_closure") || DASH), C("Growth / yr", "dividend.annual_growth_pct", r => signed(get(r, "dividend.annual_growth_pct")), {tone: r => tone(get(r, "dividend.annual_growth_pct"))}), C("Payments, 12m", "dividend.payments_last_year", r => has(get(r, "dividend.payments_last_year")) ? r.dividend.payments_last_year : DASH), COL.sector]],
  ["profitability", "Profitability", [percent("Gross margin %", "gross_margin_pct"), percent("Operating margin %", "operating_margin_pct"), percent("Net margin %", "net_margin_pct"), percent("Return on assets %", "roa_pct"), percent("Return on equity %", "roe_pct"), money("Revenue per employee", "revenue_per_employee"), money("Profit per employee", "profit_per_employee"), C("Employees", "fund.employees", r => compact(r.fund.employees))]],
  ["income", "Income statement", [money("Revenue", "revenue"), C("Revenue growth", "fund.revenue_growth_pct", r => signed(r.fund.revenue_growth_pct), {sub: "YoY", tone: r => tone(r.fund.revenue_growth_pct)}), money("Gross profit", "gross_profit"), money("EBITDA", "ebitda"), money("Net income", "net_income"), COL.eps, COL.epsg]],
  ["balance", "Balance sheet", [money("Total cash", "cash", "latest"), money("Total debt", "debt", "latest"), ratio("Debt / equity %", "debt_to_equity"), ratio("Current ratio", "current_ratio"), ratio("Quick ratio", "quick_ratio"), COL.mcap]],
  ["cashflow", "Cash flow", [money("Operating cash flow", "operating_cash_flow"), money("Free cash flow", "free_cash_flow"), money("Revenue", "revenue"), money("Net income", "net_income"), COL.mcap]],
];

/* Bot pick lists (the module's own rules), shown as a second row of pills. */
const PICKS = {
  best_buy: ["Best buy", "best buys", "Trend, momentum and risk score of 65 or more, not overbought, with volatility of 40% a year or less. Needs 60 or more days of price history."],
  best_stocks: ["Best stocks to buy", "best stocks to buy", ""],
  likely_to_rise: ["Likely to rise", "stocks that rose most often after this set-up", "On past days with the same trend set-up these were higher 21 trading days later at least 55% of the time, and more often than on an average day. This is a past frequency, not a prediction. Needs about 16 months of history."],
  speculative: ["Speculative buy", "speculative buys", "HIGH RISK. Volatile names with a strong run, an oversold reading or unusual volume. Only use money you can afford to lose."],
};
const TITLES = {all: "all stocks", top_gainers: "top gainers today", biggest_losers: "biggest losers today", large_cap: "large-cap stocks", small_cap: "small-cap stocks", largest_employers: "stocks of the largest employers", high_dividend: "high-dividend stocks", net_income: "stocks with the highest net income", highest_cash: "stocks with the most cash", profit_per_employee: "stocks with the highest profit per employee", revenue_per_employee: "stocks with the highest revenue per employee", most_active: "most active stocks", unusual_volume: "stocks with unusual volume", most_volatile: "most volatile stocks", high_beta: "high-beta stocks", best_performing: "stocks with the best yearly performance", highest_revenue: "stocks with the highest revenue", most_expensive: "most expensive stocks", penny: "penny stocks", overbought: "overbought stocks", oversold: "oversold stocks", ath: "stocks at an all-time high", atl: "stocks at an all-time low", high_52w: "stocks at a 52-week high", low_52w: "stocks at a 52-week low"};
/* The column the chart ranks by for each filter, and the table tab that shows it. */
const RANK = {all: ["change_pct", "Day change %"], top_gainers: ["change_pct", "Day change %"], biggest_losers: ["change_pct", "Day change %"], large_cap: ["fund.market_cap", "Market value"], small_cap: ["fund.market_cap", "Market value"], largest_employers: ["fund.employees", "Employees", "profitability"], high_dividend: ["dividend.yield_pct", "Dividend yield %", "dividends"], net_income: ["fund.net_income", "Net income", "income"], highest_cash: ["fund.cash", "Cash", "balance"], profit_per_employee: ["fund.profit_per_employee", "Profit per employee", "profitability"], revenue_per_employee: ["fund.revenue_per_employee", "Revenue per employee", "profitability"], most_active: ["volume", "Volume"], unusual_volume: ["metrics.rel_vol", "Relative volume"], most_volatile: ["metrics.volatility_1m", "Volatility %", "performance"], high_beta: ["fund.beta", "Beta", "valuation"], best_performing: ["metrics.perf.1y", "1-year performance %"], highest_revenue: ["fund.revenue", "Revenue", "income"], most_expensive: ["price", "Price"], penny: ["price", "Price"], overbought: ["metrics.rsi", "RSI", "technicals"], oversold: ["metrics.rsi", "RSI", "technicals"], high_52w: ["metrics.perf.1y", "1-year performance %", "technicals"], low_52w: ["metrics.perf.1y", "1-year performance %", "technicals"], ath: ["metrics.perf.1y", "1-year performance %", "performance"], atl: ["metrics.perf.1y", "1-year performance %", "performance"], best_buy: ["analysis.buy_score", "Bot buy score"], best_stocks: ["change_pct", "Day change %"], likely_to_rise: ["analysis.outlook.edge_pct", "Edge over an average day %"], speculative: ["change_pct", "Day change %"]};

/* ---------- list data ---------- */
const bySymbol = () => Object.fromEntries(S.data.rows.map(r => [r.symbol, r]));
function filtered() {
  const d = S.data, by = bySymbol();
  let symbols;
  if (S.filter === "speculative") symbols = d.picks.speculative.map(s => s.symbol);
  else if (PICKS[S.filter]) symbols = d.picks[S.filter];
  else symbols = (d.screens.find(s => s.key === S.filter) || {symbols: []}).symbols;
  const q = S.q.trim().toLowerCase();
  return symbols.map(s => by[s]).filter(r => r && (S.kind === "all" || r.kind === S.kind) && (!q || (r.symbol + " " + r.name + " " + r.sector).toLowerCase().includes(q)));
}
function sorted(rows) {
  if (!S.sort) return rows;
  const {path, dir} = S.sort;
  return [...rows].sort((a, b) => {
    const x = get(a, path), y = get(b, path);
    if (!has(x) || !has(y)) return has(x) ? -1 : has(y) ? 1 : 0;       // blanks always last
    return (typeof x === "number" ? x - y : String(x).localeCompare(String(y))) * dir;
  });
}

function pillRow() {
  const counts = Object.fromEntries(S.data ? S.data.screens.map(s => [s.key, s.symbols.length]) : []);
  const keys = S.data ? S.data.screens : Object.keys(TITLES).map(key => ({key, label: key}));
  const pill = (key, label, n) => btn(label, "sx-pill" + (S.filter === key ? " on" : "") + (n === 0 ? " empty" : ""), () => setFilter(key));
  return [h("div", "sx-pills", keys.map(s => pill(s.key, S.kind === "etf" && s.key === "all" ? "All ETFs" : s.label, counts[s.key]))),
    h("div", "sx-pills sx-picks", span("Bot picks"), Object.entries(PICKS).map(([k, v]) => pill(k, v[0], S.data ? S.data.picks[k].length : undefined)))];
}
function setFilter(key) {
  S.filter = key; S.sort = null;
  S.colTab = (RANK[key] && RANK[key][2]) || "overview";
  renderList();
}

function heading() {
  const nse = S.market === "nse", who = nse ? "Kenyan" : "US";
  let title = (PICKS[S.filter] ? PICKS[S.filter][1] : TITLES[S.filter]);
  if (S.kind === "etf") title = title.replace("stocks", "ETFs");
  const screen = S.data && S.data.screens.find(s => s.key === S.filter);
  let text = PICKS[S.filter] ? PICKS[S.filter][2] : screen ? screen.description : "";
  if (S.filter === "best_stocks") text = nse ? `Sokowatch-Web's screener: BUY means a good fit (match score 70 or more) for a ${risk()} profile, based on sector, liquidity and today's move. It is not a forecast.` : "The ten highest bot buy scores, from trend, momentum and risk.";
  const headline = title.startsWith("all ") ? `All ${who} ${title.slice(4)}` : `${who} ${title}`;
  return h("div", "sx-head", h("h1", "", headline), h("p", "", text));
}

function statusLine() {
  const m = S.data.meta, label = {live: "Delayed prices", stale: "STALE: last saved prices", unavailable: "NO DATA"}[m.source] || m.source;
  const bits = [label, m.provider, m.updated ? "as of " + String(m.updated).replace("T", " ").slice(0, 19) + (S.market === "nse" ? " UTC" : "") : "", m.status];
  if (S.market === "nse") bits.push(`price history recorded here: ${m.history_days} day(s)`, `companies with figures from your CSV: ${m.with_fundamentals}`);
  const line = h("div", "sx-status", bits.filter(Boolean).join(" · "));
  if (m.fundamentals_waiting) line.append(" · ", h("b", "", `loading company figures: ${m.fundamentals_waiting} to go`));
  if (m.note) line.append(" · ", h("b", "", m.note));
  return line;
}

function emptyMessage() {
  const d = S.data;
  if (!d.rows.length) return "No data. " + (d.meta.note || "");
  const screen = d.screens.find(s => s.key === S.filter), nse = S.market === "nse";
  if (nse && screen && screen.needs) return `Nothing to show. This filter needs ${screen.needs}. The Sokowatch NSE feed only has price, change, volume and sector: add figures in config/nse_fundamentals.csv (see the .example file). Price history builds up from the days this bot fetches prices, or can be imported.`;
  if (screen && screen.needs && d.meta.fundamentals_waiting) return "Company figures are still loading…";
  if (nse && ["best_buy", "likely_to_rise"].includes(S.filter)) return "Nothing to show yet. This list needs more NSE price history than has been recorded here.";
  return "Nothing matches this filter right now.";
}

function tableView(rows) {
  const tab = TABLES.find(t => t[0] === S.colTab) || TABLES[0];
  let cols = tab[2];
  if (S.filter === "speculative") {
    const why = Object.fromEntries(S.data.picks.speculative.map(s => [s.symbol, s.reasons.join(" · ")]));
    cols = [COL.price, COL.chg, COL.vol, C("Why it is here (high risk)", "symbol", r => why[r.symbol], {wide: true})];
  } else if (S.filter === "likely_to_rise" && S.colTab === "overview") {
    const o = (label, key, show) => C(label, "analysis.outlook." + key, r => show(r.analysis.outlook[key]));
    cols = [COL.price, COL.chg, o("Rose after 21 days", "rose_pct", v => pct(v, 1)), o("All days", "all_days_rose_pct", v => pct(v, 1)), o("Edge", "edge_pct", v => signed(v, 1)), o("Median move", "median_move_pct", v => signed(v, 1)), o("Independent samples", "independent_samples", v => v), COL.sector];
  } else if (S.filter === "best_stocks" && S.colTab === "overview") {
    const lead = S.market === "nse" ? [C("Match", "match_score", r => r.match_score), C("Profile fit", "verdict", r => r.verdict, {tone: r => ratingTone(r.verdict === "AVOID" ? "sell" : r.verdict)})] : [C("Bot buy score", "analysis.buy_score", r => r.analysis.buy_score)];
    cols = [...lead, ...tab[2]];
  } else if (S.filter === "best_buy" && S.colTab === "overview") cols = [C("Bot buy score", "analysis.buy_score", r => r.analysis.buy_score), ...tab[2]];

  const head = h("tr");
  const arrow = path => S.sort && S.sort.path === path ? (S.sort.dir < 0 ? "↓ " : "↑ ") : "";
  const th = (label, path, sub, left) => { const e = h("th", left ? "l" : "", arrow(path) + label, sub ? h("small", "", sub) : null); e.onclick = () => { S.sort = {path, dir: S.sort && S.sort.path === path ? -S.sort.dir : -1}; renderBody(); }; return e; };
  head.append(th("Symbol", "symbol", String(rows.length), true), ...cols.map(c => th(c.label, c.path, c.sub, c.left || c.wide)));
  const body = h("tbody");
  sorted(rows).forEach(r => {
    const tr = h("tr", "", h("td", "", h("div", "sx-sym", logo(r.symbol), span(r.symbol, "sx-ticker"), span(r.name, "sx-name"), h("sup", "", "D"))));
    cols.forEach(c => tr.append(h("td", (c.wide ? "wrap " : c.left ? "l " : "") + (c.tone ? c.tone(r) : ""), c.show(r))));
    tr.onclick = () => openSymbol(r.symbol);
    tr.title = "Open " + r.name;
    body.append(tr);
  });
  const table_ = h("table", "sx-table", h("thead", "", head), body);
  return h("div", "sx-scroll", table_);
}

/* ---------- market charts and heat map ---------- */
function barList(items, {format, signedBars} = {}) {
  const top = Math.max(...items.map(i => Math.abs(i.value)), 1e-9);
  if (!items.length) return h("p", "sx-mute", "No data for this chart.");
  return items.map(i => {
    const fill = h("i", signedBars ? (i.value >= 0 ? "up" : "down") : "");
    fill.style.width = Math.max(1, Math.abs(i.value) / top * 100) + "%";
    const line = h("div", "sx-hbar" + (i.symbol ? " sx-clickable" : ""), span(i.label), h("div", "sx-track", fill), span(format(i.value), signedBars ? tone(i.value) : ""));
    if (i.symbol) line.onclick = () => openSymbol(i.symbol);
    return line;
  });
}
const card = (title, ...kids) => h("div", "sx-card", h("h3", "", title), ...kids);

function chartsView(rows) {
  const [path, label] = RANK[S.filter] || RANK.all;
  let withValue = rows.filter(r => has(get(r, path)));
  // These lists are not ordered by the charted figure, so rank them here.
  if (["all", "best_stocks", "speculative"].includes(S.filter)) withValue = [...withValue].sort((a, b) => get(b, path) - get(a, path));
  const isPct = /%/.test(label), big = ["fund.market_cap", "fund.net_income", "fund.cash", "fund.revenue", "fund.employees", "volume", "fund.profit_per_employee", "fund.revenue_per_employee"].includes(path);
  const format = v => isPct ? signed(v, 2) : big ? compact(v) : fix(v);
  const moved = rows.filter(r => has(r.change_pct));
  const up = moved.filter(r => r.change_pct > 0).length, down = moved.filter(r => r.change_pct < 0).length, flat = moved.length - up - down;
  const breadth = h("div", "sx-breadth", [[up, "--ok"], [flat, "--mute"], [down, "--bad"]].map(([n, v]) => { const i = h("i"); i.style.cssText = `display:block;flex:${n};background:var(${v})`; return i; }));
  const insights = S.data.insights.points.map(p => h("p", "", h("b", p.impact === "positive" ? "sx-up" : p.impact === "negative" ? "sx-down" : "", p.title + ": "), p.text));
  const group = key => { const sum = {}; moved.forEach(r => { const v = key.value(r); if (has(v)) (sum[r.sector] = sum[r.sector] || []).push(v); }); return Object.entries(sum).map(([name, v]) => ({label: name, value: key.reduce(v)})).sort((a, b) => b.value - a.value); };
  return h("div", "sx-grid",
    card(`Top 10 by ${label.toLowerCase()}`, barList(withValue.slice(0, 10).map(r => ({label: r.symbol, symbol: r.symbol, value: get(r, path)})), {format, signedBars: isPct})),
    card("Market breadth today (this list)", breadth, h("p", "", span(`${up} up`, "sx-up"), " · ", span(`${flat} unchanged`, "sx-mute"), " · ", span(`${down} down`, "sx-down")), h("h3", "", "Whole market"), h("p", "sx-mute", S.data.insights.summary), insights),
    card("Average move today by sector", barList(group({value: r => r.change_pct, reduce: v => v.reduce((a, b) => a + b, 0) / v.length}), {format: v => signed(v), signedBars: true})),
    card("Money traded today by sector (" + S.data.meta.currency + ")", barList(group({value: r => r.price * r.volume, reduce: v => v.reduce((a, b) => a + b, 0)}).slice(0, 12), {format: compact})));
}
function heatView(rows) {
  const tiles = [...rows].filter(r => has(r.change_pct)).sort((a, b) => b.change_pct - a.change_pct).map(r => {
    const strength = .28 + Math.min(Math.abs(r.change_pct) / 4, 1) * .72;
    const b = btn([h("b", "", r.symbol), span(signed(r.change_pct))], "", () => openSymbol(r.symbol));
    b.style.background = r.change_pct > 0 ? `rgba(8,153,129,${strength})` : r.change_pct < 0 ? `rgba(242,54,69,${strength})` : "#555";
    b.title = r.name;
    return b;
  });
  return h("div", "sx-card", h("h3", "", "Heat map: today's change"), tiles.length ? h("div", "sx-heat", tiles) : h("p", "sx-mute", "No data."));
}

function renderBody() {
  const box = $("#sx-body"); if (!box || !S.data) return;
  const rows = filtered(), old = box.querySelector(".sx-scroll");
  const pos = old ? [old.scrollTop, old.scrollLeft] : null;
  if (!rows.length) { box.replaceChildren(h("div", "sx-empty", emptyMessage())); return; }
  box.replaceChildren(S.view === "heat" ? heatView(rows) : S.view === "charts" ? chartsView(rows) : tableView(rows));
  const now = box.querySelector(".sx-scroll");
  if (pos && now) { now.scrollTop = pos[0]; now.scrollLeft = pos[1]; }
}

function liveBadge() {
  const m = S.detail && S.sym ? S.detail.info : S.data && S.data.meta;
  const state = !m ? "" : m.source === "unavailable" || m.source === "stale" ? "bad" : "on";
  const every = S.market === "nse" ? "screen checks every 60s; the NSE feed itself updates about hourly" : "updates every 60s";
  return h("div", "sx-live", h("span", "sx-dot " + state), span(`Live view · ${every}${S.at ? " · checked " + new Date(S.at).toLocaleTimeString() : ""}`));
}

function toolbar() {
  const seg = (options, current, pick) => h("div", "sx-group", options.map(([k, n]) => btn(n, "sx-btn" + (k === current ? " on" : ""), () => pick(k))));
  const search = h("input"); search.placeholder = "Search symbol, name or sector…"; search.value = S.q; search.id = "sx-q";
  search.oninput = () => { S.q = search.value; renderBody(); };
  search.onkeydown = e => {
    if (e.key !== "Enter" || !S.q.trim()) return;
    const rows = filtered(), exact = rows.find(r => r.symbol === S.q.trim().toUpperCase());
    if (exact || rows.length === 1) openSymbol((exact || rows[0]).symbol);
    else if (S.market === "us" && !rows.length) openSymbol(S.q.trim().toUpperCase());    // any US ticker
  };
  search.title = S.market === "us" ? "Press Enter to open any US ticker, listed here or not" : "Press Enter to open a match";
  const bits = [seg([["nse", "Kenya (NSE)"], ["us", "United States"]], S.market, setMarket),
    seg([["all", "Stocks + ETFs"], ["stock", "Stocks"], ["etf", "ETFs"]], S.kind, k => { S.kind = k; renderList(); }), search];
  if (S.market === "nse") {
    const pick = h("select", "", [["conservative", "Conservative"], ["balanced", "Balanced"], ["aggressive", "Aggressive"]].map(([v, n]) => { const o = h("option", "", n); o.value = v; return o; }));
    pick.value = risk(); pick.onchange = () => { try { localStorage.setItem("sxRisk", pick.value); } catch (e) {} loadList(); };
    bits.push(h("label", "sx-mute", "Risk profile ", pick));
  }
  bits.push(liveBadge());
  return h("div", "sx-bar", bits);
}

function renderList() {
  if (S.sym) return;
  destroyChart();
  const focused = document.activeElement && document.activeElement.id === "sx-q";
  const parts = [toolbar(), ...pillRow(), heading()];
  if (!S.data) parts.push(h("div", "sx-empty", S.market === "us" ? "Loading US prices and full price history. The first load can take up to a minute…" : "Loading NSE prices…"));
  else {
    const views = h("div", "sx-group", [["table", "Table"], ["charts", "Charts"], ["heat", "Heat map"]].map(([k, n]) => btn(n, "sx-btn" + (S.view === k ? " on" : ""), () => { S.view = k; renderList(); })));
    const tabs = h("div", "sx-tabs", TABLES.map(([k, n]) => btn(n, S.colTab === k && S.view === "table" ? "on" : "", () => { S.colTab = k; S.view = "table"; S.sort = null; renderList(); })), views);
    const body = h("div"); body.id = "sx-body";
    parts.push(statusLine(), tabs, body, h("p", "sx-note", S.data.disclaimer + " D = delayed prices."));
  }
  root.replaceChildren(...parts);
  renderBody();
  if (focused) { const q = $("#sx-q"); q.focus(); q.setSelectionRange(q.value.length, q.value.length); }
}

async function loadList(live) {
  const req = ++S.req, market = S.market;
  S.busy = true;
  try {
    const d = await api(`/stocks/market?market=${market}&risk=${risk()}${live ? "&live=true" : ""}`);
    if (req !== S.req || market !== S.market) return;     // a newer request replaced this one
    S.data = d; S.at = Date.now(); renderList();
  } catch (e) {
    if (req === S.req && e.message !== "unauthorized") { toast("Stocks: " + e.message); S.at = Date.now(); }
  } finally { if (req === S.req) S.busy = false; }
}
function setMarket(m) {
  if (m === S.market && !S.sym) return;
  S.market = m; S.data = null; S.sort = null; S.sym = null; S.q = ""; setHash();
  renderList(); loadList();
}

/* ---------- single security ---------- */
const PERIODS = [["1D", "1 day", "1d"], ["5D", "5 days", "1w"], ["1M", "1 month", "1m"], ["3M", "3 months", "3m"], ["6M", "6 months", "6m"], ["YTD", "Year to date", "ytd"], ["1Y", "1 year", "1y"], ["5Y", "5 years", "5y"], ["ALL", "All time", "all"]];
const PERIOD_DAYS = {"1M": 30, "3M": 91, "6M": 182, "1Y": 365, "5Y": 1826};

function setHash(push) {
  const hash = S.sym ? `#stocks/${S.market}/${encodeURIComponent(S.sym)}` : "";
  if (hash === location.hash) return;
  history[push ? "pushState" : "replaceState"](null, "", location.pathname + location.search + hash);
}
async function openSymbol(symbol, fromHistory) {
  S.sym = symbol; S.detail = null; S.dtab = "overview"; S.extra = {}; S.quarterly = false;
  if (!fromHistory) setHash(true);
  destroyChart();
  root.replaceChildren(crumbs(), h("div", "sx-empty", `Loading ${symbol}…`));
  window.scrollTo(0, 0);
  const req = ++S.dreq;
  try {
    const d = await api(`/stocks/detail?market=${S.market}&symbol=${encodeURIComponent(symbol)}&risk=${risk()}`);
    if (req !== S.dreq || S.sym !== symbol) return;
    S.detail = d; S.detailAt = S.at = Date.now();
    // Kenyan history is short at first: start on the day view if there is little to draw.
    S.period = S.period && (d.chart.length > 30 || ["1D", "5D"].includes(S.period)) ? S.period : d.chart.length > 30 ? "1Y" : "1D";
    renderDetail();
  } catch (e) {
    if (req === S.dreq && S.sym === symbol) root.replaceChildren(crumbs(), h("div", "sx-empty", `Could not open ${symbol}: ${e.message}`));
  }
}
function closeSymbol(fromHistory) {
  S.sym = null; S.detail = null; S.dreq++;
  if (!fromHistory) setHash(true);
  renderList();
  if (!S.data) loadList();
}

function crumbs() {
  const r = S.detail && S.detail.row;
  const bits = [btn("Markets", "", () => closeSymbol()), span("/"), btn(S.market === "nse" ? "Kenya" : "United States", "", () => closeSymbol())];
  if (r) bits.push(span("/"), btn(r.kind === "etf" ? "ETFs" : "Stocks", "", () => { S.kind = r.kind === "etf" ? "etf" : "stock"; closeSymbol(); }), span("/"), span(r.sector));
  if (r && r.fund.industry) bits.push(span("/"), span(r.fund.industry));
  bits.push(span("/"), span(S.sym || ""));
  return h("div", "sx-crumbs", bits);
}

function priceBlock() {
  const d = S.detail, r = d.row;
  const asOf = S.market === "nse" ? (d.info.updated ? "As of " + new Date(d.info.updated).toLocaleString("en-GB", {timeZone: "Africa/Nairobi"}) + " EAT" : "") : "Latest price, session of " + d.info.updated;
  const block = h("div", "sx-price", h("b", "", fix(r.price)), span(r.currency, "sx-mute"), span(has(r.change) ? (r.change > 0 ? "+" : "") + fix(r.change) : "", tone(r.change_pct)), span(signed(r.change_pct), tone(r.change_pct)));
  return h("div", "", block, h("div", "sx-price", h("small", "", asOf)));
}

function hero() {
  const d = S.detail, r = d.row;
  const tags = h("div", "sx-tags", span(r.symbol), span("·", "sx-mute"), span(d.info.exchange),
    d.info.is_open === null ? null : span(d.info.is_open ? "● Open" : "● Closed", "sx-tag" + (d.info.is_open ? " open" : "")),
    span(d.info.source === "stale" ? "stale prices" : "delayed", "sx-tag delay"));
  const price = priceBlock(); price.id = "sx-priceblock";
  return h("div", "sx-hero", logo(r.symbol, true), h("div", "", h("h1", "", r.name), tags, price));
}

function renderDetail() {
  if (!S.detail) return;
  destroyChart();
  const isEtf = S.detail.row.kind === "etf";
  const tabs = [["overview", "Overview"], ["financials", isEtf ? "Holdings" : "Financials"], ["news", "News"], ["technicals", "Technicals"], ["seasonals", "Seasonals"], ["dividends", "Dividends"], ["analysis", "Bot analysis"]];
  const content = h("div"); content.id = "sx-content";
  root.replaceChildren(crumbs(), liveBadge(), hero(), h("div", "sx-dtabs", tabs.map(([k, n]) => btn(n, S.dtab === k ? "on" : "", () => { S.dtab = k; renderDetail(); }))), content, h("p", "sx-note", S.detail.disclaimer));
  ({overview: tabOverview, financials: tabFinancials, news: tabNews, technicals: tabTechnicals, seasonals: tabSeasonals, dividends: tabDividends, analysis: tabAnalysis})[S.dtab](content);
}

/* ----- overview: chart, period tiles, key stats ----- */
function destroyChart() {
  if (!S.chart) return;
  S.chart.observer.disconnect(); S.chart.api.remove(); S.chart = null;
}
function periodChange(key, field) {
  const r = S.detail.row;
  return key === "1D" ? r.change_pct : r.metrics.perf[field];
}
function periodTiles() {
  const box = h("div", "sx-periods", PERIODS.map(([key, label, field]) => { const v = periodChange(key, field); return btn([span(label), span(signed(v), tone(v) || "sx-mute")], S.period === key ? "on" : "", () => { S.period = key; box.replaceWith(periodTiles()); drawChart(); }); }));
  box.id = "sx-periods";
  return box;
}
function cutoff(lastDay) {
  if (S.period === "ALL") return "";
  if (S.period === "YTD") return `${Number(lastDay.slice(0, 4)) - 1}-12-31`;
  const d = new Date(lastDay + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() - PERIOD_DAYS[S.period]);
  return d.toISOString().slice(0, 10);
}
const average = (candles, n) => { const out = []; let sum = 0; candles.forEach((c, i) => { sum += c.close; if (i >= n) sum -= candles[i - n].close; if (i >= n - 1) out.push({time: c.time, value: sum / n}); }); return out; };

async function drawChart(keepView) {
  const box = $("#sx-chart"), msg = $("#sx-chartmsg"), note = $("#sx-chartnote");
  if (!box) return;
  if (typeof LightweightCharts === "undefined") { msg.textContent = "Chart library failed to load (needs internet access to cdn.jsdelivr.net)."; return; }
  const intraday = S.period === "1D" || S.period === "5D", period = S.period, symbol = S.sym;
  let points = [], previous = null, noteText = "";
  if (intraday) {
    try {
      const d = await api(`/stocks/intraday?market=${S.market}&symbol=${encodeURIComponent(symbol)}&range=${period.toLowerCase()}`);
      points = d.points; previous = d.previous_close; noteText = d.note;
    } catch (e) { noteText = e.message; }
    if (S.period !== period || S.sym !== symbol || !box.isConnected) return;
  }
  const visible = keepView && S.chart ? S.chart.api.timeScale().getVisibleLogicalRange() : null;
  destroyChart();
  const chart = LightweightCharts.createChart(box, {width: box.clientWidth, height: box.clientHeight,
    layout: {background: {color: css("--bg")}, textColor: css("--mute")}, grid: {vertLines: {color: "transparent"}, horzLines: {color: css("--line")}},
    rightPriceScale: {borderVisible: false}, timeScale: {borderVisible: false, timeVisible: intraday, secondsVisible: false}, crosshair: {mode: 0}});
  const observer = new ResizeObserver(() => box.clientWidth && chart.applyOptions({width: box.clientWidth, height: box.clientHeight}));
  observer.observe(box);
  S.chart = {api: chart, observer};

  let candles = [], line = points;
  if (!intraday) {
    const all = S.detail.chart, since = all.length ? cutoff(all[all.length - 1].time) : "";
    // Start on the last close before the window, so the drawn change matches the tile.
    let from = 0; while (from < all.length - 1 && all[from + 1].time <= since) from++;
    candles = all.slice(from);
    line = candles.map(c => ({time: c.time, value: c.close}));
  }
  const rising = line.length > 1 ? line[line.length - 1].value >= (previous ?? line[0].value) : (S.detail.row.change_pct || 0) >= 0;
  const color = css(rising ? "--ok" : "--bad");
  let main;
  if (S.style === "candles" && !intraday && S.detail.ohlc) {
    main = chart.addCandlestickSeries({upColor: css("--ok"), downColor: css("--bad"), borderVisible: false, wickUpColor: css("--ok"), wickDownColor: css("--bad")});
    main.setData(candles);
  } else {
    main = chart.addAreaSeries({lineColor: color, topColor: color + "55", bottomColor: color + "00", lineWidth: 2});
    main.setData(line.map(p => ({time: p.time, value: p.value})));
  }
  if (previous) main.createPriceLine({price: previous, color: css("--mute"), lineStyle: 1, lineWidth: 1, axisLabelVisible: true, title: "Prev close"});
  if (S.showVol && !intraday && line.length) {
    const volume = chart.addHistogramSeries({priceFormat: {type: "volume"}, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false});
    chart.priceScale("vol").applyOptions({scaleMargins: {top: .84, bottom: 0}});
    volume.setData(candles.map((c, i) => ({time: c.time, value: c.volume, color: (i && c.close < candles[i - 1].close ? css("--bad") : css("--ok")) + "66"})));
  }
  if (S.showMA && !intraday && candles.length) {
    for (const [n, shade] of [[50, "#2962ff"], [200, "#ff9800"]]) {
      const data = average(S.detail.chart, n).filter(p => p.time >= candles[0].time);
      if (data.length) chart.addLineSeries({color: shade, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, title: "MA " + n}).setData(data);
    }
  }
  if (visible) chart.timeScale().setVisibleLogicalRange(visible); else chart.timeScale().fitContent();
  msg.textContent = line.length >= 2 ? "" : intraday ? (line.length ? "Only one price so far for this period." : "No intraday prices yet for this period.") : "Not enough recorded history to draw this period yet.";
  note.textContent = noteText || (intraday ? "Times are exchange local time." : `${candles.length} daily closes.${S.market === "nse" ? " Recorded by this bot or imported; splits and bonus issues are not adjusted." : period === "ALL" ? " US history here starts in 1970 at the earliest." : ""}`);
}

const capBadge = (v, currency) => !has(v) || currency !== "USD" ? null : span(v >= 2e11 ? "MEGA CAP" : v >= 1e10 ? "LARGE CAP" : v >= 2e9 ? "MID CAP" : v >= 3e8 ? "SMALL CAP" : "MICRO CAP", "sx-cap");
function keyStats() {
  const r = S.detail.row, f = r.fund, m = r.metrics, c = r.currency;
  const stat = (label, value, badge) => h("div", "", h("small", "", label), h("b", "", value, badge || null));
  const amount = v => withUnit(compact(v), c), range = has(m.low_52w) && has(m.high_52w) ? `${fix(m.low_52w)} – ${fix(m.high_52w)}` : DASH;
  const yld = pct(get(r, "dividend.yield_pct"));
  const list = r.kind === "etf"
    ? [stat("Assets under management", amount(f.aum)), stat("Dividend yield (TTM)", yld), stat("Expense ratio", pct(f.expense_ratio_pct)), stat("Net asset value", amount(f.nav)), stat("Beta", fix(f.beta)), stat("Price to earnings", fix(f.pe)), stat("Category", f.category || DASH), stat("Fund family", f.fund_family || DASH), stat("Volume", compact(r.volume)), stat("Relative volume", fix(m.rel_vol)), stat("52-week range", range)]
    : [stat("Market capitalization", amount(f.market_cap), capBadge(f.market_cap, c)), stat("Dividend yield (TTM)", yld), stat("Price to earnings ratio (TTM)", fix(f.pe)), stat("Basic EPS (TTM)", amount(f.eps)), stat("Net income (TTM)", amount(f.net_income)), stat("Revenue (TTM)", amount(f.revenue)), stat("Shares float", compact(f.float_shares || f.shares_outstanding)), stat("Beta", fix(f.beta)), stat("Employees", compact(f.employees)), stat("Volume", compact(r.volume)), stat("Relative volume", fix(m.rel_vol)), stat("52-week range", range), stat("Sector", r.sector), stat("Industry", f.industry || DASH), stat("Analyst rating", span(f.analyst_rating || DASH, ratingTone(f.analyst_rating)))];
  return h("div", "sx-stats", list);
}

function tabOverview(box) {
  const wrap = h("div", "sx-chartwrap");
  const chartBox = h("div"); chartBox.id = "sx-chart";
  const msg = h("div", "sx-chartmsg"); msg.id = "sx-chartmsg";
  wrap.append(chartBox, msg);
  const toggle = (label, on, flip, disabled, why) => { const b = btn(label, "sx-btn" + (on ? " on" : ""), () => { flip(); renderDetail(); }); b.disabled = !!disabled; if (why) b.title = why; return b; };
  const tools = h("div", "sx-charttools",
    toggle("Area", S.style === "area", () => S.style = "area"),
    toggle("Candles", S.style === "candles", () => S.style = "candles", !S.detail.ohlc, S.detail.ohlc ? "Daily candles (1 month and longer)" : "The NSE feed has no open, high or low prices"),
    toggle("Moving averages", S.showMA, () => S.showMA = !S.showMA), toggle("Volume", S.showVol, () => S.showVol = !S.showVol),
    btn("Save image", "sx-btn", () => { if (!S.chart) return; const a = document.createElement("a"); a.href = S.chart.api.takeScreenshot().toDataURL("image/png"); a.download = `${S.sym}-${S.period}.png`; a.click(); }),
    btn("⛶ Full chart", "sx-btn", () => wrap.requestFullscreen ? wrap.requestFullscreen() : toast("Full screen is not available in this browser.")));
  const note = h("p", "sx-note"); note.id = "sx-chartnote";
  const stats = keyStats(); stats.id = "sx-stats";
  box.append(tools, wrap, periodTiles(), note, h("h2", "sx-section", "Key stats"), stats);
  if (S.market === "nse" && !Object.keys(S.detail.row.fund).length) box.append(h("p", "sx-note", "The NSE feed carries no company figures. Add them for this symbol in config/nse_fundamentals.csv."));
  if (S.detail.about) {
    box.append(h("h2", "sx-section", "About " + S.detail.row.name), h("p", "sx-about", S.detail.about));
    const site = safeUrl(S.detail.row.fund.website);
    if (site) { const a = h("a", "", site); a.href = site; a.target = "_blank"; a.rel = "noopener noreferrer"; box.append(a); }
  }
  drawChart();
}

/* Fetches a lazily loaded tab once, then renders it; ignores answers for a page already left. */
async function lazy(box, key, path, render) {
  const symbol = S.sym;
  if (!S.extra[key]) {
    box.replaceChildren(h("div", "sx-empty", "Loading…"));
    try { S.extra[key] = await api(path); }
    catch (e) { if (box.isConnected) box.replaceChildren(h("div", "sx-empty", e.message)); return; }
  }
  if (S.sym === symbol && box.isConnected) { box.replaceChildren(); render(S.extra[key]); }
}

function columns(periods, sets) {
  const top = Math.max(...sets.flatMap(s => s.values.map(v => Math.abs(v || 0))), 1);
  return h("div", "",
    h("div", "sx-legend", sets.map(s => { const sw = h("i"); sw.style.background = css(s.b ? "--ok" : "--acc"); return h("span", "", sw, s.name); }), span("red = negative")),
    h("div", "sx-columns", periods.map((p, i) => h("div", "", h("div", "bars", sets.map(s => { const v = s.values[i]; const bar = h("i", (v < 0 ? "neg" : s.b ? "b" : "")); bar.style.height = (Math.abs(v || 0) / top * 100) + "%"; bar.title = `${s.name}: ${compact(v)}`; return bar; })), h("small", "", p)))));
}
const GRAPH_LINES = {income: ["Total Revenue", "Net Income"], balance: ["Total Assets", "Total Liabilities Net Minority Interest"], cashflow: ["Operating Cash Flow", "Free Cash Flow"]};

function tabFinancials(box) {
  const key = "fin" + S.quarterly;
  lazy(box, key, `/stocks/financials?market=${S.market}&symbol=${encodeURIComponent(S.sym)}&quarterly=${S.quarterly}`, d => {
    if (d.fund) {
      box.append(h("div", "sx-grid",
        card("Top holdings (% of fund)", barList(d.fund.holdings.map(x => ({label: `${x.symbol} ${x.name}`, value: x.weight_pct})), {format: v => pct(v)})),
        card("Sector weights", barList(d.fund.sectors.map(x => ({label: x.name, value: x.weight_pct})), {format: v => pct(v, 1)}))));
      return;
    }
    if (!d.statements.length) { box.append(h("div", "sx-empty", d.note)); return; }
    box.append(h("div", "sx-group", [[false, "Annual"], [true, "Quarterly"]].map(([q, n]) => btn(n, "sx-btn" + (S.quarterly === q ? " on" : ""), () => { S.quarterly = q; renderDetail(); }))));
    d.statements.forEach(s => {
      const sets = GRAPH_LINES[s.key].map((name, i) => ({name, b: i === 1, values: (s.lines.find(l => l.name === name) || {values: []}).values})).filter(x => x.values.length);
      const head = h("tr", "", h("th", "l", S.detail.row.currency), s.periods.map(p => h("th", "", p)));
      const body = h("tbody", "", s.lines.map(l => h("tr", "", h("td", "", l.name), l.values.map(v => h("td", v < 0 ? "sx-down" : "", /EPS/.test(l.name) ? fix(v) : compact(v))))));
      box.append(h("h2", "sx-section", s.title), sets.length ? columns(s.periods, sets) : "", h("div", "sx-scroll", h("table", "sx-table", h("thead", "", head), body)));
    });
    box.append(h("p", "sx-note", "Source: Yahoo Finance. Check the company's filings before relying on any figure."));
  });
}

function tabNews(box) {
  lazy(box, "news", `/stocks/news?market=${S.market}&symbol=${encodeURIComponent(S.sym)}`, d => {
    if (d.note) box.append(h("p", "sx-note", d.note));
    const items = d.items.filter(n => safeUrl(n.url)).map(n => {
      const a = h("a", "", h("small", "", [n.source, (n.published || "").replace("T", " ").slice(0, 16)].filter(Boolean).join(" · ")), h("b", "", n.title), n.summary ? h("p", "", n.summary) : null);
      a.href = n.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      return a;
    });
    box.append(h("div", "sx-news", items));
  });
}

function gauge(title, rating) {
  const s = svg("svg", {viewBox: "0 0 200 112"});
  const point = (angle, r) => [100 - Math.cos(angle) * r, 100 - Math.sin(angle) * r];
  ["#f23645", "#f7857d", "#787b86", "#4fb3a3", "#089981"].forEach((color, i) => {
    const [x1, y1] = point(Math.PI * i / 5 + .02, 82), [x2, y2] = point(Math.PI * (i + 1) / 5 - .02, 82);
    s.append(svg("path", {d: `M${x1} ${y1}A82 82 0 0 1 ${x2} ${y2}`, fill: "none", stroke: color, "stroke-width": 10}));
  });
  if (has(rating.score)) {
    const [x, y] = point((rating.score + 1) / 2 * Math.PI, 66);
    s.append(svg("line", {x1: 100, y1: 100, x2: x, y2: y, stroke: css("--text"), "stroke-width": 3, "stroke-linecap": "round"}), svg("circle", {cx: 100, cy: 100, r: 5, fill: css("--text")}));
  }
  return h("div", "sx-card sx-gauge", h("h3", "", title), s, h("b", ratingTone(rating.label), rating.label || "Not enough history"),
    h("div", "sx-counts", [["Sell", rating.sell], ["Neutral", rating.neutral], ["Buy", rating.buy]].map(([n, v]) => h("span", "", h("b", "", String(v)), n))));
}
function signalTable(title, list) {
  return card(title, list.length ? h("table", "sx-kv", h("tbody", "", list.map(i => h("tr", "", h("td", "", i.name), h("td", "", fix(i.value), "  ", span(i.signal, ratingTone(i.signal))))))) : h("p", "sx-mute", "Needs more price history."));
}
function tabTechnicals(box) {
  const t = S.detail.technicals;
  box.append(h("div", "sx-grid", gauge("Oscillators", t.osc_rating), gauge("Summary", t.summary), gauge("Moving averages", t.ma_rating)),
    h("div", "sx-grid", signalTable("Oscillators", t.oscillators), signalTable("Moving averages", t.moving_averages)),
    h("p", "sx-note", "A count of buy and sell signals on daily prices, calculated by this bot. It is not an analyst opinion or a forecast."));
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const YEAR_COLORS = ["#2962ff", "#ff9800", "#22ab94", "#e040fb", "#f7525f", "#8c8c8c"];
function seasonChart(years) {
  const paths = years.map(y => { let level = 1; const pts = [[0, 0]]; y.months.forEach((m, i) => { if (m !== null) { level *= 1 + m / 100; pts.push([i + 1, (level - 1) * 100]); } }); return pts; });
  const values = paths.flat().map(p => p[1]), lo = Math.min(...values, 0), hi = Math.max(...values, 0), range = (hi - lo) || 1;
  const W = 900, H = 280, X = i => 46 + i / 12 * (W - 56), Y = v => 10 + (hi - v) / range * (H - 34);
  const s = svg("svg", {viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none"});
  const text = (x, y, value) => { const t = svg("text", {x, y, fill: css("--mute"), "font-size": 11}); t.textContent = value; s.append(t); };
  [lo, 0, hi].forEach(v => { s.append(svg("line", {x1: 46, x2: W - 10, y1: Y(v), y2: Y(v), stroke: css("--line")})); text(2, Y(v) + 4, fix(v, 0) + "%"); });
  MONTHS.forEach((m, i) => text(X(i + .6), H - 4, m));
  paths.forEach((pts, i) => s.append(svg("polyline", {fill: "none", stroke: YEAR_COLORS[i], "stroke-width": i ? 1.4 : 2.8, points: pts.map(p => `${X(p[0])},${Y(p[1])}`).join(" ")})));
  return h("div", "sx-seasons", h("div", "sx-legend", years.map((y, i) => { const sw = h("i"); sw.style.background = YEAR_COLORS[i]; return h("span", "", sw, String(y.year)); })), s);
}
function tabSeasonals(box) {
  const years = S.detail.seasonals;
  if (!years.length) { box.append(h("div", "sx-empty", "Seasonals need at least two months of price history.")); return; }
  const averages = MONTHS.map((_, i) => { const v = years.map(y => y.months[i]).filter(has); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; });
  const line = (label, months) => h("tr", "", h("td", "", label), months.map(v => h("td", tone(v), signed(v, 1))));
  box.append(seasonChart(years), h("h2", "sx-section", "Monthly change"),
    h("div", "sx-scroll", h("table", "sx-table", h("thead", "", h("tr", "", h("th", "l", "Year"), MONTHS.map(m => h("th", "", m)))), h("tbody", "", years.map(y => line(String(y.year), y.months)), line("Average", averages)))),
    h("p", "sx-note", "A few years of months is a very small sample: do not read a reliable pattern into it."));
}

const facts = (title, pairs) => card(title, h("table", "sx-kv", h("tbody", "", pairs.map(([k, v]) => h("tr", "", h("td", "", k), h("td", "", has(v) ? v : DASH))))));
function tabDividends(box) {
  const r = S.detail.row, d = r.dividend;
  if (!d) { box.append(h("div", "sx-empty", S.market === "nse" ? "No NSE dividend feed exists here. Enter figures under nse_dividends in config/stocks.yaml or in config/nse_fundamentals.csv." : "No dividend payments found for this security.")); return; }
  const grid = h("div", "sx-grid", facts("Dividend summary", [["Yield (12 months)", pct(d.yield_pct)], ["Per share (12 months)", `${r.currency} ${fix(d.annual_per_share, 4)}`], ["Last ex-date", d.last_ex_date ? `${d.last_ex_date} (${fix(d.last_amount, 4)})` : null], ["Payments, last 12 months", d.payments_last_year], ["Growth per year", has(d.annual_growth_pct) ? signed(d.annual_growth_pct, 1) : null], ["Payout ratio", has(r.fund.payout_ratio_pct) ? pct(r.fund.payout_ratio_pct) : null], ["Book closure", d.book_closure || null], ["Note", d.note || null], ["Source", d.source]]));
  if (d.payments) grid.append(card("Recent payments (ex-date)", h("table", "sx-kv", h("tbody", "", d.payments.map(p => h("tr", "", h("td", "", p.date), h("td", "", fix(p.amount, 4))))))));
  box.append(grid);
  if (d.by_year) { const years = d.by_year.slice(-12); box.append(h("h2", "sx-section", "Paid per share by year"), columns(years.map(y => y.year + (y.complete ? "" : "*")), [{name: "Dividends per share", values: years.map(y => y.total)}]), h("p", "sx-note", "* not a complete year of data")); }
}

function tabAnalysis(box) {
  const r = S.detail.row, a = r.analysis, o = a.outlook, grid = h("div", "sx-grid");
  if (a.enough_history) {
    grid.append(facts("Buy score", [["Score", a.buy_score + " / 100"], ["Why", a.buy_reasons.join(" · ")], ["Volatility (yearly)", pct(a.volatility_pct, 1)], ["Worst fall in the last year of data", pct(a.max_drawdown_1y_pct, 1)], ["Below that period's high", pct(a.from_high_pct, 1)]]));
    grid.append(o ? facts("Outlook: past frequency, not a forecast", [["Higher 21 trading days later", `${o.rose_pct}% of ${o.matches} similar days`], ["Same figure for all days", o.all_days_rose_pct + "%"], ["Difference", signed(o.edge_pct, 1)], ["Median move", signed(o.median_move_pct, 1)], ["Independent samples", o.independent_samples + (o.reliable ? "" : " (too few to rely on)")]]) : facts("Outlook", [["Status", "Needs about 16 months of daily history"]]));
  } else grid.append(facts("Buy score", [["Status", `Needs 60 days of price history; ${a.bars} available`]]));
  if (has(r.match_score)) grid.append(facts("Sokowatch screener (profile fit)", [["Verdict", r.verdict], ["Match score", r.match_score + " / 100"], ["Why", r.match_reasons.join(" · ")], ["Stop-loss level (a risk rule, not a forecast)", "KES " + fix(r.stop_loss)]]));
  if (S.detail.speculative.length) grid.append(facts("Speculative: HIGH RISK", S.detail.speculative.map((t, i) => [String(i + 1), t])));
  box.append(grid);
}

/* ---------- live refresh ---------- */
async function refreshDetail() {
  const symbol = S.sym, req = S.dreq;
  S.busy = true;
  try {
    const d = await api(`/stocks/detail?market=${S.market}&symbol=${encodeURIComponent(symbol)}&risk=${risk()}&live=true`);
    if (req !== S.dreq || S.sym !== symbol || !S.detail) return;
    const before = S.detail.row.price;
    S.detail = d; S.detailAt = S.at = Date.now();
    // Update in place so an open tab, scroll position and chart zoom are left alone.
    const block = priceBlock(); block.id = "sx-priceblock"; $("#sx-priceblock").replaceWith(block);
    if (d.row.price !== before) { const b = block.querySelector("b"); b.className = d.row.price > before ? "flash-up" : "flash-down"; setTimeout(() => b.className = "", 900); }
    const badge = root.querySelector(".sx-live"); if (badge) badge.replaceWith(liveBadge());
    if (S.dtab === "overview" && $("#sx-chart")) {
      $("#sx-periods").replaceWith(periodTiles());
      const stats = keyStats(); stats.id = "sx-stats"; $("#sx-stats").replaceWith(stats);
      if (!document.fullscreenElement) await drawChart(true);
    }
  } catch (e) { S.detailAt = Date.now(); if (e.message !== "unauthorized") toast("Live update failed: " + e.message); }
  finally { S.busy = false; }
}
const active = () => KEY && !$("#app").hidden && !root.hidden && !document.hidden;

/* The console's own auto-refresh skips this tab (it would redraw the whole screener), so keep the
   trading header and mode bar current from here, at the interval chosen in the header. */
let statusAt = 0;
async function refreshTradingStatus() {
  const every = +$("#refreshsel").value;
  if (!every || Date.now() - statusAt < Math.max(every, 5) * 1000) return;
  statusAt = Date.now();
  try {
    const [o, live] = await Promise.all([api("/overview"), api("/live")]);
    T.ov = o; T.live = live; modeBar(o); engPill(o.engine); renderHeader();
    $("#clock").textContent = new Date().toLocaleTimeString(); setDot("on");
  } catch (e) { if (e.message !== "unauthorized") setDot("err"); }
}
setInterval(() => {
  if (!active()) return;
  refreshTradingStatus();
  if (S.busy) return;
  if (S.sym) { if (S.detail && Date.now() - S.detailAt >= REFRESH_MS) refreshDetail(); return; }
  const waiting = S.data && S.data.meta.fundamentals_waiting;
  if (S.data && Date.now() - S.at >= (waiting ? FILL_MS : REFRESH_MS)) loadList(true);
}, 1000);

/* ---------- hooking into the console ---------- */
const route = () => { const m = location.hash.match(/^#stocks\/(nse|us)\/([^/]+)$/); return m ? {market: m[1], symbol: decodeURIComponent(m[2])} : null; };
addEventListener("popstate", () => {
  if (root.hidden) return;
  const to = route();
  if (to) { if (to.market !== S.market) { S.market = to.market; S.data = null; } if (to.symbol !== S.sym) openSymbol(to.symbol, true); }
  else if (S.sym) closeSymbol(true);
});

// Called by the console when the tab is shown, on resize and on a theme change.
LOADERS.stocks = async () => {
  const to = route();
  if (to && !S.sym) { S.market = to.market; return openSymbol(to.symbol, true); }
  if (S.sym) { if (S.detail) renderDetail(); return; }
  renderList();
  if (!S.data) loadList();
};

const consoleShow = show;
let firstShow = true;
show = t => {
  if (firstShow && route()) t = "stocks";          // reload or bookmark of a security page
  firstShow = false;
  document.body.classList.toggle("stocks-mode", t === "stocks");
  if (tab === "stocks" && t !== "stocks" && location.hash.startsWith("#stocks")) history.replaceState(null, "", location.pathname + location.search);
  consoleShow(t);
};
})();
