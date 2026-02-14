import pandas as pd
import talib
import numpy as np
from backtesting import Backtest, Strategy

# =====================
# DATA PREPARATION 🌍
# =====================
data = pd.read_csv(
    '/Users/md/Dropbox/dev/github/moon-dev-ai-agents-for-trading/src/data/rbi/BTC-USD-15m.csv',
    parse_dates=['datetime'],
    index_col='datetime'
)

# Clean and format columns 🌟
data.columns = data.columns.str.strip().str.lower()
data = data.drop(columns=[col for col in data.columns if 'unnamed' in col.lower()])
data = data.rename(columns={
    'open': 'Open',
    'high': 'High',
    'low': 'Low',
    'close': 'Close',
    'volume': 'Volume'
})

# =====================
# STRATEGY IMPLEMENTATION 🌙
# =====================
class VolatilityAsymmetry(Strategy):
    lookback_period = 24192  # 1-year lookback in 15m intervals (365*24*4)
    position_size_pct = 0.02  # 2% risk per trade
    max_hold_bars = 960  # 10 days in 15m bars
    stop_loss_pct = 0.10
    max_drawdown_pct = 0.15

    def init(self):
        # 🌙 CORE INDICATORS
        self.vix = self.data.Close
        self.vvix = self.I(talib.STDDEV, self.vix, timeperiod=20, nbdev=1)
        
        # ✨ STRATEGY INDICATORS
        self.vix_95 = self.I(self._rolling_quantile, self.vix, 0.95, name='VIX 95%')
        self.vvix_med = self.I(self._rolling_median, self.vvix, name='VVIX Median')
        self.vix_sma20 = self.I(talib.SMA, self.vix, timeperiod=20)
        self.vvix_75 = self.I(self._rolling_quantile, self.vvix, 0.75, name='VVIX 75%')

        self.entry_bar = 0
        self.entry_price = 0
        self.initial_equity = self._broker._equity  # Fixed equity reference

    def _rolling_quantile(self, series, q):
        return series.rolling(self.lookback_period).quantile(q)

    def _rolling_median(self, series):
        return series.rolling(self.lookback_period).median()

    def next(self):
        # 🌑 PORTFOLIO SAFETY CHECK
        current_equity = self._broker._equity
        if current_equity < self.initial_equity * (1 - self.max_drawdown_pct):
            self.position.close()
            print("🌑💥 MOON DEV CRITICAL: 15% Drawdown Limit Breached! Stopping!")
            return

        if not self.position:
            # 🌙 ENTRY CONDITIONS
            if (self.vix[-1] > self.vix_95[-1] and 
                self.vvix[-1] < self.vvix_med[-1]):
                
                risk_amount = self.position_size_pct * current_equity
                price = self.data.Open[-1]
                size = int(round(risk_amount / (price * self.stop_loss_pct)))
                
                if size > 0:
                    self.buy(size=size)
                    self.entry_bar = len(self.data)
                    self.entry_price = price
                    print(f"🌙🚀 MOON DEV ENTRY: Long {size} units @ {price:.2f}")
        else:
            # ✨ EXIT CONDITIONS
            exit_signal = False
            if self.vix[-1] <= self.vix_sma20[-1]:
                print(f"🌙🎯 SMA20 Reversion @ {self.data.index[-1]}")
                exit_signal = True
            elif self.vvix[-1] > self.vvix_75[-1]:
                print(f"🌙⚡ VVIX Instability Spike @ {self.data.index[-1]}")
                exit_signal = True
            elif len(self.data) - self.entry_bar >= self.max_hold_bars:
                print(f"🌙⏳
