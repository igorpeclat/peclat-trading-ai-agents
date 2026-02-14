from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from dotenv import load_dotenv

from src.agents.api import MoonDevAPI


load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class BaseDataAPI:
    def get_liquidation_data(self, limit: int = 10000) -> Optional[pd.DataFrame]:
        return None

    def get_funding_data(self) -> Optional[pd.DataFrame]:
        return None

    def get_oi_data(self) -> Optional[pd.DataFrame]:
        return None

    def get_recent_transactions(self) -> Optional[pd.DataFrame]:
        return None


class LocalDataAPI(BaseDataAPI):
    def __init__(self) -> None:
        self.funding_path = self._env_or_default(
            "LOCAL_FUNDING_CSV",
            [
                PROJECT_ROOT / "src" / "agents" / "api_data" / "funding.csv",
            ],
        )
        self.oi_path = self._env_or_default(
            "LOCAL_OI_CSV",
            [
                PROJECT_ROOT / "src" / "agents" / "api_data" / "oi.csv",
            ],
        )
        self.liq_path = self._env_or_default(
            "LOCAL_LIQUIDATION_CSV",
            [
                PROJECT_ROOT / "src" / "agents" / "api_data" / "liq_data.csv",
            ],
        )
        self.tx_path = self._env_or_default(
            "LOCAL_TX_CSV",
            [
                PROJECT_ROOT / "src" / "agents" / "api_data" / "recent_txs.csv",
                PROJECT_ROOT / "src" / "data" / "tx_agent" / "recent_transactions.csv",
            ],
        )

    @staticmethod
    def _env_or_default(key: str, defaults: list[Path]) -> Optional[Path]:
        env_value = os.getenv(key)
        if env_value:
            p = Path(env_value).expanduser().resolve()
            if p.exists():
                return p
        for p in defaults:
            if p.exists():
                return p
        return None

    @staticmethod
    def _read_csv(path: Optional[Path]) -> Optional[pd.DataFrame]:
        if not path or not path.exists():
            return None
        try:
            return pd.read_csv(path)
        except Exception:
            return None

    def get_funding_data(self) -> Optional[pd.DataFrame]:
        df = self._read_csv(self.funding_path)
        if df is None or df.empty:
            return None

        rename_map = {
            "yearly_funding_rate": "yearly_funding_rate",
            "annual_rate": "yearly_funding_rate",
            "fundingTime": "event_time",
            "funding_time": "event_time",
            "time": "event_time",
            "nextFundingTime": "event_time",
        }
        df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

        if "funding_rate" not in df.columns:
            if "fundingRate" in df.columns:
                df["funding_rate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
            elif "lastFundingRate" in df.columns:
                df["funding_rate"] = (
                    pd.to_numeric(df["lastFundingRate"], errors="coerce") * 100.0
                )

        if "yearly_funding_rate" not in df.columns and "funding_rate" in df.columns:
            rates = pd.to_numeric(df["funding_rate"], errors="coerce")
            df["yearly_funding_rate"] = rates * 3 * 365

        if "event_time" not in df.columns:
            df["event_time"] = pd.Timestamp.utcnow().isoformat()
        else:
            parsed = pd.to_datetime(df["event_time"], errors="coerce", utc=True)
            df["event_time"] = parsed.fillna(pd.Timestamp.utcnow()).astype(str)

        if "symbol" not in df.columns:
            return None

        required = ["symbol", "funding_rate", "yearly_funding_rate", "event_time"]
        for col in required:
            if col not in df.columns:
                df[col] = None
        return df[required]

    def get_oi_data(self) -> Optional[pd.DataFrame]:
        df = self._read_csv(self.oi_path)
        if df is None or df.empty:
            return None

        rename_map = {
            "open_interest": "openInterest",
            "openinterest": "openInterest",
            "timestamp": "time",
            "event_time": "time",
            "markPrice": "price",
        }
        df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

        required = ["symbol", "openInterest", "price", "time"]
        for col in required:
            if col not in df.columns:
                return None
        return df[required]

    def get_liquidation_data(self, limit: int = 10000) -> Optional[pd.DataFrame]:
        df = self._read_csv(self.liq_path)
        if df is None or df.empty:
            return None

        required = [
            "symbol",
            "side",
            "type",
            "time_in_force",
            "quantity",
            "price",
            "price2",
            "status",
            "filled_qty",
            "total_qty",
            "timestamp",
            "usd_value",
        ]
        if all(col in df.columns for col in required):
            return df[required].tail(limit)

        if df.shape[1] >= 12:
            out = df.iloc[:, :12].copy()
            out.columns = required
            return out.tail(limit)
        return None

    def get_recent_transactions(self) -> Optional[pd.DataFrame]:
        df = self._read_csv(self.tx_path)
        if df is None or df.empty:
            return None
        if "blockTime" not in df.columns:
            if "block_time" in df.columns:
                df["blockTime"] = df["block_time"]
            elif "timestamp" in df.columns:
                ts = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
                df["blockTime"] = (ts.astype("int64") // 10**9).astype("Int64")
        if "birdeye_link" not in df.columns:
            if "token_address" in df.columns:
                df["birdeye_link"] = (
                    "https://birdeye.so/token/"
                    + df["token_address"].astype(str)
                    + "?chain=solana"
                )
        if "blockTime" not in df.columns or "birdeye_link" not in df.columns:
            return None
        return df[["blockTime", "birdeye_link"]]


class PublicMarketAPI(BaseDataAPI):
    BINANCE_BASE = "https://fapi.binance.com"

    def __init__(self) -> None:
        self.session = requests.Session()
        self.funding_symbols = [
            s.strip().upper()
            for s in os.getenv(
                "PUBLIC_FUNDING_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT"
            ).split(",")
            if s.strip()
        ]
        self.oi_symbols = [
            s.strip().upper()
            for s in os.getenv("PUBLIC_OI_SYMBOLS", "BTCUSDT,ETHUSDT").split(",")
            if s.strip()
        ]
        self.local_fallback = LocalDataAPI()

    def get_funding_data(self) -> Optional[pd.DataFrame]:
        try:
            resp = self.session.get(
                f"{self.BINANCE_BASE}/fapi/v1/premiumIndex", timeout=20
            )
            resp.raise_for_status()
            payload = resp.json()
            rows = payload if isinstance(payload, list) else [payload]
            out = []
            for row in rows:
                symbol = str(row.get("symbol", "")).upper()
                if symbol not in self.funding_symbols:
                    continue
                raw_rate = pd.to_numeric(row.get("lastFundingRate"), errors="coerce")
                if pd.isna(raw_rate):
                    continue
                funding_pct = float(raw_rate) * 100.0
                yearly_pct = funding_pct * 3 * 365
                event_time = row.get("nextFundingTime") or row.get("time")
                out.append(
                    {
                        "symbol": symbol.replace("USDT", ""),
                        "funding_rate": funding_pct,
                        "yearly_funding_rate": yearly_pct,
                        "event_time": pd.to_datetime(
                            event_time, unit="ms", errors="coerce", utc=True
                        )
                        .fillna(pd.Timestamp.utcnow())
                        .isoformat(),
                    }
                )
            if out:
                return pd.DataFrame(out)
            return self.local_fallback.get_funding_data()
        except Exception:
            return self.local_fallback.get_funding_data()

    def get_oi_data(self) -> Optional[pd.DataFrame]:
        rows = []
        for symbol in self.oi_symbols:
            try:
                oi_resp = self.session.get(
                    f"{self.BINANCE_BASE}/fapi/v1/openInterest",
                    params={"symbol": symbol},
                    timeout=20,
                )
                oi_resp.raise_for_status()
                oi = oi_resp.json()

                price_resp = self.session.get(
                    f"{self.BINANCE_BASE}/fapi/v1/ticker/price",
                    params={"symbol": symbol},
                    timeout=20,
                )
                price_resp.raise_for_status()
                price = price_resp.json()

                rows.append(
                    {
                        "symbol": symbol,
                        "openInterest": float(oi.get("openInterest", 0.0)),
                        "price": float(price.get("price", 0.0)),
                        "time": int(oi.get("time", 0)),
                    }
                )
            except Exception:
                continue
        if rows:
            return pd.DataFrame(rows)
        return self.local_fallback.get_oi_data()

    def get_liquidation_data(self, limit: int = 10000) -> Optional[pd.DataFrame]:
        out = []
        for symbol in self.oi_symbols:
            try:
                resp = self.session.get(
                    f"{self.BINANCE_BASE}/fapi/v1/allForceOrders",
                    params={"symbol": symbol, "limit": min(limit, 100)},
                    timeout=20,
                )
                resp.raise_for_status()
                orders = resp.json()
                for o in orders:
                    price = float(o.get("price") or 0.0)
                    avg_price = float(o.get("avgPrice") or price)
                    filled_qty = float(o.get("executedQty") or 0.0)
                    out.append(
                        {
                            "symbol": o.get("symbol", symbol),
                            "side": o.get("side", ""),
                            "type": o.get("origType", o.get("type", "")),
                            "time_in_force": o.get("timeInForce", ""),
                            "quantity": float(o.get("origQty") or 0.0),
                            "price": price,
                            "price2": avg_price,
                            "status": o.get("status", ""),
                            "filled_qty": filled_qty,
                            "total_qty": float(
                                o.get("cumQty") or o.get("origQty") or 0.0
                            ),
                            "timestamp": int(o.get("time") or o.get("updateTime") or 0),
                            "usd_value": filled_qty * avg_price,
                        }
                    )
            except Exception:
                continue
        if out:
            df = pd.DataFrame(out)
            return df.tail(limit)
        return self.local_fallback.get_liquidation_data(limit=limit)

    def get_recent_transactions(self) -> Optional[pd.DataFrame]:
        return self.local_fallback.get_recent_transactions()


class MoonDevDataAPI(BaseDataAPI):
    def __init__(self) -> None:
        self.api = MoonDevAPI()

    def get_liquidation_data(self, limit: int = 10000) -> Optional[pd.DataFrame]:
        return self.api.get_liquidation_data(limit=limit)

    def get_funding_data(self) -> Optional[pd.DataFrame]:
        return self.api.get_funding_data()

    def get_oi_data(self) -> Optional[pd.DataFrame]:
        return self.api.get_oi_data()

    def get_recent_transactions(self) -> Optional[pd.DataFrame]:
        return self.api.get_copybot_recent_transactions()


def get_data_provider(agent_name: str = "") -> BaseDataAPI:
    mode = os.getenv("DATA_PROVIDER", "public").strip().lower()

    if mode == "moondev":
        if os.getenv("MOONDEV_API_KEY"):
            return MoonDevDataAPI()
        print(
            "⚠️ DATA_PROVIDER=moondev but MOONDEV_API_KEY missing. Falling back to local provider."
        )
        return LocalDataAPI()

    if mode == "local":
        return LocalDataAPI()

    if mode == "public":
        return PublicMarketAPI()

    if mode == "auto":
        if os.getenv("MOONDEV_API_KEY"):
            return MoonDevDataAPI()
        return PublicMarketAPI()

    print(f"⚠️ Unknown DATA_PROVIDER='{mode}' for {agent_name}. Using public provider.")
    return PublicMarketAPI()
