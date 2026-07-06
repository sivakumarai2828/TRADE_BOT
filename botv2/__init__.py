"""TRADE_BOT_V2 — AI-first swing trading bot.

Architecture inversion vs V1:
  V1: rule engine decides, Claude vetoes.
  V2: Claude IS the portfolio manager. Code provides data, execution,
      and hard risk caps the AI can never override.
"""

__version__ = "2.0.0"
