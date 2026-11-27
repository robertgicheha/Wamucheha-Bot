"""
Stock and ETF research for the Nairobi Securities Exchange (NSE) and the US market.

Read-only: nothing in this package places orders or touches the trading engine.
  nse.py        NSE prices from the same RapidAPI feed Sokowatch-Web uses
  screener.py   Sokowatch-Web's rule-based screener and market insights, in Python
  us.py         US stocks and ETFs (prices, history, dividends) from Yahoo Finance
  history.py    daily NSE closes recorded here (the NSE feed has no history endpoint)
  analytics.py  indicators, historical rise frequency and the ranked pick lists
  service.py    puts the above together for the admin console and the CLI
"""
