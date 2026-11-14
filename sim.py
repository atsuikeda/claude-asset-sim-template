"""ペーパートレード・シミュレーター(実際のお金は動かない)。

判断は Claude、ルールの強制はこのコードが行う。標準ライブラリのみで動く。
売買できる市場、株数単位、コスト、上限ルールは config.json で決める(初期値は楽天証券の条件)。

  python sim.py init                      仮想資金(config.json の initial_jpy)で開始
  python sim.py rules                     いま有効なルールを表示
  python sim.py quote SYMBOL [SYMBOL...]  現在値、騰落率、最低購入額
  python sim.py order buy  SYMBOL --shares 2 --reason "..."
  python sim.py order sell SYMBOL --shares 1 --reason "..."
  python sim.py order sell SYMBOL --all      --reason "..."
  python sim.py cancel ORDER_ID
  python sim.py settle                    約定処理 + 評価額の記録 + REPORT.md 更新
  python sim.py status                    現在の状態を表示
  python sim.py resume                    停止状態の解除(人間専用)
"""
import argparse
import csv
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---- ハードルール(Claude の判断より優先) -------------------------------
# 数字は config.json で決める。config.json が無い・項目が足りないときは、ここの初期値を使う。
DEFAULTS = {
    "initial_jpy": 300_000,       # 仮想資金
    "benchmark": "ACWI",          # 比較対象: 全世界株式(オルカン相当)を買って放置
    "max_position_pct": 100,      # 1銘柄の上限(総資産比 %)
    "max_orders_per_window": 3,   # 期間内の注文数の上限
    "order_window_days": 6,       # 注文数を数える期間。週1回の実行で前週の注文と重ならないよう7日より短くする
    "min_reason_chars": 20,       # 注文理由の最小文字数
    "slippage_pct": 0.1,          # 約定価格のずれ(不利な方向 %)
    "halt_drawdown_pct": 30,      # 最高値からこれだけ下げたら新規買いを停止
    "order_expiry_days": 10,      # 約定しない注文の失効日数
    "allowed_types": ["EQUITY", "ETF"],
    # 売買できる市場: Yahoo Finance の銘柄コードの末尾 -> 市場名 / 売買単位(株) / 片道コスト(%)
    # 初期値は楽天証券の条件。コストは売買手数料と為替コストの概算。日本株はかぶミニ(1株から、手数料無料)を前提にする。
    "markets": {
        "":    {"name": "米国", "lot": 1, "cost_pct": 0.6},
        ".T":  {"name": "日本", "lot": 1, "cost_pct": 0.2},
        ".HK": {"name": "香港", "lot": 100, "cost_pct": 1.0},
        ".SS": {"name": "上海", "lot": 100, "cost_pct": 1.0},
        ".SI": {"name": "シンガポール", "lot": 100, "cost_pct": 1.0},
        ".JK": {"name": "インドネシア", "lot": 100, "cost_pct": 1.0},
        ".BK": {"name": "タイ", "lot": 100, "cost_pct": 1.0},
        ".KL": {"name": "マレーシア", "lot": 100, "cost_pct": 1.0},
    },
}


def load_config(path):
    cfg = dict(DEFAULTS)
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            sys.exit(f"config.json を読めません。カンマや引用符の抜けがないか確かめてください ({e})")
        cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    return cfg


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE = DATA / "state.json"
PENDING = DATA / "pending.json"
LEDGER = DATA / "ledger.jsonl"
HISTORY = DATA / "history.csv"
REPORT = ROOT / "REPORT.md"
CONFIG = load_config(ROOT / "config.json")
INITIAL_JPY = CONFIG["initial_jpy"]
BENCHMARK = CONFIG["benchmark"]
MAX_POSITION_PCT = CONFIG["max_position_pct"]
MAX_ORDERS_PER_WEEK = CONFIG["max_orders_per_window"]
ORDER_WINDOW_DAYS = CONFIG["order_window_days"]
MIN_REASON_CHARS = CONFIG["min_reason_chars"]
SLIPPAGE_PCT = CONFIG["slippage_pct"]
HALT_DRAWDOWN_PCT = CONFIG["halt_drawdown_pct"]
ORDER_EXPIRY_DAYS = CONFIG["order_expiry_days"]
ALLOWED_TYPES = set(CONFIG["allowed_types"])
MARKETS = {k: (v["name"], v["lot"], v["cost_pct"]) for k, v in CONFIG["markets"].items()}
JST = timezone(timedelta(hours=9))
# 補助通貨単位で値付けされる市場(ロンドンのペンスなど)
SUBUNITS = {"GBp": ("GBP", 100), "GBX": ("GBP", 100), "ZAc": ("ZAR", 100), "ILA": ("ILS", 100)}

