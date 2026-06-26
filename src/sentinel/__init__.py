"""Sentinel Edge — a Sentiment Edge-style market-neutral long/short strategy on KoinBay futures.

Pipeline: market data -> continuous score -> cross-sectional rank -> dollar-neutral long/short
target book (in contracts) -> reconcile vs live positions -> execute (paper or live).
"""

__version__ = "0.1.0"
