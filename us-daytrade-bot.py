#!/usr/bin/env -S uv run --python 3.13 python3
# -*- coding: utf-8 -*-
"""
美股當沖模擬交易 Bot — Alpaca Paper Trading
版本: 2.0.0

架構：
  - Alpaca Paper Trading API 整合
  - SMC Engine v3.0 移植（OB / FVG / BOS / CHoCH / Sweep）
  - VWAPCalculator：日內累積 VWAP + ±1σ/±2σ 偏差帶 + Bounce 偵測
  - ORBTracker：開盤前 15 分鐘（09:30-09:45 ET）高低點 + 突破確認
  - 多層確認評分系統（SMC + VWAP + ORB），總分 ≥ 70 入場
  - Kelly × 0.3 保守部位管理
  - ATR × 2.0 停損、ATR × 3.0 停利
  - 日損上限 1.5%
  - 交易日誌 JSON

交易時段（台灣時間 UTC+8）：
  - Kill Zone 1: 21:30–23:00（美股開盤）
  - Kill Zone 2: 00:00–01:30（午盤）
  - Kill Zone 3: 02:30–04:00（收盤前）

⚠️ 僅使用 Paper Trading endpoint，禁止真實下單
⚠️ API Key 一律從 credentials JSON 載入，禁止 hardcode
"""

__version__ = "2.0.0"
__author__ = "us-daytrade-bot"

import sys
import os
import json
import logging
import time
import traceback
import subprocess
from datetime import datetime, time as dt_time, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple
from enum import Enum

# ============================================================================
# 依賴自動安裝
# ============================================================================

def check_and_install_dependencies() -> None:
    """檢查並自動安裝所需套件"""
    required = {
        'pandas': 'pandas',
        'numpy': 'numpy',
        'requests': 'requests',
        'ta': 'ta',
    }
    missing = []
    for module, pkg in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(pkg)

    if missing:
        print(f"[SETUP] 安裝缺失套件: {missing}")
        # 優先使用 uv（環境中預設安裝），fallback 到 pip
        try:
            import shutil
            uv_path = shutil.which('uv')
            if uv_path:
                subprocess.check_call([uv_path, 'pip', 'install', '-q'] + missing)
            else:
                subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q'] + missing)
        except subprocess.CalledProcessError:
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--user', '-q'] + missing)
        print("[SETUP] 安裝完成")


check_and_install_dependencies()

import pandas as pd
import numpy as np
import requests
import ta

# ============================================================================
# 路徑配置
# ============================================================================

BASE_DIR = Path('/home/node/clawd')
CRED_FILE = BASE_DIR / 'credentials' / '8df795c8dca1.json'
LOG_DIR = BASE_DIR / 'logs'
DATA_DIR = BASE_DIR / 'data' / 'us-daytrade'

LOG_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 交易日誌 JSON（每日一檔）
def trade_log_path() -> Path:
    """取得當日交易日誌路徑（台灣時間）"""
    tw_date = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
    return DATA_DIR / f'trades-{tw_date}.json'

# ============================================================================
# 日誌系統
# ============================================================================

def setup_logging() -> logging.Logger:
    """初始化日誌（檔案 + Console）"""
    logger = logging.getLogger('us_daytrade_bot')
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 檔案 handler — DEBUG 以上
    log_file = LOG_DIR / 'us-daytrade-bot.log'
    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler — INFO 以上
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


logger = setup_logging()

# ============================================================================
# Credentials 載入
# ============================================================================