_cache = {}


def market(symbol):
    """(市場名, 売買単位, 片道コスト %)。取扱市場外なら None。"""
    suffix = symbol[symbol.rfind("."):] if "." in symbol else ""
    return MARKETS.get(suffix)


def fetch(symbol, rng="1y"):
    """Yahoo Finance の日足を取得する。"""
    key = (symbol, rng)
    if key in _cache:
        return _cache[key]
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?range={rng}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as res:
        result = json.load(res)["chart"]["result"][0]
    meta = result["meta"]
    currency, scale = SUBUNITS.get(meta["currency"], (meta["currency"], 1))
    q = result["indicators"]["quote"][0]
    bars = [(ts, o / scale, c / scale)
            for ts, o, c in zip(result.get("timestamp", []), q["open"], q["close"])
            if o and c]
    if not bars:
        raise ValueError(f"{symbol}: 価格データがありません")
    data = {"symbol": symbol, "currency": currency, "bars": bars,
            "type": meta.get("instrumentType"),
            "name": meta.get("longName") or meta.get("shortName") or symbol}
    _cache[key] = data
    return data


def fx(currency, ts=None):
    """1通貨あたりの円。ts を指定するとその時点以前で最も近い終値。"""
    if currency == "JPY":
        return 1.0
    bars = fetch(f"{currency}JPY=X")["bars"]
    if ts is not None:
        earlier = [b for b in bars if b[0] <= ts + 86400]
        if earlier:
            return earlier[-1][2]
    return bars[-1][2]


def price_jpy(symbol):
    d = fetch(symbol)
    return d["bars"][-1][2] * fx(d["currency"])


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def log(event, **fields):
    """台帳は追記のみ。後から書き換えない。"""
    row = {"ts": int(time.time()), "at": now_str(), "event": event, **fields}
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def ledger():
    if not LEDGER.exists():
        return []
    return [json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines() if line]


def now_str():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M")


def need_state():
    if not STATE.exists():
        sys.exit("未初期化です。先に python sim.py init を実行してください。")
    return load(STATE, None)


def valuation(state):
    """(総資産, {銘柄: 評価額}, ベンチマーク評価額) を円で返す。"""
    values = {s: p["qty"] * price_jpy(s) for s, p in state["positions"].items()}
    bench = state["benchmark"]
    bench_value = bench["qty"] * price_jpy(bench["symbol"]) if bench["qty"] else state["initial_jpy"]
    return state["cash_jpy"] + sum(values.values()), values, bench_value


def orders_this_week():
    since = time.time() - ORDER_WINDOW_DAYS * 86400
    return sum(1 for e in ledger() if e["event"] == "order" and e["ts"] > since)


def reject(msg):
    log("reject", message=msg)
    sys.exit(f"注文拒否: {msg}")


# ---- コマンド ------------------------------------------------------------

def cmd_init(args):
    if STATE.exists():
        sys.exit("すでに初期化済みです。やり直す場合は data フォルダを人間が削除してください。")
    DATA.mkdir(exist_ok=True)
    (DATA / "journal").mkdir(exist_ok=True)
    fetch(BENCHMARK)  # 取得できることを確認
    save(STATE, {
        "initial_jpy": INITIAL_JPY, "cash_jpy": INITIAL_JPY, "peak_jpy": INITIAL_JPY,
        "started_ts": int(time.time()), "started_at": now_str(), "halted": False,
        "positions": {}, "benchmark": {"symbol": BENCHMARK, "qty": 0},
    })
    save(PENDING, [])
    log("init", initial_jpy=INITIAL_JPY, benchmark=BENCHMARK)
    print(f"仮想資金 {INITIAL_JPY:,} 円で開始しました。比較対象: {BENCHMARK} を買って放置。")


