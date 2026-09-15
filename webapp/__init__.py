"""Web backtest simulator built on top of tradingagents.

See plan: webapp package is self-contained; it imports tradingagents as a
library and never edits it. The no-lookahead guarantee lives in
webapp/engine/clock.py + webapp/engine/data_gateway.py.
"""
