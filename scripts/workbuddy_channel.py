"""WorkBuddy 决策通道 —— 不调用付费 LLM API，由 WorkBuddy(我) 现场做投资决策跑回测。

与 TradingAgents 的 API 通道(DeepSeek/GLM/… )完全并存、互不影响：本脚本是**独立**的
轻量工具，只做确定性的事(取数、算指标、账户/仓位/风控的账本数学)，"买卖决策"由我
在对话里现场拍板后填入 DECISIONS，最后输出一张逐日决策表。

用法:
    python scripts/workbuddy_channel.py <TICKER> <START> <END> [--init 1000000]

示例:
    python scripts/workbuddy_channel.py 600396.SS 2026-07-01 2026-07-23
"""
import argparse
import csv
import glob
import os
import sys

# ---------------------------------------------------------------------------
# 确定性账本参数(与引擎保持一致的口径)
# ---------------------------------------------------------------------------
BUY_COMM = 0.0003      # 买入佣金
SELL_COMM = 0.0003     # 卖出佣金
STAMP = 0.0005         # 卖出印花税(A股)
LOT = 100              # 一手 100 股


def _sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _sd(xs, n):
    if len(xs) < n:
        return None
    m = sum(xs[-n:]) / n
    return (sum((v - m) ** 2 for v in xs[-n:]) / n) ** 0.5


def _rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(-n, 0):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    ag, al = gains / n, losses / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def load_prices(ticker, start, end):
    """优先读本地缓存 CSV，找不到则报错提示先跑一次 API 通道预热数据。"""
    pattern = os.path.join("webapp", "data", "graph_cache", f"{ticker}-YFin-data-*.csv")
    candidates = sorted(glob.glob(pattern))
    if not candidates:
        sys.exit(f"[workbuddy-channel] 找不到 {ticker} 的缓存 CSV，请先运行一次数据预热 "
                 f"(或先跑一次 API 通道回测让 {pattern} 生成)。")
    path = candidates[-1]  # 取最新缓存
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append((r["Date"], float(r["Close"])))
    window = [(d, c) for d, c in rows if start <= d <= end]
    if not window:
        sys.exit(f"[workbuddy-channel] 缓存中没有 {start}~{end} 的数据。")
    return path, rows, window


def indicators(rows, window):
    """为窗口内每个交易日计算 SMA20 / Bollinger下轨 / RSI14 / 涨跌幅。"""
    closes = [c for _, c in rows]
    out = []
    for d, c in window:
        i = rows.index((d, c))
        prev = closes[i - 1] if i > 0 else c
        chg = (c / prev - 1) * 100
        s20 = _sma(closes[: i + 1], 20)
        sd = _sd(closes[: i + 1], 20)
        blb = s20 - 2 * sd if s20 is not None and sd is not None else None
        r = _rsi(closes[: i + 1])
        out.append({"date": d, "close": c, "chg": chg, "sma20": s20,
                    "boll_lb": blb, "rsi": r})
    return out