def cmd_quote(args):
    for symbol in args.symbols:
        try:
            d = fetch(symbol)
        except Exception as e:
            print(f"{symbol}: 取得失敗 ({e})")
            continue
        closes = [b[2] for b in d["bars"]]
        last = closes[-1]
        last_jpy = last * fx(d["currency"])

        def chg(n):
            return f"{(last / closes[-n - 1] - 1) * 100:+.1f}%" if len(closes) > n else "n/a"

        mk = market(symbol)
        tradable = (f"{mk[0]} / {mk[1]}株単位 / 最低購入額 約{last_jpy * mk[1]:,.0f} 円"
                    if mk else "取扱市場外のため買えない(config.json の markets にない)")
        print(f"{symbol} [{d['type']}] {d['name']}")
        print(f"  終値 {last:,.2f} {d['currency']} (= {last_jpy:,.0f} 円) / 1か月 {chg(21)} / 3か月 {chg(63)}"
              f" / 1年 {(last / closes[0] - 1) * 100:+.1f}%")
        print(f"  1年高値比 {(last / max(closes) - 1) * 100:+.1f}% / 1年安値比 {(last / min(closes) - 1) * 100:+.1f}%")
        print(f"  {tradable}")


def cmd_order(args):
    state = need_state()
    pending = load(PENDING, [])
    symbol = args.symbol.upper()
    if len(args.reason.strip()) < MIN_REASON_CHARS:
        reject(f"理由は{MIN_REASON_CHARS}文字以上で書いてください")
    if orders_this_week() >= MAX_ORDERS_PER_WEEK:
        reject(f"直近{ORDER_WINDOW_DAYS}日間の注文数が上限({MAX_ORDERS_PER_WEEK}件)に達しています")
    mk = market(symbol)
    if not mk:
        reject(f"{symbol} は取扱市場外です(config.json の markets にありません)。米国上場のADRがあればそちらを使ってください")
    try:
        d = fetch(symbol)
    except Exception as e:
        reject(f"{symbol} の価格を取得できません ({e})")
    if d["type"] not in ALLOWED_TYPES:
        reject(f"{symbol} は対象外の商品です ({d['type']})。現物の株式とETFのみ")
    if any(o["symbol"] == symbol for o in pending):
        reject(f"{symbol} には約定待ちの注文があります")

    order = {"id": f"{datetime.now(JST):%Y%m%d-%H%M%S}-{symbol}", "ts": int(time.time()),
             "side": args.side, "symbol": symbol, "reason": args.reason.strip()}
    if args.side == "buy":
        if state["halted"]:
            reject("停止中のため新規買いはできません(人間による resume が必要)")
        if not args.shares or args.shares <= 0:
            reject("買い注文は --shares で株数を指定してください")
        if args.shares % mk[1]:
            reject(f"{mk[0]}株は{mk[1]}株単位です")
        est = args.shares * price_jpy(symbol) * (1 + (mk[2] + SLIPPAGE_PCT) / 100)
        reserved = sum(o["est_jpy"] for o in pending if o["side"] == "buy")
        if est > state["cash_jpy"] - reserved:
            reject(f"現金不足です(必要 約{est:,.0f} 円 / 使える現金 {state['cash_jpy'] - reserved:,.0f} 円)")
        equity, values, _ = valuation(state)
        after = values.get(symbol, 0) + est
        if after > equity * MAX_POSITION_PCT / 100:
            reject(f"1銘柄の上限 {MAX_POSITION_PCT}% を超えます"
                   f"(上限 {equity * MAX_POSITION_PCT / 100:,.0f} 円 / 注文後 約{after:,.0f} 円)")
        order["shares"] = args.shares
        order["est_jpy"] = round(est)
    else:
        pos = state["positions"].get(symbol)
        if not pos:
            reject(f"{symbol} を保有していません")
        if args.all:
            order["shares"] = pos["qty"]
        elif not args.shares or not 0 < args.shares <= pos["qty"]:
            reject(f"売り注文は --shares で 1〜{pos['qty']} 株、または --all を指定してください")
        else:
            order["shares"] = args.shares

    pending.append(order)
    save(PENDING, pending)
    log("order", **{k: v for k, v in order.items() if k != "ts"})
    print(f"注文を受け付けました: {order['id']} {args.side} {symbol} {order['shares']}株。"
          f"次の取引日の始値で約定します。")


