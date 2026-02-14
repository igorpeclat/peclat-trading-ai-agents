import pandas as pd
import talib
from backtesting import Backtest, Strategy
from pathlib import Path


class TestStrategy(Strategy):
    def init(self):
        self.sma = self.I(talib.SMA, self.data.Close, timeperiod=20)

    def next(self):
        if self.data.Close[-1] > self.sma[-1]:
            if not self.position:
                self.buy()
        elif self.position:
            self.position.close()


# Load data
print("🌙 Moon Dev's Test Strategy Loading...")
data_path = Path(__file__).resolve().parents[2] / "data" / "rbi" / "BTC-USD-15m.csv"
data = pd.read_csv(data_path)
data.columns = data.columns.str.strip().str.lower()
drop_cols = [c for c in data.columns if c == "" or c.startswith("unnamed")]
if drop_cols:
    data = data.drop(columns=drop_cols)
data["datetime"] = pd.to_datetime(data["datetime"])
data.set_index("datetime", inplace=True)
data.columns = ["Open", "High", "Low", "Close", "Volume"]

print("🚀 Running backtest...")
bt = Backtest(data, TestStrategy, cash=1000000)
stats = bt.run()
print("✨ Backtest complete!")
print(stats)