def load_credentials() -> Dict:
    """從 credentials JSON 載入 API Key（禁止 hardcode）"""
    try:
        with open(CRED_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.critical(f"找不到 credentials 檔案: {CRED_FILE}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.critical(f"credentials 解析失敗: {e}")
        sys.exit(1)


_CREDS = load_credentials()
_ALPACA = _CREDS.get('alpaca', {})

ALPACA_API_KEY: str = _ALPACA.get('api_key', '')
ALPACA_SECRET_KEY: str = _ALPACA.get('secret_key', '')
ALPACA_BASE_URL: str = _ALPACA.get('base_url', 'https://paper-api.alpaca.markets')
ALPACA_DATA_URL: str = 'https://data.alpaca.markets'

if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    logger.critical("Alpaca API Key / Secret 未設定，請確認 credentials 檔案")
    sys.exit(1)

logger.info(f"Alpaca Paper Trading 已載入 — 帳號: {_ALPACA.get('account', 'N/A')}")

# ============================================================================
# 交易配置
# ============================================================================

CONFIG = {
    # --- 風控 ---
    'MAX_DAILY_LOSS_PCT': 0.015,      # 日損上限 1.5%
    'KELLY_FRACTION': 0.3,            # Kelly × 0.3（保守）
    'ATR_STOP_MULT': 2.0,             # 停損 ATR × 2.0
    'ATR_TP_MULT': 3.0,               # 停利 ATR × 3.0
    'MAX_POSITIONS': 3,               # 最多同時持倉數量
    'MAX_SINGLE_RISK_PCT': 0.005,     # 單筆最大風險 0.5%

    # --- 篩選條件 ---
    'MIN_VOLUME': 1_000_000,          # Volume > 1M shares
    'MIN_PRICE': 5.0,                 # 最低股價 $5（避免 penny stocks）
    'MAX_PRICE': 2000.0,              # 最高股價（避免高價小 vol）
    'MIN_SCORE': 70,                  # 多層確認評分門檻（SMC + VWAP + ORB）

    # --- 掃描 ---
    'SCAN_INTERVAL_SEC': 60,          # 無持倉：每 60 秒掃描
    'MONITOR_INTERVAL_SEC': 20,       # 有持倉：每 20 秒監控
    'WATCHLIST_SIZE': 20,             # 候選股數量

    # --- Kill Zone（台灣時間，UTC+8）---
    # Kill Zone 1: 21:30 – 23:00 (美股開盤)
    'KZ1_START': dt_time(21, 30),
    'KZ1_END':   dt_time(23, 0),
    # Kill Zone 2: 00:00 – 01:30 (午盤)
    'KZ2_START': dt_time(0, 0),
    'KZ2_END':   dt_time(1, 30),
    # Kill Zone 3: 02:30 – 04:00 (收盤前)
    'KZ3_START': dt_time(2, 30),
    'KZ3_END':   dt_time(4, 0),
    # 強制平倉：04:00 之後不開新倉，04:15 強制清倉
    'NO_NEW_TRADE_AFTER': dt_time(4, 0),
    'FORCE_CLOSE': dt_time(4, 15),
}

# ============================================================================
# Alpaca REST 客戶端
# ============================================================================

class AlpacaClient:
    """
    Alpaca Paper Trading REST API 封裝
    
    支援：帳戶查詢、下單、持倉、歷史 K 線、即時報價
    僅連接 paper-api.alpaca.markets
    """

    def __init__(self) -> None:
        self._headers = {
            'APCA-API-KEY-ID': ALPACA_API_KEY,
            'APCA-API-SECRET-KEY': ALPACA_SECRET_KEY,
            'Content-Type': 'application/json',
        }
        self._session = requests.Session()
        self._session.headers.update(self._headers)

    # ---- 帳戶 ----

    def get_account(self) -> Dict:
        """
        GET /v2/account
        取得帳戶資訊：資金、淨值、購買力等
        """
        return self._get('/v2/account')

    def get_account_balance(self) -> float:
        """取得帳戶現金餘額（equity）"""
        acc = self.get_account()
        return float(acc.get('equity', 100000))

    # ---- 訂單 ----

    def place_order(
        self,
        symbol: str,
        qty: float,
        side: str,          # 'buy' or 'sell'
        order_type: str = 'market',
        time_in_force: str = 'day',
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> Dict:
        """
        POST /v2/orders
        下單（預設：市價、當日有效）

        Args:
            symbol: 股票代碼（如 'AAPL'）
            qty: 股數（支援小數：fractional shares）
            side: 'buy' 或 'sell'
            order_type: 'market' / 'limit' / 'stop' / 'stop_limit'
            time_in_force: 'day' / 'gtc' / 'ioc' / 'fok'
            limit_price: 限價（order_type='limit' 時必填）
            stop_price: 觸發價（order_type='stop' 時必填）
            client_order_id: 自訂訂單 ID（方便追蹤）
        """
        payload: Dict = {
            'symbol': symbol,
            'qty': str(qty),
            'side': side,
            'type': order_type,
            'time_in_force': time_in_force,
        }
        if limit_price is not None:
            payload['limit_price'] = str(limit_price)
        if stop_price is not None:
            payload['stop_price'] = str(stop_price)
        if client_order_id:
            payload['client_order_id'] = client_order_id

        return self._post('/v2/orders', payload)

    def cancel_order(self, order_id: str) -> Dict:
        """DELETE /v2/orders/{order_id}"""
        return self._delete(f'/v2/orders/{order_id}')

    def get_orders(self, status: str = 'open') -> List[Dict]:
        """GET /v2/orders — 查詢訂單列表"""
        return self._get('/v2/orders', params={'status': status, 'limit': 50})

    # ---- 持倉 ----

    def get_positions(self) -> List[Dict]:
        """GET /v2/positions — 查詢所有持倉"""
        return self._get('/v2/positions')

    def get_position(self, symbol: str) -> Optional[Dict]:
        """GET /v2/positions/{symbol} — 查詢單一持倉"""
        try:
            return self._get(f'/v2/positions/{symbol}')
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                return None
            raise

    def close_position(self, symbol: str) -> Dict:
        """DELETE /v2/positions/{symbol} — 平倉"""
        return self._delete(f'/v2/positions/{symbol}')

    # ---- 市場資料 ----

    def get_bars(
        self,
        symbol: str,
        timeframe: str = '5Min',
        limit: int = 200,
        adjustment: str = 'raw',
    ) -> pd.DataFrame:
        """
        GET /v2/stocks/{symbol}/bars（via data.alpaca.markets）
        
        取得歷史 K 線，返回 DataFrame（columns: Open, High, Low, Close, Volume）

        Args:
            symbol: 股票代碼
            timeframe: '1Min' / '5Min' / '15Min' / '1Hour' / '1Day'
            limit: 最多幾根 K 棒
            adjustment: 'raw' / 'split' / 'dividend' / 'all'
        """
        # 根據 timeframe 決定 start 日期範圍（確保取到足夠歷史資料）
        from datetime import timezone as tz
        now_utc = datetime.now(tz.utc)
        timeframe_days = {
            '1Min': 2, '5Min': 7, '15Min': 14,
            '1Hour': 30, '1Day': 365
        }
        lookback_days = timeframe_days.get(timeframe, 7)
        start = (now_utc - timedelta(days=lookback_days)).strftime('%Y-%m-%dT00:00:00Z')
        end   = now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')

        params = {
            'timeframe': timeframe,
            'limit': limit,
            'adjustment': adjustment,
            'feed': 'iex',   # IEX 免費 feed（paper trading 適用）
            'start': start,
            'end': end,
        }
        resp = self._get_data(f'/v2/stocks/{symbol}/bars', params=params)
        bars = resp.get('bars') or []
        if not bars:
            return pd.DataFrame()

        df = pd.DataFrame(bars)
        df['t'] = pd.to_datetime(df['t'])
        df = df.rename(columns={
            't': 'Datetime',
            'o': 'Open',
            'h': 'High',
            'l': 'Low',
            'c': 'Close',
            'v': 'Volume',
            'vw': 'VWAP',
        })
        df = df.set_index('Datetime').sort_index()
        return df

    def get_latest_quote(self, symbol: str) -> Dict:
        """
        GET /v2/stocks/{symbol}/quotes/latest
        取得最新報價（bid / ask）
        """
        resp = self._get_data(f'/v2/stocks/{symbol}/quotes/latest')
        return resp.get('quote', {})

    def get_latest_trade(self, symbol: str) -> Dict:
        """GET /v2/stocks/{symbol}/trades/latest — 取得最新成交"""
        resp = self._get_data(f'/v2/stocks/{symbol}/trades/latest')
        return resp.get('trade', {})

    def get_latest_price(self, symbol: str) -> float:
        """取得最新成交價"""
        try:
            trade = self.get_latest_trade(symbol)
            return float(trade.get('p', 0))
        except Exception:
            try:
                quote = self.get_latest_quote(symbol)
                ask = float(quote.get('ap', 0))
                bid = float(quote.get('bp', 0))
                return (ask + bid) / 2 if ask and bid else 0
            except Exception:
                return 0

    def get_market_status(self) -> Dict:
        """GET /v2/clock — 市場開收盤狀態"""
        return self._get('/v2/clock')

    def is_market_open(self) -> bool:
        """確認美股市場是否開盤"""
        try:
            clock = self.get_market_status()
            return bool(clock.get('is_open', False))
        except Exception:
            return False

    def get_screener_most_actives(self, top: int = 20) -> List[Dict]:
        """
        GET /v2/screener/stocks/most-actives
        取得最活躍股票清單（成交量排名）
        """
        try:
            resp = self._get('/v2/screener/stocks/most-actives',
                             params={'top': top, 'by': 'volume'},
                             base_url='https://data.alpaca.markets')
            return resp.get('most_actives', [])
        except Exception as e:
            logger.warning(f"get_screener_most_actives 失敗: {e}")
            return []

    def get_market_movers(self, market_type: str = 'stocks', top: int = 20) -> Dict:
        """
        GET /v2/screener/stocks/movers
        取得漲跌幅排名（gainers / losers）
        """
        try:
            resp = self._get('/v2/screener/stocks/movers',
                             params={'top': top},
                             base_url='https://data.alpaca.markets')
            return resp
        except Exception as e:
            logger.warning(f"get_market_movers 失敗: {e}")
            return {}

    # ---- 內部 HTTP 方法 ----

    def _get(self, path: str, params: Dict = None, base_url: str = None) -> any:
        url = (base_url or ALPACA_BASE_URL) + path
        resp = self._session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: Dict = None) -> Dict:
        url = ALPACA_BASE_URL + path
        resp = self._session.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> Dict:
        url = ALPACA_BASE_URL + path
        resp = self._session.delete(url, timeout=15)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}

    def _get_data(self, path: str, params: Dict = None) -> Dict:
        """從 data.alpaca.markets 取得市場資料"""
        url = ALPACA_DATA_URL + path
        resp = self._session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()


# 全域 client 實例
alpaca = AlpacaClient()

# ============================================================================
# 盤前篩選（Watchlist 生成）
# ============================================================================

def premarket_screener(client: AlpacaClient, top_n: int = 20) -> List[str]:
    """
    盤前篩選候選股：
      優先從 Alpaca Screener API 取得（需訂閱），
      若失敗則 fallback 到預設高流動性美股清單。

    篩選條件：
      - Volume > 1M shares
      - 股價 $5 – $2000

    Returns:
        候選股代碼列表
    """
    logger.info("=== 盤前篩選開始 ===")
    candidates: set = set()

    # 1. Alpaca Most Actives（Premium subscription 才有）
    try:
        actives = client.get_screener_most_actives(top=top_n)
        for item in actives:
            sym = item.get('symbol', '').upper()
            vol = item.get('volume', 0)
            if sym and vol >= CONFIG['MIN_VOLUME'] and _is_valid_symbol(sym):
                candidates.add(sym)
        if actives:
            logger.info(f"Alpaca Most Actives: {len(actives)} 支")
    except Exception as e:
        logger.debug(f"Alpaca screener 不可用（需訂閱）: {e}")

    # 2. Gainers / Losers（Premium）
    try:
        movers = client.get_market_movers(top=top_n)
        for direction in ('gainers', 'losers'):
            for item in movers.get(direction, []):
                sym = item.get('symbol', '').upper()
                if sym and _is_valid_symbol(sym):
                    candidates.add(sym)
    except Exception as e:
        logger.debug(f"Alpaca movers 不可用: {e}")

    # 3. Fallback：高流動性美股預設清單
    if not candidates:
        logger.info("使用預設高流動性美股清單")
        default_watchlist = [
            # 科技大型股
            'AAPL', 'MSFT', 'NVDA', 'TSLA', 'META', 'AMZN', 'GOOGL', 'AMD',
            # 金融 & 其他
            'JPM', 'BAC', 'GS',
            # ETF（高流動性）
            'SPY', 'QQQ', 'SOXL', 'TQQQ', 'SQQQ',
            # 高波動性熱門股
            'PLTR', 'MSTR', 'COIN', 'RIVN', 'SOFI',
        ]
        candidates.update(default_watchlist)

    # 4. 驗證股價範圍
    filtered: List[str] = []
    for sym in list(candidates)[:top_n * 2]:
        if not _is_valid_symbol(sym):
            continue
        try:
            price = client.get_latest_price(sym)
            if price > 0 and CONFIG['MIN_PRICE'] <= price <= CONFIG['MAX_PRICE']:
                filtered.append(sym)
                logger.debug(f"  {sym}: ${price:.2f} ✓")
        except Exception:
            filtered.append(sym)  # 無法取得價格則保留

    result = filtered[:top_n]
    logger.info(f"=== 盤前篩選完成：{result} ===")
    return result


def _is_valid_symbol(sym: str) -> bool:
    """基本股票代碼驗證（排除 warrant、preferred 等）"""
    if not sym or len(sym) > 5:
        return False
    # 排除包含非字母字符的代碼（如 BRK.B → BRKB 另處理）
    if not sym.isalpha():
        return False
    return True


# ============================================================================
# SMC Engine v3.0（移植自 daytrade-bot-smc.py，調整美股參數）
# ============================================================================

class Bias(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass
class SwingPoint:
    index: int
    price: float
    type: str           # 'high' or 'low'


@dataclass
class FVG:
    type: str           # 'bullish' or 'bearish'
    top: float
    bottom: float
    ce: float           # Consequent Encroachment（50%）
    index: int
    mitigated: bool = False


@dataclass
class OrderBlock:
    type: str           # 'bullish' or 'bearish'
    high: float
    low: float
    index: int
    mitigated: bool = False


@dataclass
class StructureEvent:
    type: str           # 'bos' / 'choch' / 'sweep'
    direction: str      # 'bullish' / 'bearish'
    index: int
    price: float
    swing_ref: float


@dataclass
class POI:
    """Point of Interest"""
    type: str
    direction: str
    high: float
    low: float
    score: int = 0
    has_displacement: bool = False
    has_imbalance: bool = False
    has_liquidity_sweep: bool = False


def find_swing_points(df: pd.DataFrame, lookback: int = 5) -> List[SwingPoint]:
    """辨識波段高低點（Fractal method）"""
    swings: List[SwingPoint] = []
    highs = df['High'].values
    lows = df['Low'].values

    for i in range(lookback, len(df) - lookback):
        if highs[i] == np.max(highs[i - lookback:i + lookback + 1]):
            swings.append(SwingPoint(index=i, price=float(highs[i]), type='high'))
        if lows[i] == np.min(lows[i - lookback:i + lookback + 1]):
            swings.append(SwingPoint(index=i, price=float(lows[i]), type='low'))

    return sorted(swings, key=lambda s: s.index)


def detect_structure(df: pd.DataFrame, swings: List[SwingPoint]) -> List[StructureEvent]:
    """
    偵測 BOS / CHoCH / Sweep

    規則：
    - BOS: 收盤突破前波段高/低 → 趨勢延續
    - Sweep: 影線刺穿但收盤回到結構內 → 掃止損
    - CHoCH: 逆勢突破保護性結構點 → 趨勢反轉
    """
    events: List[StructureEvent] = []
    closes = df['Close'].values
    highs = df['High'].values
    lows = df['Low'].values
    trend = Bias.NEUTRAL

    swing_highs = [s for s in swings if s.type == 'high']
    swing_lows  = [s for s in swings if s.type == 'low']

    for i in range(1, len(df)):
        for sh in swing_highs:
            if sh.index >= i:
                continue
            if highs[i] > sh.price and closes[i] < sh.price:
                events.append(StructureEvent('sweep', 'bearish', i, float(highs[i]), sh.price))
            elif closes[i] > sh.price:
                etype = 'choch' if trend == Bias.BEARISH else 'bos'
                events.append(StructureEvent(etype, 'bullish', i, float(closes[i]), sh.price))
                trend = Bias.BULLISH

        for sl in swing_lows:
            if sl.index >= i:
                continue
            if lows[i] < sl.price and closes[i] > sl.price:
                events.append(StructureEvent('sweep', 'bullish', i, float(lows[i]), sl.price))
            elif closes[i] < sl.price:
                etype = 'choch' if trend == Bias.BULLISH else 'bos'
                events.append(StructureEvent(etype, 'bearish', i, float(closes[i]), sl.price))
                trend = Bias.BEARISH

    return events


def detect_fvg(df: pd.DataFrame) -> List[FVG]:
    """偵測 Fair Value Gap（三根 K 棒影線不重疊）"""
    fvgs: List[FVG] = []
    highs = df['High'].values
    lows  = df['Low'].values

    for i in range(2, len(df)):
        if lows[i] > highs[i - 2]:
            fvgs.append(FVG('bullish', top=float(lows[i]), bottom=float(highs[i - 2]),
                            ce=(float(lows[i]) + float(highs[i - 2])) / 2, index=i))
        if highs[i] < lows[i - 2]:
            fvgs.append(FVG('bearish', top=float(lows[i - 2]), bottom=float(highs[i]),
                            ce=(float(lows[i - 2]) + float(highs[i])) / 2, index=i))
    return fvgs


def check_fvg_mitigation(fvgs: List[FVG], df: pd.DataFrame) -> List[FVG]:
    """標記已被回測的 FVG"""
    closes = df['Close'].values
    for fvg in fvgs:
        for i in range(fvg.index + 1, len(df)):
            if fvg.type == 'bullish' and closes[i] < fvg.bottom:
                fvg.mitigated = True; break
            elif fvg.type == 'bearish' and closes[i] > fvg.top:
                fvg.mitigated = True; break
    return fvgs


def detect_order_blocks(df: pd.DataFrame, events: List[StructureEvent]) -> List[OrderBlock]:
    """偵測 Order Block（BOS/CHoCH 前最後一根反向 K 棒）"""
    obs: List[OrderBlock] = []
    opens  = df['Open'].values
    closes = df['Close'].values
    highs  = df['High'].values
    lows   = df['Low'].values

    for ev in events:
        if ev.type not in ('bos', 'choch'):
            continue
        for j in range(ev.index - 1, max(0, ev.index - 10), -1):
            if ev.direction == 'bullish' and closes[j] < opens[j]:
                obs.append(OrderBlock('bullish', float(highs[j]), float(lows[j]), j))
                break
            elif ev.direction == 'bearish' and closes[j] > opens[j]:
                obs.append(OrderBlock('bearish', float(highs[j]), float(lows[j]), j))
                break
    return obs


def detect_displacement(df: pd.DataFrame, index: int, lookback: int = 10) -> bool:
    """位移偵測：當前 K 棒實體 > 前 N 根平均實體的 1.5 倍"""
    if index < lookback:
        return False
    body = abs(df['Close'].iloc[index] - df['Open'].iloc[index])
    past_bodies = abs(df['Close'].iloc[max(0, index - lookback):index] -
                      df['Open'].iloc[max(0, index - lookback):index])
    avg_body = past_bodies.mean()
    return bool(body > avg_body * 1.5) if avg_body > 0 else False


def get_kill_zone_us() -> Optional[str]:
    """
    判斷當前是否在美股 Kill Zone（台灣時間）

    Returns:
        'kz1_open' / 'kz2_mid' / 'kz3_close' / None
    """
    now_tw = datetime.now(timezone(timedelta(hours=8))).time()

    if CONFIG['KZ1_START'] <= now_tw <= CONFIG['KZ1_END']:
        return 'kz1_open'
    elif CONFIG['KZ2_START'] <= now_tw <= CONFIG['KZ2_END']:
        return 'kz2_mid'
    elif CONFIG['KZ3_START'] <= now_tw <= CONFIG['KZ3_END']:
        return 'kz3_close'
    return None


def calculate_smc_score_us(
    df_5m: pd.DataFrame,
    df_15m: Optional[pd.DataFrame] = None,
    df_1h: Optional[pd.DataFrame] = None,
    vwap_data: Optional[Dict] = None,
    orb_result: Optional[Tuple[int, List[str]]] = None,
) -> Dict:
    """
    計算美股 SMC + VWAP + ORB 多層確認綜合評分

    評分組成（總分 0–100）：
    ┌─────────────────────────────────────────────────────┐
    │  SMC 基礎分（最高 ~70 分）                            │
    │  1H  趨勢偏向（BOS/CHoCH）             +15           │
    │  15M OB 確認                           +20           │
    │  15M FVG 確認                          +10           │
    │  5M  CHoCH                             +20           │
    │  5M  BOS                               +10           │
    │  Sweep → CHoCH 組合                    +15           │
    │  CHoCH 後 FVG                          +10           │
    │  Kill Zone 加分                        +8~+15        │
    │  多時框方向一致                         +10           │
    ├─────────────────────────────────────────────────────┤
    │  VWAP 確認（最高 +25 分）                             │
    │  價格在 VWAP 正確側                     +10           │
    │  VWAP Bounce / Rejection               +15           │
    │  (逆向扣分)                             -5            │
    ├─────────────────────────────────────────────────────┤
    │  ORB 確認（最高 +20 分）                              │
    │  ORB 突破方向與 SMC 一致                +15           │
    │  成交量確認                             +5            │
    └─────────────────────────────────────────────────────┘

    入場門檻：總分 ≥ 70（CONFIG['MIN_SCORE']）

    多時框分析：1H 方向 → 15M POI → 5M 入場

    Args:
        df_5m:      5 分鐘 K 棒（必填）
        df_15m:     15 分鐘 K 棒（選填）
        df_1h:      1 小時 K 棒（選填）
        vwap_data:  VWAPCalculator.calculate() 的返回值（選填）
        orb_result: ORBTracker.get_score_contribution() 的返回值 (score, signals)（選填）

    Returns:
        {
            'score': int,               # 0–100，截斷後的最終分數
            'bias': str,                # 'bullish' / 'bearish' / 'neutral'
            'signals': List[str],       # 觸發訊號列表（含 VWAP/ORB 訊號）
            'entry_zone': tuple,        # (low, high) 建議入場區間
            'stop_price': float,        # 建議停損價
            'latest_event': str,        # 最近 5M 結構事件名稱
            'vwap_score': int,          # VWAP 貢獻分數（用於除錯）
            'orb_score': int,           # ORB 貢獻分數（用於除錯）
        }
    """
    result: Dict = {
        'score': 0, 'bias': 'neutral', 'signals': [],
        'entry_zone': (0.0, 0.0), 'stop_price': 0.0, 'latest_event': '',
        'vwap_score': 0, 'orb_score': 0,
    }

    htf_bias = Bias.NEUTRAL

    # ---- 1H 趨勢偏向 ----
    if df_1h is not None and len(df_1h) >= 15:
        try:
            swings_1h = find_swing_points(df_1h, lookback=3)
            events_1h = detect_structure(df_1h, swings_1h)
            if events_1h:
                latest_1h = events_1h[-1]
                if latest_1h.type in ('bos', 'choch'):
                    htf_bias = (Bias.BULLISH if latest_1h.direction == 'bullish'
                                else Bias.BEARISH)
                    result['bias'] = htf_bias.value
                    result['score'] += 15
                    result['signals'].append(
                        f"1H {latest_1h.type.upper()} {latest_1h.direction}")
        except Exception as e:
            logger.debug(f"1H SMC 分析失敗: {e}")

    # ---- 15M POI ----
    if df_15m is not None and len(df_15m) >= 20:
        try:
            swings_15m = find_swing_points(df_15m, lookback=3)
            events_15m = detect_structure(df_15m, swings_15m)
            obs_15m    = detect_order_blocks(df_15m, events_15m)
            fvgs_15m   = check_fvg_mitigation(detect_fvg(df_15m), df_15m)

            # 最佳 OB
            if obs_15m:
                ob = obs_15m[-1]
                if not ob.mitigated:
                    result['entry_zone'] = (ob.low, ob.high)
                    result['score'] += 20
                    result['signals'].append(f"15M OB({ob.type})")

            # 未回測 FVG
            active_fvgs = [f for f in fvgs_15m if not f.mitigated]
            if active_fvgs:
                result['score'] += 10
                result['signals'].append(f"15M FVG x{len(active_fvgs)}")
        except Exception as e:
            logger.debug(f"15M SMC 分析失敗: {e}")

    # ---- 5M 結構分析 ----
    if len(df_5m) >= 20:
        try:
            swings_5m = find_swing_points(df_5m, lookback=3)
            events_5m = detect_structure(df_5m, swings_5m)
            fvgs_5m   = check_fvg_mitigation(detect_fvg(df_5m), df_5m)

            if events_5m:
                latest_5m = events_5m[-1]
                result['latest_event'] = f"{latest_5m.type}_{latest_5m.direction}"

                if latest_5m.type == 'choch':
                    result['score'] += 20
                    result['signals'].append(f"5M CHoCH {latest_5m.direction}")
                elif latest_5m.type == 'bos':
                    result['score'] += 10
                    result['signals'].append(f"5M BOS {latest_5m.direction}")

                # Sweep → CHoCH 組合
                sweeps = [e for e in events_5m if e.type == 'sweep']
                chochs = [e for e in events_5m if e.type == 'choch']
                if sweeps and chochs:
                    if (chochs[-1].index > sweeps[-1].index and
                            chochs[-1].index - sweeps[-1].index <= 10):
                        result['score'] += 15
                        result['signals'].append("Sweep→CHoCH")

            # CHoCH 後的 FVG = 入場信號
            active_fvgs_5m = [f for f in fvgs_5m if not f.mitigated]
            if active_fvgs_5m and events_5m:
                for fvg in active_fvgs_5m[-3:]:
                    for ev in events_5m[-5:]:
                        if ev.type == 'choch' and fvg.index > ev.index:
                            result['score'] += 10
                            result['signals'].append(f"CHoCH後FVG({fvg.type})")
                            # 以 FVG 中點為入場 CE
                            if result['entry_zone'] == (0.0, 0.0):
                                result['entry_zone'] = (fvg.bottom, fvg.top)
                            break
        except Exception as e:
            logger.debug(f"5M SMC 分析失敗: {e}")

    # ---- Kill Zone 加減分 ----
    kz = get_kill_zone_us()
    if kz == 'kz1_open':
        result['score'] += 15
        result['signals'].append("🟢 KZ1 開盤")
    elif kz == 'kz2_mid':
        result['score'] += 8
        result['signals'].append("🟡 KZ2 午盤")
    elif kz == 'kz3_close':
        result['score'] += 10
        result['signals'].append("🟠 KZ3 收盤")
    else:
        result['score'] -= 5
        result['signals'].append("⚠️ 非Kill Zone")

    # ---- 多時框方向一致加分 ----
    if (htf_bias != Bias.NEUTRAL and
            result.get('latest_event', '') and
            htf_bias.value in result['latest_event']):
        result['score'] += 10
        result['signals'].append("多時框方向一致")

    # ---- VWAP 確認加分（最高 +25）----
    if vwap_data is not None:
        try:
            v_score, v_signals = VWAPCalculator.get_score_contribution(
                df_5m, vwap_data, result.get('bias', 'neutral')
            )
            result['score'] += v_score
            result['vwap_score'] = v_score
            result['signals'].extend(v_signals)
        except Exception as e:
            logger.debug(f"VWAP 評分計算失敗: {e}")

    # ---- ORB 確認加分（最高 +20）----
    if orb_result is not None:
        try:
            o_score, o_signals = orb_result
            result['score'] += o_score
            result['orb_score'] = o_score
            result['signals'].extend(o_signals)
        except Exception as e:
            logger.debug(f"ORB 評分計算失敗: {e}")

    result['score'] = max(0, min(100, result['score']))
    return result

# ============================================================================
# 技術指標補充（ATR + VWAP）
# ============================================================================

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """計算 ATR（Average True Range）"""
    try:
        atr_series = ta.volatility.average_true_range(
            df['High'], df['Low'], df['Close'], window=period
        )
        val = atr_series.iloc[-1]
        if np.isnan(val) or val <= 0:
            # fallback: 近 5 根 K 棒的平均真實區間
            tr = (df['High'] - df['Low']).tail(5).mean()
            return float(tr) if tr > 0 else float(df['Close'].iloc[-1] * 0.02)
        return float(val)
    except Exception:
        return float(df['Close'].iloc[-1] * 0.02)


def calculate_vwap(df: pd.DataFrame) -> float:
    """
    計算 VWAP（向後相容用，保留舊介面）
    
    ⚠️ 新程式碼請使用 VWAPCalculator.calculate(df) 取得完整資訊（含偏差帶）
    """
    data = VWAPCalculator.calculate(df)
    return data.get('vwap', float(df['Close'].iloc[-1]) if not df.empty else 0.0)


# ============================================================================
# VWAP 計算器（策略層級，含偏差帶與 Bounce 偵測）
# ============================================================================

class VWAPCalculator:
    """
    日內 VWAP 計算器

    功能：
    - 累積式日內 VWAP（從當日開盤起算，非固定滾動窗口）
    - VWAP 偏差帶（±1σ / ±2σ，動態支撐阻力）
    - VWAP Bounce 偵測（回測 VWAP 後反彈，高勝率進場訊號）
    - 與 SMC 整合評分（最高 +25 分）

    VWAP 是機構交易員的重要參考價位：
    - 價格在 VWAP 上方 → 多方強勢（做多優先）
    - 價格在 VWAP 下方 → 空方強勢（做空優先）
    - 回測 VWAP 後的反彈 → Bounce，高勝率順勢進場點
    - VWAP 偏差帶 ±1σ/±2σ 提供動態支撐阻力層級
    """

    @staticmethod
    def calculate(df: pd.DataFrame) -> Dict:
        """
        計算 VWAP 及偏差帶

        優先使用 Alpaca bars 內建的 'VWAP' 欄位（若有）；
        否則從 OHLCV 自行計算累積式 VWAP。

        Args:
            df: OHLCV DataFrame（index=Datetime，columns 含 High/Low/Close/Volume）

        Returns:
            {
                'vwap': float,              # 當前 VWAP 值
                'upper_1': float,           # +1σ 偏差帶
                'lower_1': float,           # -1σ 偏差帶
                'upper_2': float,           # +2σ 偏差帶
                'lower_2': float,           # -2σ 偏差帶
                'series_vwap': pd.Series,   # 完整 VWAP 時間序列
                'series_upper_1': pd.Series, # +1σ 時間序列
                'series_lower_1': pd.Series, # -1σ 時間序列
                'price_position': str,      # 'above' / 'below' / 'at'
            }
        """
        if df.empty or len(df) < 5:
            last_price = float(df['Close'].iloc[-1]) if not df.empty else 0.0
            empty_s = pd.Series(dtype=float)
            return {
                'vwap': last_price, 'upper_1': last_price, 'lower_1': last_price,
                'upper_2': last_price, 'lower_2': last_price,
                'series_vwap': empty_s,
                'series_upper_1': empty_s,
                'series_lower_1': empty_s,
                'price_position': 'at',
            }

        # 優先使用 Alpaca bars 已內建的 VWAP 欄位
        if 'VWAP' in df.columns and df['VWAP'].notna().any():
            vwap_series = df['VWAP'].ffill()
        else:
            # 手動計算累積式 VWAP = Σ(典型價格 × 成交量) / Σ(成交量)
            typical_price = (df['High'] + df['Low'] + df['Close']) / 3
            cum_tp_vol = (typical_price * df['Volume']).cumsum()
            cum_vol    = df['Volume'].cumsum().replace(0, np.nan)
            vwap_series = (cum_tp_vol / cum_vol).ffill().fillna(df['Close'])

        # 以典型價格和 VWAP 差的滾動標準差計算偏差帶
        typical_price = (df['High'] + df['Low'] + df['Close']) / 3
        win = min(20, len(df))
        deviation = (typical_price - vwap_series).rolling(window=win).std().fillna(0)

        current_vwap  = float(vwap_series.iloc[-1])
        current_dev   = float(deviation.iloc[-1])
        current_price = float(df['Close'].iloc[-1])

        upper_1 = current_vwap + 1.0 * current_dev
        lower_1 = current_vwap - 1.0 * current_dev
        upper_2 = current_vwap + 2.0 * current_dev
        lower_2 = current_vwap - 2.0 * current_dev

        # 價格相對 VWAP 位置（±0.1% 視為「在 VWAP 上」）
        threshold = current_vwap * 0.001
        if current_price > current_vwap + threshold:
            price_position = 'above'
        elif current_price < current_vwap - threshold:
            price_position = 'below'
        else:
            price_position = 'at'

        return {
            'vwap': current_vwap,
            'upper_1': upper_1,
            'lower_1': lower_1,
            'upper_2': upper_2,
            'lower_2': lower_2,
            'series_vwap': vwap_series,
            'series_upper_1': vwap_series + deviation,
            'series_lower_1': vwap_series - deviation,
            'price_position': price_position,
        }

    @staticmethod
    def detect_bounce(df: pd.DataFrame, vwap_data: Dict) -> Dict:
        """
        偵測 VWAP Bounce（回測 VWAP 後反彈）或 Rejection（被 VWAP 拒絕）

        做多 Bounce 條件（全部滿足）：
          1. 前 N 根 K 棒有影線觸及 VWAP（±0.2%）
          2. 當前收盤 > 前收盤（反彈確認）
          3. 當前收盤在 VWAP 上方

        做空 Rejection 條件（全部滿足）：
          1. 前 N 根 K 棒有影線觸及 VWAP（±0.2%）
          2. 當前收盤 < 前收盤（下跌確認）
          3. 當前收盤在 VWAP 下方

        成交量加成：volume > 近 5 根均量 1.2 倍 → 'strong'，否則 'weak'

        Args:
            df:         OHLCV DataFrame
            vwap_data:  VWAPCalculator.calculate() 的返回值

        Returns:
            {
                'bounce_long': bool,       # VWAP bounce 做多訊號
                'rejection_short': bool,   # VWAP rejection 做空訊號
                'near_vwap': bool,         # 目前是否接近 VWAP（±0.3%）
                'bounce_strength': str,    # 'strong' / 'weak' / 'none'
            }
        """
        result = {
            'bounce_long': False,
            'rejection_short': False,
            'near_vwap': False,
            'bounce_strength': 'none',
        }

        if len(df) < 5 or not vwap_data.get('vwap'):
            return result

        current_vwap  = vwap_data['vwap']
        current_price = float(df['Close'].iloc[-1])
        prev_price    = float(df['Close'].iloc[-2])

        # 是否接近 VWAP（±0.3%）
        near_threshold = current_vwap * 0.003
        result['near_vwap'] = abs(current_price - current_vwap) < near_threshold

        # 查看前幾根 K 棒是否有影線觸及 VWAP（±0.2%）
        lookback = min(5, len(df) - 1)
        touch_threshold = current_vwap * 0.002
        recent_lows  = df['Low'].iloc[-lookback:-1]    # 排除最後一根
        recent_highs = df['High'].iloc[-lookback:-1]

        touched_from_above = any(low <= current_vwap + touch_threshold for low in recent_lows)
        touched_from_below = any(high >= current_vwap - touch_threshold for high in recent_highs)

        # 成交量確認
        avg_vol    = df['Volume'].tail(5).mean()
        vol_surge  = float(df['Volume'].iloc[-1]) > avg_vol * 1.2 if avg_vol > 0 else False

        # VWAP Bounce 做多：前根觸及 VWAP（從上方），當前在 VWAP 上方且收盤反彈
        if touched_from_above and current_price > current_vwap and current_price > prev_price:
            result['bounce_long'] = True
            result['bounce_strength'] = 'strong' if vol_surge else 'weak'

        # VWAP Rejection 做空：前根觸及 VWAP（從下方），當前在 VWAP 下方且收盤下跌
        if touched_from_below and current_price < current_vwap and current_price < prev_price:
            result['rejection_short'] = True
            result['bounce_strength'] = 'strong' if vol_surge else 'weak'

        return result

    @staticmethod
    def get_score_contribution(
        df: pd.DataFrame,
        vwap_data: Dict,
        smc_bias: str,
    ) -> Tuple[int, List[str]]:
        """
        計算 VWAP 對 SMC 評分系統的貢獻分數

        評分規則（最大 +25 分）：
          +10：價格在 VWAP 正確側（多頭在上方，空頭在下方）
          +15：VWAP Bounce / Rejection 訊號（同方向時）
          -5 ：價格在 VWAP 錯誤側（逆向位置）

        Args:
            df:         OHLCV DataFrame
            vwap_data:  VWAPCalculator.calculate() 的返回值
            smc_bias:   SMC 方向偏向（'bullish' / 'bearish' / 'neutral'）

        Returns:
            (score_addition: int, signals_list: List[str])
        """
        score   = 0
        signals: List[str] = []

        if not vwap_data or not vwap_data.get('vwap'):
            return 0, signals

        position = vwap_data.get('price_position', 'at')
        bounce   = VWAPCalculator.detect_bounce(df, vwap_data)

        # 價格在 VWAP 正確側 +10
        if smc_bias == 'bullish' and position == 'above':
            score += 10
            signals.append("📊 VWAP上方(多頭確認+10)")
        elif smc_bias == 'bearish' and position == 'below':
            score += 10
            signals.append("📊 VWAP下方(空頭確認+10)")
        elif position != 'at' and smc_bias != 'neutral':
            # 方向相反 -5
            score -= 5
            signals.append("⚠️ VWAP方向逆向(-5)")

        # VWAP Bounce / Rejection +15
        if bounce['bounce_long'] and smc_bias == 'bullish':
            score += 15
            strength = "強" if bounce['bounce_strength'] == 'strong' else "弱"
            signals.append(f"🔄 VWAP Bounce({strength}+15)")
        elif bounce['rejection_short'] and smc_bias == 'bearish':
            score += 15
            strength = "強" if bounce['bounce_strength'] == 'strong' else "弱"
            signals.append(f"🔄 VWAP Rejection({strength}+15)")

        return score, signals


# ============================================================================
# ORB 追蹤器（Opening Range Breakout）
# ============================================================================

class ORBTracker:
    """
    Opening Range Breakout（ORB）追蹤器

    記錄美股開盤前 15 分鐘（09:30-09:45 ET）的最高/最低點，
    並在突破時產生交易訊號。與 SMC Kill Zone 1（21:30-23:00 台灣時間）完美對齊。

    狀態管理：
    - 每個 symbol 每個交易日只建立一次 ORB
    - 使用台灣日期字串（YYYY-MM-DD）作為 key，避免跨日污染

    台灣時間對照：
    - 09:30-09:45 ET（美東）= 21:30-21:45 台灣時間（UTC+8）
    """

    # ORB 時段邊界（美東時間）
    ORB_START_ET = dt_time(9, 30)
    ORB_END_ET   = dt_time(9, 45)   # 15 分鐘 ORB

    def __init__(self) -> None:
        # 格式：{symbol: {'high': float, 'low': float, 'range': float,
        #                  'established': bool, 'date': str, 'avg_vol': float}}
        self._orb: Dict[str, Dict] = {}

    def update_from_bars(
        self,
        symbol: str,
        df: pd.DataFrame,
    ) -> bool:
        """
        從歷史 bars 建立（或確認）ORB 區間

        從 DataFrame 中篩選 09:30-09:44 ET 的 K 棒（需 timezone-aware index），
        計算當日最高/最低點作為 ORB 邊界。

        Args:
            symbol: 股票代碼
            df:     含 Datetime index 的 OHLCV DataFrame（1Min 或 5Min bars）

        Returns:
            True 表示 ORB 已成功建立
        """
        if df.empty or len(df) < 3:
            return False

        tw_date_str = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')

        # 今日已建立則直接返回
        existing = self._orb.get(symbol, {})
        if existing.get('established') and existing.get('date') == tw_date_str:
            return True

        try:
            # 轉換 index 到美東時間
            idx = pd.to_datetime(df.index)
            if idx.tz is None:
                idx = idx.tz_localize('UTC')
            et_idx = idx.tz_convert('America/New_York')

            # 篩選 09:30 ~ 09:44 ET（ORB 期間）
            orb_mask = (
                (pd.Series(et_idx.time) >= self.ORB_START_ET) &
                (pd.Series(et_idx.time) < self.ORB_END_ET)
            ).values
            orb_bars = df[orb_mask]

            # 若無 ORB bars（可能是非開盤時段），使用前 3 根近似
            if len(orb_bars) < 1:
                orb_bars = df.iloc[:3]

            orb_high  = float(orb_bars['High'].max())
            orb_low   = float(orb_bars['Low'].min())
            orb_range = orb_high - orb_low

            # 過濾低波動日（ORB 區間 < $0.10 不值得交易）
            if orb_range < 0.10:
                logger.debug(f"{symbol} ORB 區間過小 ({orb_range:.3f})，跳過")
                return False

            self._orb[symbol] = {
                'high': orb_high,
                'low': orb_low,
                'range': orb_range,
                'established': True,
                'date': tw_date_str,
                'avg_vol': float(orb_bars['Volume'].mean()),
            }
            logger.debug(
                f"{symbol} ORB 建立: H=${orb_high:.2f} L=${orb_low:.2f} "
                f"range={orb_range:.2f}"
            )
            return True

        except Exception as e:
            logger.debug(f"{symbol} ORB 建立失敗: {e}")
            return False

    def get_orb(self, symbol: str) -> Optional[Dict]:
        """取得 symbol 的 ORB 資料（若已建立且為今日交易日）"""
        tw_date_str = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
        orb = self._orb.get(symbol)
        if orb and orb.get('established') and orb.get('date') == tw_date_str:
            return orb
        return None

    def check_breakout(
        self,
        symbol: str,
        df: pd.DataFrame,
    ) -> Optional[Dict]:
        """
        檢查 ORB 突破訊號（需 ORB 已建立）

        突破條件：
        - 做多：最新收盤 > ORB High（確認突破，非僅影線刺穿）
        - 做空：最新收盤 < ORB Low
        - 成交量確認：當前 volume > ORB 期間均量 × 1.5

        Args:
            symbol: 股票代碼
            df:     最新 OHLCV DataFrame

        Returns:
            None 若無突破，突破時返回：
            {
                'direction': 'bullish' / 'bearish',
                'breakout_price': float,
                'orb_high': float,
                'orb_low': float,
                'orb_range': float,
                'volume_confirmed': bool,   # 成交量 > ORB 均量 1.5 倍
                'target_1': float,          # orb_range × 1.5
                'target_2': float,          # orb_range × 2.0
            }
        """
        orb = self.get_orb(symbol)
        if not orb or df.empty:
            return None

        current_close  = float(df['Close'].iloc[-1])
        current_volume = float(df['Volume'].iloc[-1])
        orb_high    = orb['high']
        orb_low     = orb['low']
        orb_range   = orb['range']
        orb_avg_vol = orb.get('avg_vol', 0)

        # 成交量確認：突破時 volume > 1.5× ORB 均量
        vol_confirmed = (orb_avg_vol > 0 and current_volume > orb_avg_vol * 1.5)

        # 做多突破
        if current_close > orb_high:
            return {
                'direction': 'bullish',
                'breakout_price': current_close,
                'orb_high': orb_high,
                'orb_low': orb_low,
                'orb_range': orb_range,
                'volume_confirmed': vol_confirmed,
                'target_1': orb_high + orb_range * 1.5,
                'target_2': orb_high + orb_range * 2.0,
            }

        # 做空突破
        if current_close < orb_low:
            return {
                'direction': 'bearish',
                'breakout_price': current_close,
                'orb_high': orb_high,
                'orb_low': orb_low,
                'orb_range': orb_range,
                'volume_confirmed': vol_confirmed,
                'target_1': orb_low - orb_range * 1.5,
                'target_2': orb_low - orb_range * 2.0,
            }

        return None

    def get_score_contribution(
        self,
        symbol: str,
        df: pd.DataFrame,
        smc_bias: str,
    ) -> Tuple[int, List[str]]:
        """
        計算 ORB 對 SMC 評分系統的貢獻分數

        評分規則（最大 +20 分）：
          +15：ORB 突破方向與 SMC 方向一致
          +5 ：成交量確認（volume > ORB 均量 1.5 倍）
          0  ：ORB 未突破或方向相反（不扣分，避免懲罰非 KZ1 時段）

        Args:
            symbol:   股票代碼
            df:       OHLCV DataFrame
            smc_bias: SMC 方向偏向（'bullish' / 'bearish' / 'neutral'）

        Returns:
            (score_addition: int, signals_list: List[str])
        """
        score   = 0
        signals: List[str] = []

        if smc_bias == 'neutral':
            return 0, signals

        # 嘗試（重新）建立 ORB
        self.update_from_bars(symbol, df)

        orb = self.get_orb(symbol)
        if not orb:
            return 0, signals

        breakout = self.check_breakout(symbol, df)

        if breakout is None:
            # ORB 已建立但未觸發突破
            signals.append(
                f"📦 ORB已建立 H=${orb['high']:.2f} L=${orb['low']:.2f} "
                f"range={orb['range']:.2f}"
            )
            return 0, signals

        # 突破方向與 SMC 一致
        if breakout['direction'] == smc_bias:
            score += 15
            dir_label = "突破↑" if smc_bias == 'bullish' else "跌破↓"
            signals.append(f"🚀 ORB {dir_label}(SMC一致+15)")

            # 成交量確認加分
            if breakout['volume_confirmed']:
                score += 5
                signals.append("📈 ORB量能確認(+5)")
        else:
            # 突破方向與 SMC 相反（警告，但不扣分）
            signals.append(
                f"⚠️ ORB方向({breakout['direction']}) 與SMC({smc_bias})不一致"
            )

        return score, signals


# 全域 ORB 追蹤器（每日重置狀態由日期字串控制，跨 symbol 共用）
orb_tracker = ORBTracker()

# ============================================================================
# 風控系統
# ============================================================================

class RiskManager:
    """
    日內風控管理器

    功能：
    - 日損上限（超過 1.5% → 鎖倉）
    - Kelly Criterion × 0.3 計算部位大小
    - ATR × 2.0 停損確認
    - 連虧 3 筆冷靜機制
    """

    def __init__(self, initial_balance: float) -> None:
        self.initial_balance = initial_balance
        self.current_balance = initial_balance
        self.daily_pnl: float = 0.0
        self.trade_count: int = 0
        self.consecutive_losses: int = 0
        self.is_locked: bool = False
        self.lock_reason: str = ""

        self.max_daily_loss = initial_balance * CONFIG['MAX_DAILY_LOSS_PCT']
        logger.info(
            f"RiskManager 初始化: 餘額=${initial_balance:,.2f}, "
            f"日損上限=${self.max_daily_loss:,.2f}"
        )

    def can_trade(self) -> Tuple[bool, str]:
        """是否允許開新倉"""
        if self.is_locked:
            return False, self.lock_reason
        return True, "OK"

    def record_trade(self, pnl: float) -> None:
        """記錄一筆交易結果並更新風控狀態"""
        self.daily_pnl += pnl
        self.trade_count += 1
        self.current_balance += pnl

        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        self._check_limits()
        logger.info(
            f"交易記錄: PnL={pnl:+.2f} | 日累計={self.daily_pnl:+.2f} | "
            f"連虧={self.consecutive_losses}"
        )

    def _check_limits(self) -> None:
        if self.daily_pnl <= -self.max_daily_loss:
            self.is_locked = True
            self.lock_reason = (
                f"日損上限觸發 (${self.daily_pnl:.2f} / "
                f"-${self.max_daily_loss:.2f})"
            )
            logger.warning(f"🔒 鎖倉: {self.lock_reason}")

        if self.consecutive_losses >= 3:
            self.is_locked = True
            self.lock_reason = f"連虧 {self.consecutive_losses} 筆，冷靜 30 分鐘"
            logger.warning(f"🔒 鎖倉: {self.lock_reason}")

    def calculate_position_size(
        self, entry_price: float, stop_price: float,
        win_rate: float = 0.5, avg_win_pct: float = 0.03, avg_loss_pct: float = 0.015
    ) -> int:
        """
        計算部位大小（股數）
        
        步驟：
        1. Kelly Criterion 計算最佳風險比例
        2. × 0.3 保守係數
        3. 依 ATR 停損距離換算股數
        
        Args:
            entry_price: 進場價
            stop_price:  停損價
            win_rate:    估計勝率（預設 50%）
            avg_win_pct: 平均獲利百分比（預設 3%）
            avg_loss_pct: 平均虧損百分比（預設 1.5%）
        
        Returns:
            股數（整數，最少 1 股）
        """
        # Kelly Criterion
        R = avg_win_pct / avg_loss_pct if avg_loss_pct > 0 else 2.0
        f_kelly = win_rate - (1 - win_rate) / R
        f_kelly = max(0.01, min(0.10, f_kelly))          # 限制在 1%–10%
        f_conservative = f_kelly * CONFIG['KELLY_FRACTION']  # × 0.3

        # 最大風險金額
        risk_amount = self.current_balance * f_conservative
        risk_amount = min(risk_amount,
                          self.current_balance * CONFIG['MAX_SINGLE_RISK_PCT'])

        # 每股風險
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0:
            return 1

        shares = int(risk_amount / risk_per_share)
        shares = max(1, shares)

        # 確保不超過帳戶 10% 持倉
        max_shares = int(self.current_balance * 0.10 / entry_price)
        shares = min(shares, max_shares)

        logger.debug(
            f"部位計算: Kelly={f_kelly:.3f}×0.3={f_conservative:.3f} "
            f"risk_amt=${risk_amount:.2f} risk_share=${risk_per_share:.2f} "
            f"→ {shares} 股"
        )
        return shares


# ============================================================================
# 交易日誌
# ============================================================================

@dataclass
class TradeRecord:
    """單筆交易記錄"""
    symbol: str
    side: str           # 'buy' / 'sell'
    qty: int
    entry_price: float
    exit_price: float
    stop_price: float
    take_profit: float
    pnl: float
    pnl_pct: float
    entry_time: str
    exit_time: str
    exit_reason: str    # 'stop_loss' / 'take_profit' / 'force_close' / 'trailing'
    smc_score: int
    smc_signals: List[str]
    kill_zone: str
    order_id: str = ""


def save_trade_log(record: TradeRecord) -> None:
    """將交易記錄追加寫入當日 JSON 日誌"""
    log_file = trade_log_path()

    # 讀取現有記錄
    existing: List[Dict] = []
    if log_file.exists():
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                existing = json.load(f)
        except Exception:
            existing = []

    existing.append(asdict(record))

    with open(log_file, 'w', encoding='utf-8') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    logger.info(f"交易日誌已更新: {log_file.name} ({len(existing)} 筆)")


# ============================================================================
# 主交易引擎
# ============================================================================

class USDaytradeBot:
    """
    美股當沖模擬交易 Bot
    
    核心流程：
      1. 盤前篩選候選股（最活躍、漲跌幅榜）
      2. Kill Zone 進入時掃描 SMC 信號
      3. 評分 ≥ 門檻 → 計算部位 → Alpaca 市價買入
      4. 每 20 秒監控持倉 → 觸發停損/停利/移動停損 → 市價平倉
      5. 日損上限觸發 → 全部平倉並鎖倉

    使用 Alpaca Paper Trading API，禁止真實下單
    """

    def __init__(self) -> None:
        self.client = alpaca
        self.risk_mgr: Optional[RiskManager] = None
        self.watchlist: List[str] = []

        # 當前持倉快照：{symbol: position_info}
        self.positions: Dict[str, Dict] = {}

        # 初始化帳戶
        self._init_account()

    def _init_account(self) -> None:
        """初始化帳戶資訊"""
        try:
            acc = self.client.get_account()
            balance = float(acc.get('equity', 100000))
            self.risk_mgr = RiskManager(balance)
            logger.info(
                f"帳戶初始化: equity=${balance:,.2f} "
                f"cash=${float(acc.get('cash', 0)):,.2f} "
                f"buying_power=${float(acc.get('buying_power', 0)):,.2f}"
            )
        except Exception as e:
            logger.error(f"帳戶初始化失敗: {e}")
            self.risk_mgr = RiskManager(100_000.0)

    def run(self) -> None:
        """主迴圈入口"""
        logger.info(f"🚀 US DayTrade Bot v{__version__} 啟動")
        logger.info(f"Kill Zones（台灣時間）:")
        logger.info(f"  KZ1 開盤: 21:30 – 23:00")
        logger.info(f"  KZ2 午盤: 00:00 – 01:30")
        logger.info(f"  KZ3 收盤: 02:30 – 04:00")

        # 盤前篩選
        self.watchlist = premarket_screener(self.client, top_n=CONFIG['WATCHLIST_SIZE'])
        if not self.watchlist:
            logger.warning("候選股清單為空，使用備用清單")
            self.watchlist = ['AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMD',
                              'META', 'AMZN', 'GOOGL', 'SPY', 'QQQ']

        logger.info(f"監控清單: {self.watchlist}")

        try:
            while True:
                self._loop()
        except KeyboardInterrupt:
            logger.info("Bot 停止（使用者中斷）")
            self._close_all_positions("手動停止")
        except Exception as e:
            logger.critical(f"主迴圈異常: {e}\n{traceback.format_exc()}")
            self._close_all_positions("緊急平倉")

    def _loop(self) -> None:
        """單次掃描迴圈"""
        now_tw = datetime.now(timezone(timedelta(hours=8)))
        now_tw_t = now_tw.time()

        # 強制平倉時間（04:15 台灣時間）
        if now_tw_t >= CONFIG['FORCE_CLOSE']:
            if self.positions:
                logger.info("⏰ 強制平倉時間")
                self._close_all_positions("強制平倉 04:15")
            time.sleep(60)
            return

        # 風控鎖倉
        can, reason = self.risk_mgr.can_trade()
        if not can:
            logger.info(f"🔒 {reason}")
            if self.positions:
                self._close_all_positions(f"風控:{reason}")
            time.sleep(60)
            return

        # 市場開盤確認
        if not self.client.is_market_open():
            logger.debug("市場未開盤，等待中...")
            time.sleep(30)
            return

        kz = get_kill_zone_us()

        # 監控現有持倉（有持倉時優先）
        if self.positions:
            self._monitor_positions()
            time.sleep(CONFIG['MONITOR_INTERVAL_SEC'])
            return

        # Kill Zone 掃描入場信號
        if kz and now_tw_t < CONFIG['NO_NEW_TRADE_AFTER']:
            if len(self.positions) < CONFIG['MAX_POSITIONS']:
                self._scan_entries(kz)
            time.sleep(CONFIG['SCAN_INTERVAL_SEC'])
        else:
            time.sleep(CONFIG['SCAN_INTERVAL_SEC'])

    def _scan_entries(self, kz: str) -> None:
        """掃描候選股，尋找 SMC 入場信號"""
        logger.info(f"=== 掃描入場 [{kz}] — {len(self.watchlist)} 支 ===")

        for symbol in self.watchlist:
            if symbol in self.positions:
                continue
            if len(self.positions) >= CONFIG['MAX_POSITIONS']:
                break
            try:
                self._analyze_and_enter(symbol, kz)
            except Exception as e:
                logger.warning(f"{symbol} 分析失敗: {e}")

    def _analyze_and_enter(self, symbol: str, kz: str) -> None:
        """
        分析單一股票並決定是否進場

        步驟：
        1. 取得 5M / 15M / 1H K 線
        2. 計算 ATR、VWAP
        3. SMC 評分
        4. 評分 ≥ 門檻 → 計算停損 → 計算部位 → 下市價單
        """
        # 取得多時框 K 線
        df_5m  = self.client.get_bars(symbol, '5Min',  limit=100)
        df_15m = self.client.get_bars(symbol, '15Min', limit=100)
        df_1h  = self.client.get_bars(symbol, '1Hour', limit=60)

        if df_5m.empty or len(df_5m) < 20:
            logger.debug(f"{symbol} 資料不足，跳過")
            return

        # 成交量門檻（近 5 根平均）
        avg_vol = df_5m['Volume'].tail(5).mean()
        if avg_vol < CONFIG['MIN_VOLUME'] / 78:   # 1M/day ÷ 78個5min = ~12800
            # 粗估：1M 日量 ÷ 78 根 5min K 棒
            pass  # 5min bar volume 門檻較寬鬆，不強制篩除

        # 取得最新價格
        current_price = float(df_5m['Close'].iloc[-1])
        if current_price < CONFIG['MIN_PRICE'] or current_price > CONFIG['MAX_PRICE']:
            return

        # 計算 ATR（5M）
        atr = calculate_atr(df_5m)

        # ---- VWAP 計算 ----
        # 使用 VWAPCalculator 取得完整 VWAP 資訊（含偏差帶）
        vwap_data = VWAPCalculator.calculate(df_5m)

        # ---- SMC + VWAP 聯合評分（第一階段）----
        # 傳入 vwap_data 讓 calculate_smc_score_us 計算 VWAP 加分
        smc = calculate_smc_score_us(
            df_5m,
            df_15m if not df_15m.empty else None,
            df_1h  if not df_1h.empty  else None,
            vwap_data=vwap_data,
        )
        bias    = smc['bias']
        signals = smc['signals']

        # ---- ORB 確認（第二階段）----
        # 在 SMC 方向確定後，計算 ORB 加分並直接疊加
        orb_score, orb_signals = orb_tracker.get_score_contribution(
            symbol, df_5m, bias
        )
        smc['score']    = min(100, smc['score'] + orb_score)
        smc['orb_score'] = orb_score
        smc['signals'].extend(orb_signals)
        signals = smc['signals']   # 更新 signals 引用

        score = smc['score']

        logger.debug(
            f"{symbol}: 價格=${current_price:.2f} ATR={atr:.3f} "
            f"VWAP=${vwap_data['vwap']:.2f}({vwap_data['price_position']}) "
            f"SMC+VWAP={smc['score']-orb_score} ORB={orb_score} "
            f"總分={score} bias={bias}"
        )

        # 只做多頭（bullish bias）
        if bias != 'bullish':
            return

        if score < CONFIG['MIN_SCORE']:
            logger.debug(f"{symbol} 評分 {score} < {CONFIG['MIN_SCORE']}，跳過")
            return

        # 停損價：ATR × 2.0 下方
        stop_price    = current_price - CONFIG['ATR_STOP_MULT'] * atr
        take_profit   = current_price + CONFIG['ATR_TP_MULT'] * atr

        # 部位大小
        qty = self.risk_mgr.calculate_position_size(
            entry_price=current_price,
            stop_price=stop_price
        )
        if qty <= 0:
            logger.warning(f"{symbol} 計算部位為 0，跳過")
            return

        # 下市價買入單
        client_oid = f"us_bot_{symbol}_{int(time.time())}"
        logger.info(
            f"📈 進場: {symbol} @ ~${current_price:.2f} "
            f"qty={qty} stop=${stop_price:.2f} tp=${take_profit:.2f} "
            f"SMC={score} [{', '.join(signals[:3])}...]"
        )

        order = self.client.place_order(
            symbol=symbol,
            qty=qty,
            side='buy',
            order_type='market',
            time_in_force='day',
            client_order_id=client_oid,
        )
        order_id = order.get('id', '')
        filled_price = float(order.get('filled_avg_price') or current_price)

        # 記錄持倉
        self.positions[symbol] = {
            'qty': qty,
            'entry_price': filled_price,
            'stop_price': stop_price,
            'take_profit': take_profit,
            'atr': atr,
            'entry_time': datetime.now(timezone(timedelta(hours=8))).isoformat(),
            'smc_score': score,
            'smc_signals': signals,
            'kill_zone': kz,
            'order_id': order_id,
            'highest_price': filled_price,   # 移動停損追蹤
        }

        logger.info(
            f"✅ 買入成功: {symbol} {qty}股 @ ${filled_price:.2f} "
            f"order_id={order_id}"
        )

    def _monitor_positions(self) -> None:
        """監控所有持倉，決定是否出場"""
        positions_to_close: List[Tuple[str, str]] = []

        for symbol, pos in list(self.positions.items()):
            try:
                current_price = self.client.get_latest_price(symbol)
                if current_price <= 0:
                    logger.warning(f"{symbol} 無法取得最新價格")
                    continue

                # 更新最高價（移動停損）
                if current_price > pos['highest_price']:
                    pos['highest_price'] = current_price
                    # 移動停損：最高價 - ATR×2.0
                    trailing_stop = current_price - CONFIG['ATR_STOP_MULT'] * pos['atr']
                    if trailing_stop > pos['stop_price']:
                        pos['stop_price'] = trailing_stop
                        logger.debug(
                            f"{symbol} 移動停損更新: ${trailing_stop:.2f}"
                        )

                pnl = (current_price - pos['entry_price']) * pos['qty']
                pnl_pct = (current_price - pos['entry_price']) / pos['entry_price']

                exit_reason: Optional[str] = None

                # 停損
                if current_price <= pos['stop_price']:
                    exit_reason = 'stop_loss'
                    logger.info(
                        f"🛑 停損: {symbol} @ ${current_price:.2f} "
                        f"(stop=${pos['stop_price']:.2f}) PnL={pnl_pct:+.2%}"
                    )

                # 停利
                elif current_price >= pos['take_profit']:
                    exit_reason = 'take_profit'
                    logger.info(
                        f"💰 停利: {symbol} @ ${current_price:.2f} "
                        f"(tp=${pos['take_profit']:.2f}) PnL={pnl_pct:+.2%}"
                    )

                # 強制平倉時間
                now_tw_t = datetime.now(timezone(timedelta(hours=8))).time()
                if now_tw_t >= CONFIG['FORCE_CLOSE']:
                    exit_reason = 'force_close'

                if exit_reason:
                    positions_to_close.append((symbol, exit_reason))

            except Exception as e:
                logger.error(f"{symbol} 監控異常: {e}")

        for symbol, reason in positions_to_close:
            self._exit_position(symbol, reason)

    def _exit_position(self, symbol: str, reason: str) -> None:
        """
        平倉單一持倉

        Args:
            symbol: 股票代碼
            reason: 出場原因（stop_loss / take_profit / force_close）
        """
        pos = self.positions.get(symbol)
        if not pos:
            return

        try:
            # 取得最新成交價
            current_price = self.client.get_latest_price(symbol)
            if current_price <= 0:
                current_price = pos['entry_price']

            # 下市價賣出單
            order = self.client.close_position(symbol)
            exit_price = float(order.get('filled_avg_price') or current_price)

        except Exception as e:
            logger.error(f"{symbol} 平倉下單失敗: {e}")
            exit_price = current_price if current_price > 0 else pos['entry_price']

        pnl = (exit_price - pos['entry_price']) * pos['qty']
        pnl_pct = (exit_price - pos['entry_price']) / pos['entry_price']

        logger.info(
            f"{'✅' if pnl >= 0 else '❌'} 出場: {symbol} "
            f"入={pos['entry_price']:.2f} 出={exit_price:.2f} "
            f"PnL=${pnl:+.2f} ({pnl_pct:+.2%}) 原因={reason}"
        )

        # 更新風控
        self.risk_mgr.record_trade(pnl)

        # 記錄交易日誌
        record = TradeRecord(
            symbol=symbol,
            side='buy',
            qty=pos['qty'],
            entry_price=pos['entry_price'],
            exit_price=exit_price,
            stop_price=pos['stop_price'],
            take_profit=pos['take_profit'],
            pnl=pnl,
            pnl_pct=pnl_pct,
            entry_time=pos['entry_time'],
            exit_time=datetime.now(timezone(timedelta(hours=8))).isoformat(),
            exit_reason=reason,
            smc_score=pos['smc_score'],
            smc_signals=pos['smc_signals'],
            kill_zone=pos['kill_zone'],
            order_id=pos.get('order_id', ''),
        )
        save_trade_log(record)

        # 移除持倉
        del self.positions[symbol]

    def _close_all_positions(self, reason: str) -> None:
        """強制平倉所有持倉"""
        logger.info(f"=== 全部平倉: {reason} ===")
        for symbol in list(self.positions.keys()):
            try:
                self._exit_position(symbol, reason)
            except Exception as e:
                logger.error(f"{symbol} 強制平倉失敗: {e}")
        logger.info("=== 全部平倉完成 ===")


# ============================================================================
# CLI 工具命令
# ============================================================================

def cmd_account() -> None:
    """顯示帳戶資訊"""
    try:
        acc = alpaca.get_account()
        print("\n📊 Alpaca Paper Trading 帳戶")
        print(f"  帳號:       {acc.get('id', 'N/A')}")
        print(f"  Equity:     ${float(acc.get('equity', 0)):,.2f}")
        print(f"  Cash:       ${float(acc.get('cash', 0)):,.2f}")
        print(f"  買入力:     ${float(acc.get('buying_power', 0)):,.2f}")
        print(f"  日損益:     ${float(acc.get('equity', 0)) - float(acc.get('last_equity', acc.get('equity', 0))):+,.2f}")
        print(f"  狀態:       {acc.get('status', 'N/A')}\n")
    except Exception as e:
        print(f"查詢帳戶失敗: {e}")


def cmd_positions() -> None:
    """顯示當前持倉"""
    try:
        positions = alpaca.get_positions()
        if not positions:
            print("目前無持倉")
            return
        print(f"\n📈 當前持倉（{len(positions)} 支）")
        for p in positions:
            sym = p.get('symbol', '')
            qty = p.get('qty', 0)
            avg_entry = float(p.get('avg_entry_price', 0))
            cur_price = float(p.get('current_price', 0))
            unreal_pl = float(p.get('unrealized_pl', 0))
            unreal_plpc = float(p.get('unrealized_plpc', 0))
            print(
                f"  {sym:6s} {qty:>5} 股  "
                f"均價=${avg_entry:.2f}  現價=${cur_price:.2f}  "
                f"未實現={unreal_pl:+.2f} ({unreal_plpc:+.2%})"
            )
        print()
    except Exception as e:
        print(f"查詢持倉失敗: {e}")


def cmd_screener() -> None:
    """執行盤前篩選並顯示結果"""
    print("\n🔍 盤前篩選...")
    result = premarket_screener(alpaca, top_n=CONFIG['WATCHLIST_SIZE'])
    print(f"候選股（{len(result)} 支）: {result}\n")


def cmd_analyze(symbol: str) -> None:
    """分析單一股票的 SMC + VWAP + ORB 多層確認評分"""
    print(f"\n🔬 分析: {symbol}（多層確認系統 v{__version__}）")
    try:
        df_5m  = alpaca.get_bars(symbol, '5Min',  limit=100)
        df_15m = alpaca.get_bars(symbol, '15Min', limit=100)
        df_1h  = alpaca.get_bars(symbol, '1Hour', limit=60)

        if df_5m.empty:
            print(f"無法取得 {symbol} 資料")
            return

        current_price = float(df_5m['Close'].iloc[-1])
        atr = calculate_atr(df_5m)

        # VWAP 完整計算
        vwap_data = VWAPCalculator.calculate(df_5m)

        # SMC + VWAP 評分
        smc = calculate_smc_score_us(
            df_5m,
            df_15m if not df_15m.empty else None,
            df_1h  if not df_1h.empty  else None,
            vwap_data=vwap_data,
        )

        # ORB 追蹤與評分
        orb_score, orb_signals = orb_tracker.get_score_contribution(
            symbol, df_5m, smc['bias']
        )
        final_score = min(100, smc['score'] + orb_score)
        all_signals = smc['signals'] + [s for s in orb_signals
                                         if s not in smc['signals']]

        stop = current_price - CONFIG['ATR_STOP_MULT'] * atr
        tp   = current_price + CONFIG['ATR_TP_MULT']  * atr
        kz   = get_kill_zone_us()

        # 取得 ORB 資料顯示
        orb = orb_tracker.get_orb(symbol)

        print(f"  現價:      ${current_price:.2f}")
        print(f"  ATR:       {atr:.3f}")
        print()
        print(f"  ── VWAP ──────────────────────────────")
        print(f"  VWAP:      ${vwap_data['vwap']:.2f}  "
              f"（價格在 {vwap_data['price_position']}）")
        print(f"  +1σ 帶:    ${vwap_data['upper_1']:.2f}")
        print(f"  -1σ 帶:    ${vwap_data['lower_1']:.2f}")
        print(f"  +2σ 帶:    ${vwap_data['upper_2']:.2f}")
        print(f"  -2σ 帶:    ${vwap_data['lower_2']:.2f}")
        print(f"  VWAP加分:  {smc.get('vwap_score', 0):+d}")
        print()
        print(f"  ── ORB（開盤前15分鐘）───────────────")
        if orb:
            orb_breakout = orb_tracker.check_breakout(symbol, df_5m)
            print(f"  ORB High:  ${orb['high']:.2f}")
            print(f"  ORB Low:   ${orb['low']:.2f}")
            print(f"  ORB Range: ${orb['range']:.2f}")
            if orb_breakout:
                print(f"  突破方向:  {orb_breakout['direction']} "
                      f"{'✅量確認' if orb_breakout['volume_confirmed'] else '⚠️量未確認'}")
                print(f"  T1 目標:   ${orb_breakout['target_1']:.2f}")
            else:
                print(f"  狀態:      尚未突破")
        else:
            print(f"  狀態:      ORB 未建立（需 09:30-09:45 ET 資料）")
        print(f"  ORB加分:   {orb_score:+d}")
        print()
        print(f"  ── 評分總結 ──────────────────────────")
        print(f"  SMC基礎分: {smc['score'] - smc.get('vwap_score', 0)}")
        print(f"  VWAP加分:  {smc.get('vwap_score', 0):+d}")
        print(f"  ORB加分:   {orb_score:+d}")
        print(f"  最終評分:  {final_score} / 100  "
              f"（門檻 ≥ {CONFIG['MIN_SCORE']}）")
        print(f"  方向偏向:  {smc['bias']}")
        print(f"  觸發訊號:  {', '.join(all_signals)}")
        print()
        print(f"  建議停損:  ${stop:.2f} (-{CONFIG['ATR_STOP_MULT']}×ATR)")
        print(f"  建議停利:  ${tp:.2f} (+{CONFIG['ATR_TP_MULT']}×ATR)")
        print(f"  Kill Zone: {kz or '非KZ時段'}")
        can_enter = (final_score >= CONFIG['MIN_SCORE'] and smc['bias'] == 'bullish')
        print(f"  入場建議:  {'✅ 可入場' if can_enter else '❌ 不建議'}\n")

    except Exception as e:
        print(f"分析失敗: {e}\n{traceback.format_exc()}")


def cmd_trade_log() -> None:
    """顯示當日交易日誌"""
    log_file = trade_log_path()
    if not log_file.exists():
        print("今日尚無交易記錄")
        return

    with open(log_file, 'r', encoding='utf-8') as f:
        trades = json.load(f)

    print(f"\n📋 今日交易記錄（{len(trades)} 筆）")
    total_pnl = 0.0
    for t in trades:
        pnl = t.get('pnl', 0)
        total_pnl += pnl
        print(
            f"  {t['symbol']:6s} {t['side']:4s} {t['qty']:>4}股  "
            f"入=${t['entry_price']:.2f} 出=${t['exit_price']:.2f}  "
            f"PnL={pnl:+.2f} ({t['pnl_pct']:+.2%})  "
            f"原因={t['exit_reason']}  KZ={t['kill_zone']}"
        )
    print(f"\n  日累計 PnL: ${total_pnl:+.2f}\n")


def print_help() -> None:
    print(f"""
US DayTrade Bot v{__version__} — Alpaca Paper Trading

用法:
  python3 us-daytrade-bot.py [命令]

命令:
  run             啟動主交易循環
  account         顯示帳戶資訊
  positions       顯示當前持倉
  screener        執行盤前篩選
  analyze SYMBOL  分析單一股票（SMC + VWAP + ORB，如 AAPL）
  log             顯示今日交易日誌
  help            顯示此說明

交易時段（台灣時間）:
  KZ1 美股開盤: 21:30 – 23:00
  KZ2 午盤:     00:00 – 01:30
  KZ3 收盤前:   02:30 – 04:00

評分系統（多層確認，門檻 ≥ {CONFIG['MIN_SCORE']} 入場）:
  SMC Engine:  OB / FVG / BOS / CHoCH / Sweep（最高 ~70 分）
  VWAP 確認:   正確側 +10，Bounce +15，逆向 -5（最高 +25）
  ORB 確認:    突破一致 +15，量確認 +5（最高 +20）

風控:
  日損上限:   {CONFIG['MAX_DAILY_LOSS_PCT']*100:.1f}%
  Kelly 係數: {CONFIG['KELLY_FRACTION']}
  ATR 停損:   ×{CONFIG['ATR_STOP_MULT']}
  ATR 停利:   ×{CONFIG['ATR_TP_MULT']}
""")


# ============================================================================
# 入口點
# ============================================================================

if __name__ == '__main__':
    args = sys.argv[1:]

    if not args or args[0] in ('help', '--help', '-h'):
        print_help()

    elif args[0] == 'run':
        bot = USDaytradeBot()
        bot.run()

    elif args[0] == 'account':
        cmd_account()

    elif args[0] == 'positions':
        cmd_positions()

    elif args[0] == 'screener':
        cmd_screener()

    elif args[0] == 'analyze':
        if len(args) < 2:
            print("請指定股票代碼，例如: python3 us-daytrade-bot.py analyze AAPL")
        else:
            cmd_analyze(args[1].upper())

    elif args[0] == 'log':
        cmd_trade_log()

    else:
        print(f"未知命令: {args[0]}")
        print_help()