def cmd_cancel(args):
    pending = load(PENDING, [])
    rest = [o for o in pending if o["id"] != args.id]
    if len(rest) == len(pending):
        sys.exit(f"注文 {args.id} は見つかりません")
    save(PENDING, rest)
    log("cancel", id=args.id)
    print(f"注文 {args.id} を取り消しました。")


def next_open(symbol, after_ts):
    """after_ts より後に始まった最初の取引日の (時刻, 始値, 通貨)。"""
    d = fetch(symbol, "3mo")
    for ts, o, _ in d["bars"]:
        if ts > after_ts:
            return ts, o, d["currency"]
    return None


def cmd_settle(args):
    state = need_state()

    # ベンチマークはオルカン相当の投資信託を想定し、金額指定・手数料なしで買う
    bench = state["benchmark"]
    if not bench["qty"]:
        hit = next_open(bench["symbol"], state["started_ts"])
        if hit:
            ts, o, cur = hit
            px = o * fx(cur, ts)
            bench["qty"] = state["initial_jpy"] / px
            log("benchmark_fill", symbol=bench["symbol"], qty=bench["qty"], price_jpy=px)

    remaining = []
    for o in load(PENDING, []):
        try:
            hit = next_open(o["symbol"], o["ts"])
        except Exception as e:
            print(f"{o['symbol']}: 価格取得に失敗したため次回に持ち越します ({e})")
            hit = None
        if not hit:
            if time.time() - o["ts"] > ORDER_EXPIRY_DAYS * 86400:
                log("expire", id=o["id"], symbol=o["symbol"])
            else:
                remaining.append(o)
            continue
        ts, open_px, cur = hit
        _, lot, cost_pct = market(o["symbol"])
        rate = fx(cur, ts)
        pos = state["positions"].get(o["symbol"])
        if o["side"] == "buy":
            px = open_px * (1 + SLIPPAGE_PCT / 100) * rate
            unit = px * (1 + cost_pct / 100)
            shares = min(o["shares"], math.floor(state["cash_jpy"] / unit / lot) * lot)
            if shares <= 0:
                log("skip", id=o["id"], symbol=o["symbol"], message="始値で現金が足りず約定せず")
                continue
            jpy = shares * unit
            pos = pos or {"qty": 0, "cost_jpy": 0, "first_bought": now_str()[:10]}
            pos["qty"] += shares
            pos["cost_jpy"] += jpy
            state["positions"][o["symbol"]] = pos
            state["cash_jpy"] -= jpy
            log("fill", id=o["id"], side="buy", symbol=o["symbol"], shares=shares,
                price_jpy=px, jpy=jpy)
        elif pos:
            shares = min(o["shares"], pos["qty"])
            px = open_px * (1 - SLIPPAGE_PCT / 100) * rate
            jpy = shares * px * (1 - cost_pct / 100)
            cost = pos["cost_jpy"] * shares / pos["qty"]
            pos["qty"] -= shares
            pos["cost_jpy"] -= cost
            if pos["qty"] <= 0:
                del state["positions"][o["symbol"]]
            state["cash_jpy"] += jpy
            log("fill", id=o["id"], side="sell", symbol=o["symbol"], shares=shares,
                price_jpy=px, jpy=jpy, realized_jpy=jpy - cost)
    save(PENDING, remaining)

    equity, values, bench_value = valuation(state)
    state["peak_jpy"] = max(state["peak_jpy"], equity)
    if not state["halted"] and equity <= state["peak_jpy"] * (1 - HALT_DRAWDOWN_PCT / 100):
        state["halted"] = True
        log("halt", equity=equity, peak=state["peak_jpy"])
    save(STATE, state)

    today = datetime.now(JST).strftime("%Y-%m-%d")
    rows = []
    if HISTORY.exists():
        with HISTORY.open(encoding="utf-8", newline="") as f:
            rows = [r for r in csv.reader(f)][1:]
    rows = [r for r in rows if r[0] != today]
    rows.append([today, f"{equity:.0f}", f"{bench_value:.0f}", f"{state['cash_jpy']:.0f}"])
    with HISTORY.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "portfolio_jpy", "benchmark_jpy", "cash_jpy"])
        w.writerows(rows)

    text = render(state, equity, values, bench_value, remaining)
    REPORT.write_text(text, encoding="utf-8")
    print(text)