# ---------------------------------------------------------------------------
# 账户/仓位/风控的确定性数学(不碰任何 LLM)
# ---------------------------------------------------------------------------
class Account:
    def __init__(self, init_cash):
        self.cash = float(init_cash)
        self.shares = 0
        self.cost = 0.0
        self.fees = 0.0

    def equity(self, px):
        return self.cash + self.shares * px

    def buy(self, px, cash_frac):
        amt = self.cash * cash_frac
        n = int(amt / (px * (1 + BUY_COMM)) // LOT * LOT)
        if n <= 0:
            return 0
        fee = n * px * BUY_COMM
        self.cash -= n * px + fee
        self.fees += fee
        self.cost = ((self.shares * self.cost + n * px) / (self.shares + n)
                     if self.shares + n > 0 else px)
        self.shares += n
        return n

    def sell(self, px, pos_frac):
        n = int(self.shares * pos_frac // LOT * LOT)
        if n <= 0:
            return 0
        fee = n * px * (SELL_COMM + STAMP)
        self.cash += n * px - fee
        self.fees += fee
        self.shares -= n
        return n


def run(ticker, start, end, init_cash, decisions):
    """decisions: {(date, 'buy', cash_frac|None, reason) / (date, 'sell', pos_frac, reason)}"""
    path, rows, window = load_prices(ticker, start, end)
    ind = indicators(rows, window)
    acct = Account(init_cash)
    ledger = []
    for k, info in enumerate(ind):
        d, px = info["date"], info["close"]
        act, pct, reason = "", 0.0, ""
        dec = decisions.get(d)
        if k == 0:  # 首日强制满仓(引擎规则)
            n = acct.buy(px, 1.0)
            act, pct = f"BUY {n}", 1.0
            reason = "首日强制满仓(引擎规则)"
        elif dec:
            kind = dec[0]
            if kind == "buy":
                n = acct.buy(px, dec[1])
                act, pct = f"BUY {n}", dec[1]
            else:
                n = acct.sell(px, dec[1])
                act, pct = f"SELL {n}", dec[1]
            reason = dec[2]
        else:
            act = "HOLD(空仓观望)" if acct.shares == 0 else "HOLD(持仓)"
        eq = acct.equity(px)
        pos = acct.shares * px / eq * 100 if eq > 0 else 0
        ledger.append((d, px, act, reason, acct.cash, acct.shares,
                       acct.cost if acct.shares else 0.0, pos, eq))

    # 输出
    print(f"\n== WorkBuddy 决策通道 · {ticker}  {start}~{end} ==  数据源: {path}")
    print(f"{'日期':<11}{'收盘':>7}{'涨跌%':>7}  {'动作':<12}{'理由':<40}{'现金':>11}{'股数':>8}{'仓位%':>7}{'总资产':>12}")
    for d, px, act, reason, c, sh, cb, pos, eq in ledger:
        chg = next(x["chg"] for x in ind if x["date"] == d)
        print(f"{d:<11}{px:>7.2f}{chg:>7.2f}  {act:<12}{reason:<40}{c:>11.0f}{int(sh):>8}{pos:>7.1f}{eq:>12.0f}")

    final_eq = ledger[-1][-1]
    first_close = window[0][1]
    last_close = window[-1][1]
    print()
    print(f"初始资金: {init_cash:,.0f}")
    print(f"期末资产: {final_eq:,.0f}   区间收益: {(final_eq/init_cash-1)*100:+.2f}%")
    print(f"基准(买入持有): {(last_close/first_close-1)*100:+.2f}%   累计费用: {acct.fees:,.0f}")
    return ledger


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("start")
    ap.add_argument("end")
    ap.add_argument("--init", type=float, default=1_000_000.0)
    args = ap.parse_args()

    # =========================================================================
    # 【我(WorkBuddy)现场做的决策】—— 每次运行前，先看指标，再把决策填进这里。
    # 键=日期；值=(方向, 比例, 理由)。buy 的比例=动用现金比例；sell 的比例=卖持仓比例。
    # 未列出的交易日默认 HOLD(沿用前一决策 = coast)。
    # =========================================================================
    DECISIONS = {
        "2026-07-02": ("sell", 1.0, "单日-10%破位 → 硬止损清仓"),
        "2026-07-20": ("buy", 1 / 3, "+10%放量反转K线(企稳触发) → 试探1/3"),
        "2026-07-21": ("buy", 2 / 3, "站上20日线(趋势反转确认) → 加至2/3"),
        "2026-07-22": ("buy", 1.0, "连续第二日站稳20日线 → 加满"),
        "2026-07-23": ("sell", 1 / 3, "8日+52%过热/RSI66 → 止盈兑现1/3"),
    }

    run(args.ticker, args.start, args.end, args.init, DECISIONS)
