"""Portfolio state and deterministic execution model.

The LLM decides *what* to do; this module decides *what is possible*.
Every fill passes validate_and_fill so an impossible request becomes a
recorded rejection instead of a crash or a fabricated trade.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from webapp.core.errors import InvalidDecision
from webapp.core.models import Decision, Fill


@dataclass
class Portfolio:
    cash: float
    shares: float = 0.0
    avg_cost: float = 0.0

    def equity(self, price: float) -> float:
        return self.cash + self.shares * price

    def snapshot(self, price: float) -> dict:
        open_pnl = (price - self.avg_cost) * self.shares if self.shares else 0.0
        return {
            "cash": round(self.cash, 2),
            "shares": round(self.shares, 4),
            "avg_cost": round(self.avg_cost, 4) if self.shares else 0.0,
            "equity": round(self.equity(price), 2),
            "open_pnl": round(open_pnl, 2),
        }

    def apply_fill(self, side: str, price: float, shares: float, fee: float) -> None:
        notional = price * shares
        if side == "buy":
            total_cost = notional + fee
            new_shares = self.shares + shares
            self.avg_cost = (
                (self.avg_cost * self.shares + total_cost) / new_shares
                if new_shares > 0
                else 0.0
            )
            self.cash -= total_cost
            self.shares = new_shares
        elif side == "sell":
            self.cash += notional - fee
            self.shares -= shares
            if self.shares <= 1e-9:
                self.shares = 0.0
                self.avg_cost = 0.0


@dataclass
class ExecutionModel:
    commission_rate: float = 0.0005
    min_commission: float = 0.0
    slippage_bps: float = 0.0
    min_lot_shares: float = 0.0  # deprecated compatibility alias
    min_order_shares: float = 0.0
    share_step: float = 0.0001
    sell_tax_rate: float = 0.0

    def __post_init__(self) -> None:
        if self.min_lot_shares > 0 and self.min_order_shares <= 0:
            self.min_order_shares = self.min_lot_shares
            self.share_step = self.min_lot_shares

    def quote(self, side: str, close_price: float) -> float:
        """Execution price after adverse slippage."""
        adj = close_price * (1 + self.slippage_bps / 10_000)
        return adj if side == "buy" else close_price * (1 - self.slippage_bps / 10_000)

    def fee(self, notional: float, side: str = "buy") -> float:
        commission = max(
            notional * self.commission_rate,
            self.min_commission if notional > 0 else 0.0,
        )
        tax = notional * self.sell_tax_rate if side == "sell" else 0.0
        return commission + tax

    def _round_shares(self, shares: float) -> float:
        step = self.share_step if self.share_step > 0 else 0.0001
        return math.floor(shares / step + 1e-12) * step

    def validate_and_fill(self, decision: Decision, portfolio: Portfolio, close_price: float) -> Fill:
        if decision.action == "hold":
            return Fill(action="hold", reason="agent chose to hold")

        if decision.action == "buy":
            if decision.position_pct <= 0:
                raise InvalidDecision("buy with position_pct=0")
            budget = portfolio.cash * min(decision.position_pct, 1.0)
            price = self.quote("buy", close_price)
            # leave room for the fee inside the budget
            raw_shares = budget / (price * (1 + self.commission_rate) + 1e-12)
            shares = self._round_shares(raw_shares)
            if shares <= 0 or shares < self.min_order_shares:
                raise InvalidDecision(
                    f"insufficient cash: budget {budget:.2f} cannot meet minimum order "
                    f"{self.min_order_shares:g} shares at {price:.2f}"
                )
            fee = self.fee(price * shares, "buy")
            while fee > budget - price * shares and shares > 0:  # fee overflow guard
                shares = self._round_shares(shares - self.share_step)
                if shares <= 0 or shares < self.min_order_shares:
                    raise InvalidDecision("insufficient cash after fees")
                fee = self.fee(price * shares, "buy")
            return Fill(action="buy", requested_pct=decision.position_pct,
                        shares=shares, price=round(price, 4), fee=round(fee, 4))

        # sell
        if portfolio.shares <= 0:
            raise InvalidDecision("cannot sell: no open position")
        if decision.position_pct <= 0:
            raise InvalidDecision("sell with position_pct=0")
        fraction = min(decision.position_pct, 1.0)
        shares = portfolio.shares * fraction
        shares = self._round_shares(shares) if fraction < 0.999 else portfolio.shares
        if 0 < shares < self.min_order_shares and fraction < 0.999:
            if portfolio.shares < self.min_order_shares:
                shares = portfolio.shares
            else:
                raise InvalidDecision(
                    f"sell order {shares:g} is below minimum {self.min_order_shares:g} shares"
                )
        if shares <= 0:
            raise InvalidDecision(f"sell fraction {fraction} rounds to zero shares")
        price = self.quote("sell", close_price)
        fee = self.fee(price * shares, "sell")
        return Fill(action="sell", requested_pct=decision.position_pct,
                    shares=round(shares, 4), price=round(price, 4), fee=round(fee, 4))