def render(state, equity, values, bench_value, pending):
    init = state["initial_jpy"]
    lines = [
        "# 運用レポート(ペーパートレード)", "",
        f"更新: {now_str()} / 開始: {state['started_at']}", "",
        "| | 評価額 | 開始からの増減 |", "|---|---:|---:|",
        f"| Claude の運用 | {equity:,.0f} 円 | {(equity / init - 1) * 100:+.2f}% |",
        f"| オルカン相当({state['benchmark']['symbol']})を買って放置 | {bench_value:,.0f} 円 | {(bench_value / init - 1) * 100:+.2f}% |",
        "",
        f"差: {(equity - bench_value) / init * 100:+.2f} ポイント / "
        f"最高値からの下落: {(equity / state['peak_jpy'] - 1) * 100:.1f}% / "
        f"直近{ORDER_WINDOW_DAYS}日の注文: {orders_this_week()}/{MAX_ORDERS_PER_WEEK} 件"
        + (" / **停止中(新規買い不可)**" if state["halted"] else ""),
        "", "## 保有", "",
        "| 銘柄 | 株数 | 評価額 | 比率 | 取得額 | 損益 | 初回購入 |", "|---|---:|---:|---:|---:|---:|---|",
    ]
    for s, p in sorted(state["positions"].items(), key=lambda kv: -values[kv[0]]):
        v = values[s]
        lines.append(f"| {s} | {p['qty']} | {v:,.0f} 円 | {v / equity * 100:.0f}% | {p['cost_jpy']:,.0f} 円 | "
                     f"{(v / p['cost_jpy'] - 1) * 100:+.1f}% | {p.get('first_bought', '')} |")
    lines.append(f"| 現金 | | {state['cash_jpy']:,.0f} 円 | {state['cash_jpy'] / equity * 100:.0f}% | | | |")
    if pending:
        lines += ["", "## 約定待ち", ""]
        for o in pending:
            lines.append(f"- {o['id']} {o['side']} {o['symbol']} {o['shares']}株: {o['reason']}")
    fills = [e for e in ledger() if e["event"] == "fill"][-5:]
    if fills:
        lines += ["", "## 直近の約定", ""]
        for e in reversed(fills):
            lines.append(f"- {e['at']} {e['side']} {e['symbol']} {e['shares']}株 {e['jpy']:,.0f} 円")
    return "\n".join(lines) + "\n"


def cmd_rules(args):
    print(f"仮想資金 {INITIAL_JPY:,} 円 / 比較対象 {BENCHMARK}")
    print(f"1銘柄の上限 総資産の{MAX_POSITION_PCT}% / 注文は直近{ORDER_WINDOW_DAYS}日間で{MAX_ORDERS_PER_WEEK}件まで"
          f" / 理由は{MIN_REASON_CHARS}文字以上")
    print(f"価格ずれ {SLIPPAGE_PCT}% / 最高値から{HALT_DRAWDOWN_PCT}%下げたら新規買い停止 / 約定待ちは{ORDER_EXPIRY_DAYS}日で失効")
    print("売買できる市場(銘柄コードの末尾 / 売買単位 / 片道コスト):")
    for suffix, (name, lot, cost) in MARKETS.items():
        print(f"  {suffix or '(なし)'} {name} / {lot}株単位 / {cost}%")


def cmd_status(args):
    state = need_state()
    equity, values, bench_value = valuation(state)
    print(render(state, equity, values, bench_value, load(PENDING, [])))


def cmd_resume(args):
    state = need_state()
    state["halted"] = False
    state["peak_jpy"] = valuation(state)[0]
    save(STATE, state)
    log("resume")
    print("停止を解除しました。")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="ペーパートレード・シミュレーター")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    q = sub.add_parser("quote")
    q.add_argument("symbols", nargs="+")
    q.set_defaults(fn=cmd_quote)
    o = sub.add_parser("order")
    o.add_argument("side", choices=["buy", "sell"])
    o.add_argument("symbol")
    o.add_argument("--shares", type=int)
    o.add_argument("--all", action="store_true")
    o.add_argument("--reason", required=True)
    o.set_defaults(fn=cmd_order)
    c = sub.add_parser("cancel")
    c.add_argument("id")
    c.set_defaults(fn=cmd_cancel)
    sub.add_parser("settle").set_defaults(fn=cmd_settle)
    sub.add_parser("rules").set_defaults(fn=cmd_rules)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
