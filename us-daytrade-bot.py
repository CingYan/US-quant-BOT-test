#!/usr/bin/env -S uv run --python 3.13 python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "yfinance",
#   "pandas",
#   "numpy",
#   "ta",
#   "requests",
# ]
# ///
# -*- coding: utf-8 -*-
"""
美股當沖模擬交易 Bot — Alpaca Paper Trading + SMC + VWAP

v2 重點修正（來自 trading-lessons.md）：
  - 多時框衝突 → HARD REJECT（非扣分）
  - FVG 飽和度 ≥7 → HARD REJECT
  - 連虧 3 筆 → 今日停止交易（非 1 小時後解鎖）
  - 交易記錄進場時立即寫入磁碟
  - Watchdog 主迴圈：永不永久崩潰
  - Heartbeat 每 30 秒寫入 /tmp/us-bot-heartbeat
  - SIGTERM/SIGINT 優雅關閉
  - Super circuit breaker: 日虧 > $10K 或 > -5% → 停止交易

v2.0.2（2026-08-13，來自 08-12 場次檢討）：
  - long-only：bias 非 bullish 一律拒絕（過去 bearish setup 仍以 long 進場）
  - 全域停損冷卻：任一標的停損後 10 分鐘內不得換股開新倉
  - 移動停損在獲利區出場不再被誤判成停損（避免誤觸同股 ban）

⚠️ 僅使用 Paper Trading endpoint，禁止真實下單
"""

__version__ = "2.0.2"

import sys
import os
import json
import signal
import logging
import time
import traceback
from datetime import datetime, date, time as dt_time, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ============================================================================
# Path setup
# ============================================================================
_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))

# ============================================================================
# CONFIG
# ============================================================================
CONFIG = {
    'CAPITAL': 100_000,           # USD — Alpaca paper account
    'MAX_DAILY_LOSS_PCT': 0.05,   # 5%
    'MAX_DAILY_LOSS_USD': 10_000, # Super circuit breaker: $10K
    'MAX_POSITIONS': 3,
    'KELLY_FRACTION': 0.3,
    'MAX_SINGLE_RISK_PCT': 0.005,
    'ATR_STOP_MULT': 2.5,
    'ATR_TP_MULT': 3.5,
    'RE_ENTRY_COOLDOWN_SEC': 600,
    'ENTRY_COOLDOWN_SEC': 120,
    'GLOBAL_STOP_COOLDOWN_SEC': 600,  # 任一標的停損後，全域禁止新倉 10 分鐘
    'MIN_SCORE': 70,
    'KZ1_MIN_SCORE': 85,          # KZ1 開盤 grace period 85 分
    'FVG_SATURATION_THRESHOLD': 7,
    'CONSECUTIVE_LOSS_LIMIT': 3,
    'HEARTBEAT_INTERVAL_SEC': 30,
    'NOTIFY_CHAT_ID': os.environ.get('DAYTRADE_NOTIFY_CHAT_ID', ''),
    'SCAN_INTERVAL_SEC': 60,
    'MONITOR_INTERVAL_SEC': 20,
    'STALE_QUOTE_SEC': 30,        # Reject if quote age > 30s
    # US market hours (ET) expressed as Taiwan time (UTC+8, winter: ET+13)
    # KZ1: 09:30-10:30 ET = 22:30-23:30 TW (winter)
    # KZ2: 12:00-13:30 ET = 01:00-02:30 TW+1 (winter)
    # KZ3: 14:30-15:50 ET = 03:30-04:50 TW+1 (winter)
    # Force close: 15:50 ET = 04:50 TW+1 (winter)
    # No new positions: 15:30 ET = 04:30 TW+1 (winter)
}

# Hardcoded watchlist
WATCHLIST = ['AAPL', 'MSFT', 'NVDA', 'TSLA', 'META', 'GOOGL', 'AMZN', 'JPM', 'QQQ', 'SPY']

# ============================================================================
# Paths
# ============================================================================
BASE_DIR = Path(os.environ.get('DAYTRADE_BASE_DIR', Path(__file__).resolve().parent))
CRED_FILE = Path(os.environ.get('DAYTRADE_CRED_FILE',
                                str(BASE_DIR / 'credentials' / 'credentials.json')))
DATA_DIR = BASE_DIR / 'data' / 'us-daytrade'
DATA_DIR.mkdir(parents=True, exist_ok=True)
HEARTBEAT_FILE = Path('/tmp/us-bot-heartbeat')

TW_TZ = timezone(timedelta(hours=8))
ET_WINTER_OFFSET = timedelta(hours=-5)   # UTC-5 (EST)
ET_SUMMER_OFFSET = timedelta(hours=-4)   # UTC-4 (EDT)


def _tw_now() -> datetime:
    return datetime.now(TW_TZ)


def _et_now() -> datetime:
    """Current time in ET (auto-detect summer/winter)."""
    utc_now = datetime.now(timezone.utc)
    # DST: second Sunday of March to first Sunday of November
    year = utc_now.year
    march = datetime(year, 3, 8, 2, 0, tzinfo=timezone.utc)
    while march.weekday() != 6:
        march += timedelta(days=1)
    november = datetime(year, 11, 1, 2, 0, tzinfo=timezone.utc)
    while november.weekday() != 6:
        november += timedelta(days=1)
    is_dst = march <= utc_now < november
    offset = ET_SUMMER_OFFSET if is_dst else ET_WINTER_OFFSET
    return utc_now.astimezone(timezone(offset))


def _today_str() -> str:
    return _tw_now().strftime('%Y-%m-%d')


def _session_date_str() -> str:
    """
    美股交易日 = ET 日期。

    US session（09:30-16:00 ET）跨過台灣午夜，若用台灣日曆日當檔名，同一場交易
    會被切成兩個檔，而且記憶體中的整份交易清單會被重寫進隔天的檔案 → 跨日重複累積。
    ET 日期在盤中永不換日，是唯一穩定的歸屬鍵。
    """
    return _et_now().strftime('%Y-%m-%d')


def _session_start_dt() -> datetime:
    """當前美股交易日的 session 起點（08:00 ET，涵蓋盤前啟動）。"""
    et = _et_now()
    return et.replace(hour=8, minute=0, second=0, microsecond=0)


def bot_log_path() -> Path:
    return DATA_DIR / f'bot-{_today_str()}.log'


# ============================================================================
# Logging
# ============================================================================
def setup_logging() -> logging.Logger:
    lg = logging.getLogger('us_daytrade_bot')
    lg.setLevel(logging.DEBUG)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    fh = logging.FileHandler(bot_log_path(), encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    lg.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    lg.addHandler(ch)
    return lg


logger = setup_logging()

# ============================================================================
# Credentials
# ============================================================================
def load_credentials() -> Dict:
    try:
        return json.loads(CRED_FILE.read_text(encoding='utf-8'))
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

if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    logger.critical("Alpaca API Key / Secret 未設定")
    sys.exit(1)

# ============================================================================
# Lazy imports
# ============================================================================
_pd = None
_np = None
_yf = None
_ta = None


def _ensure_deps():
    global _pd, _np, _yf, _ta
    if _pd is not None:
        return
    import pandas as __pd
    import numpy as __np
    import yfinance as __yf
    import ta as __ta
    _pd = __pd
    _np = __np
    _yf = __yf
    _ta = __ta


# ============================================================================
# Core module imports
# ============================================================================
from v2.core.smc_engine import SMCEngine, VWAPCalculator, calculate_atr
from v2.core.risk_manager import RiskManager
from v2.core.notification import TelegramNotifier
from v2.core.trade_recorder import TradeRecorder

# ============================================================================
# Alpaca REST API helpers
# ============================================================================
import urllib.request
import urllib.parse


def _alpaca_request(method: str, endpoint: str, body: Optional[Dict] = None) -> Optional[Dict]:
    url = f"{ALPACA_BASE_URL}{endpoint}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('APCA-API-KEY-ID', ALPACA_API_KEY)
    req.add_header('APCA-API-SECRET-KEY', ALPACA_SECRET_KEY)
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"Alpaca API {method} {endpoint} 失敗: {e}")
        return None


def alpaca_get_account() -> Optional[Dict]:
    return _alpaca_request('GET', '/v2/account')


def alpaca_get_positions() -> List[Dict]:
    result = _alpaca_request('GET', '/v2/positions')
    return result if isinstance(result, list) else []


def alpaca_place_order(symbol: str, qty: int, side: str = 'buy') -> Optional[Dict]:
    """Place market order. Returns order dict or None."""
    body = {
        'symbol': symbol,
        'qty': str(qty),
        'side': side,
        'type': 'market',
        'time_in_force': 'day',
    }
    return _alpaca_request('POST', '/v2/orders', body)


def alpaca_close_position(symbol: str) -> Optional[Dict]:
    """Close position by liquidating all shares."""
    return _alpaca_request('DELETE', f'/v2/positions/{symbol}')


def alpaca_cancel_order(order_id: str) -> Optional[Dict]:
    """Cancel an open order."""
    return _alpaca_request('DELETE', f'/v2/orders/{order_id}')


def alpaca_get_order(order_id: str) -> Optional[Dict]:
    return _alpaca_request('GET', f'/v2/orders/{order_id}')


def _filled_qty(order: Dict) -> int:
    try:
        return int(float(order.get('filled_qty') or 0))
    except (TypeError, ValueError):
        return 0


def _filled_price(order: Dict) -> Optional[float]:
    price = order.get('filled_avg_price')
    try:
        return float(price) if price else None
    except (TypeError, ValueError):
        return None


def alpaca_poll_fill(order_id: str, timeout_sec: int = 30) -> Tuple[Optional[float], int]:
    """Poll until order is filled. Returns (filled_avg_price, filled_qty)."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        order = alpaca_get_order(order_id)
        if order is None:
            time.sleep(1)
            continue
        status = order.get('status', '')
        if status == 'filled':
            return _filled_price(order), _filled_qty(order)
        if status in ('canceled', 'expired', 'rejected'):
            qty = _filled_qty(order)
            if qty > 0:
                return _filled_price(order), qty
            logger.warning(f"Order {order_id} {status}")
            return None, 0
        time.sleep(1)

    logger.warning(f"Order {order_id} fill timeout")
    alpaca_cancel_order(order_id)

    # Market orders may fill shortly after the timeout/cancel request. Do one
    # final bounded check so filled paper orders are recorded and monitored.
    final_deadline = time.time() + 5
    while time.time() < final_deadline:
        order = alpaca_get_order(order_id)
        if order is None:
            time.sleep(1)
            continue
        qty = _filled_qty(order)
        if qty > 0:
            logger.warning(f"Order {order_id} timeout後確認已成交 qty={qty}")
            return _filled_price(order), qty
        if order.get('status', '') in ('canceled', 'expired', 'rejected'):
            return None, 0
        time.sleep(1)
    return None, 0


def get_latest_price_alpaca(symbol: str) -> Tuple[float, float]:
    """Get latest price and timestamp via yfinance. Returns (price, ts)."""
    _ensure_deps()
    try:
        ticker = _yf.Ticker(symbol)
        hist = ticker.history(period='1d', interval='1m')
        if not hist.empty:
            price = float(hist['Close'].iloc[-1])
            ts = time.time()  # approximate
            return price, ts
    except Exception:
        pass
    return 0.0, 0.0


def get_quote_with_age(symbol: str) -> Tuple[float, float]:
    """Returns (price, age_seconds). age=0 means fresh."""
    price, ts = get_latest_price_alpaca(symbol)
    age = time.time() - ts if ts > 0 else 999
    return price, age


# ============================================================================
# Market hours helpers
# ============================================================================

def get_kill_zone_us() -> Optional[str]:
    """Determine current Kill Zone based on ET time."""
    et = _et_now()
    t = et.time()
    if dt_time(9, 30) <= t <= dt_time(10, 30):
        return 'kz1_open'
    if dt_time(12, 0) <= t <= dt_time(13, 30):
        return 'kz2_mid'
    if dt_time(14, 30) <= t <= dt_time(15, 50):
        return 'kz3_close'
    return None


def is_market_open_us() -> bool:
    et = _et_now()
    t = et.time()
    # Mon-Fri only
    if et.weekday() >= 5:
        return False
    return dt_time(9, 30) <= t <= dt_time(16, 0)


def is_force_close_time_us() -> bool:
    et = _et_now()
    return et.time() >= dt_time(15, 50)


def is_no_new_trade_time_us() -> bool:
    et = _et_now()
    return et.time() >= dt_time(15, 30)


def is_opening_grace_period() -> bool:
    """First 20 minutes after 09:30 ET — higher bar required."""
    et = _et_now()
    t = et.time()
    return dt_time(9, 30) <= t <= dt_time(9, 50)


# ============================================================================
# K-bar data
# ============================================================================
def get_kbars_us(symbol: str, interval: str = '5m', period: str = '5d') -> 'object':
    _ensure_deps()
    try:
        df = _yf.download(symbol, period=period, interval=interval, progress=False)
        if df.empty:
            return _pd.DataFrame()
        if isinstance(df.columns, _pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        logger.debug(f"{symbol} yfinance 失敗: {e}")
        return _pd.DataFrame()


# ============================================================================
# ORB Tracker
# ============================================================================
class ORBTracker:
    """Opening Range Breakout: 09:30-09:45 ET"""

    def __init__(self):
        self._orb: Dict[str, Dict] = {}  # symbol → {high, low, avg_vol}

    def update(self, symbol: str, df_5m: object) -> None:
        """Compute ORB from first 3 bars (09:30-09:45 ET)."""
        if df_5m is None or df_5m.empty:
            return
        try:
            orb_bars = df_5m.iloc[:3]
            self._orb[symbol] = {
                'high': float(orb_bars['High'].max()),
                'low': float(orb_bars['Low'].min()),
                'avg_vol': float(orb_bars['Volume'].mean()),
            }
        except Exception:
            pass

    def get_score(self, symbol: str, current_price: float,
                  current_vol: float, smc_bias: str) -> Tuple[int, List[str]]:
        """Returns (score, signals)."""
        orb = self._orb.get(symbol)
        if not orb:
            return 0, []
        score = 0
        signals = []
        avg_vol = orb.get('avg_vol', 0)
        vol_confirmed = avg_vol > 0 and current_vol > avg_vol * 1.5

        if current_price > orb['high']:
            if smc_bias == 'bullish':
                score += 15
                signals.append("ORB突破↑(+15)")
                if vol_confirmed:
                    score += 5
                    signals.append("ORB量確認(+5)")
            else:
                score -= 15
                signals.append("ORB突破↑但SMC空(-15)")
        elif current_price < orb['low']:
            if smc_bias == 'bearish':
                score += 15
                signals.append("ORB突破↓(+15)")
                if vol_confirmed:
                    score += 5
                    signals.append("ORB量確認(+5)")
            else:
                score -= 15
                signals.append("ORB突破↓但SMC多(-15)")
        return score, signals


# ============================================================================
# Heartbeat
# ============================================================================
_last_heartbeat: float = 0.0


def write_heartbeat(positions_count: int = 0) -> None:
    global _last_heartbeat
    now = time.time()
    if now - _last_heartbeat < CONFIG['HEARTBEAT_INTERVAL_SEC']:
        return
    try:
        HEARTBEAT_FILE.write_text(
            json.dumps({
                'ts': datetime.now(TW_TZ).isoformat(),
                'pid': os.getpid(),
                'positions': positions_count,
                'market': 'US',
                'et_time': _et_now().strftime('%H:%M:%S ET'),
            })
        )
        _last_heartbeat = now
    except Exception:
        pass


# ============================================================================
# Main Bot Class
# ============================================================================
class USDaytradeBot:

    def __init__(self):
        self.notifier = TelegramNotifier(CONFIG['NOTIFY_CHAT_ID'])
        self.risk = RiskManager(
            capital=CONFIG['CAPITAL'],
            max_daily_loss_pct=CONFIG['MAX_DAILY_LOSS_PCT'],
            max_positions=CONFIG['MAX_POSITIONS'],
            kelly_fraction=CONFIG['KELLY_FRACTION'],
            max_single_risk_pct=CONFIG['MAX_SINGLE_RISK_PCT'],
            consecutive_loss_limit=CONFIG['CONSECUTIVE_LOSS_LIMIT'],
            re_entry_cooldown_sec=CONFIG['RE_ENTRY_COOLDOWN_SEC'],
        )
        self.recorder = TradeRecorder(
            'us', DATA_DIR,
            session_date_fn=_session_date_str,
            session_start_fn=_session_start_dt,
        )
        self.smc = SMCEngine()
        self.orb = ORBTracker()
        self.positions: Dict[str, Dict] = self.recorder.load_positions()
        self._shutdown = False
        self._entry_times: Dict[str, float] = {}
        self._last_stop_loss_ts: float = 0.0  # 全域連續停損冷卻用
        self._day_stop_notified: bool = False
        self._orb_updated: bool = False

        # Update capital from Alpaca account if possible
        self._sync_capital()
        self._guard_broker_position_sync()
        self._restore_risk_state()

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _sync_capital(self) -> None:
        account = alpaca_get_account()
        if account:
            equity = float(account.get('equity', CONFIG['CAPITAL']))
            self.risk.capital = equity
            self.risk.current_balance = equity
            self.risk.max_daily_loss = equity * CONFIG['MAX_DAILY_LOSS_PCT']
            logger.info(f"Alpaca 帳戶資金: ${equity:,.2f}")

    def _restore_risk_state(self) -> None:
        """
        重啟後從當日交易檔重建風控狀態。

        沒有這段的話，watchdog 把 bot 拉起來時 daily_pnl / 連虧計數 / 停損 ban
        會全部歸零，等於重啟一次就洗掉一次風控。
        """
        closed = [
            t for t in self.recorder.get_today_trades()
            if t.get('status') == 'closed'
        ]
        if not closed:
            return

        closed.sort(key=lambda t: t.get('exit_time') or '')
        balance_before = self.risk.current_balance

        for t in closed:
            pnl = float(t.get('pnl') or 0)
            reason = str(t.get('exit_reason') or '')
            is_stop_exit = 'stop' in reason.lower() or '(sl)' in reason.lower()
            is_stop_loss = is_stop_exit and pnl < 0
            self.risk.record_trade(pnl, symbol=t.get('symbol'), is_stop_loss=is_stop_loss)

            if is_stop_loss:
                ts = self._parse_trade_time(t.get('exit_time'))
                if ts > self._last_stop_loss_ts:
                    self._last_stop_loss_ts = ts

        # current_balance 已由 Alpaca equity 反映實現損益，避免重複扣一次
        self.risk.current_balance = balance_before

        logger.info(
            f"重啟恢復風控狀態: 已完成 {len(closed)} 筆 | "
            f"日 PnL=${self.risk.daily_pnl:+,.2f} | 連虧={self.risk.consecutive_losses} | "
            f"day_stopped={self.risk.day_stopped}"
        )

    @staticmethod
    def _parse_trade_time(value: Optional[str]) -> float:
        """交易檔時間字串（台灣時間）→ epoch 秒；失敗回 0。"""
        if not value:
            return 0.0
        try:
            dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=TW_TZ)
            return dt.timestamp()
        except Exception:
            return 0.0

    def _guard_broker_position_sync(self) -> None:
        broker_positions = alpaca_get_positions()
        broker_symbols = {p.get('symbol') for p in broker_positions if p.get('symbol')}
        local_symbols = set(self.positions.keys())

        if not broker_symbols:
            return
        if broker_symbols == local_symbols:
            logger.info(f"載入本地持倉並與 Alpaca 同步: {sorted(local_symbols)}")
            return

        msg = (
            "Alpaca 持倉與本地 recorder 不同步，拒絕啟動以避免 orphan positions: "
            f"broker={sorted(broker_symbols)}, local={sorted(local_symbols)}"
        )
        logger.critical(msg)
        self.notifier.on_error(msg, 'US')
        sys.exit(2)

    def _handle_signal(self, signum, frame):
        logger.info(f"收到信號 {signum}，開始優雅關閉...")
        self._shutdown = True

    def _graceful_shutdown(self):
        logger.info("優雅關閉：強制平倉所有持倉")
        self.force_close_all("Bot 關閉")
        self.notifier.on_shutdown('US', self.risk.trade_count, self.risk.daily_pnl)
        logger.info("US Bot 已安全關閉")

    # -------------------------------------------------------------------------
    # Screener
    # -------------------------------------------------------------------------
    def run_screener(self) -> List[Dict]:
        """Pre-market screening: score each watchlist symbol."""
        _ensure_deps()
        results = []
        for symbol in WATCHLIST:
            try:
                df_1h = get_kbars_us(symbol, interval='1h', period='5d')
                if df_1h.empty or len(df_1h) < 10:
                    continue
                price = float(df_1h['Close'].iloc[-1])
                avg_vol = float(df_1h['Volume'].tail(20).mean())
                results.append({
                    'symbol': symbol,
                    'price': price,
                    'avg_vol': avg_vol,
                })
            except Exception:
                pass
        results.sort(key=lambda x: x['avg_vol'], reverse=True)
        return results

    # -------------------------------------------------------------------------
    # Entry analysis
    # -------------------------------------------------------------------------
    def _analyze_entry(self, symbol: str) -> Optional[Dict]:
        """Analyze symbol for entry. Returns entry dict or None."""
        _ensure_deps()
        try:
            df_5m = get_kbars_us(symbol, interval='5m', period='5d')
            df_15m = get_kbars_us(symbol, interval='15m', period='5d')
            df_1h = get_kbars_us(symbol, interval='1h', period='1mo')

            if df_5m.empty or len(df_5m) < 20:
                return None

            current_price, quote_age = get_quote_with_age(symbol)
            if current_price <= 0:
                current_price = float(df_5m['Close'].iloc[-1])

            # Stale quote check (v2 fix)
            if quote_age > CONFIG['STALE_QUOTE_SEC']:
                logger.warning(f"{symbol}: 報價過期 ({quote_age:.0f}s)，跳過")
                return None

            atr = calculate_atr(df_5m)
            vwap_data = VWAPCalculator.calculate(df_5m)
            kill_zone = get_kill_zone_us()

            result = self.smc.analyze(
                df_5m=df_5m,
                df_15m=df_15m if not df_15m.empty else None,
                df_1h=df_1h if not df_1h.empty else None,
                vwap_data=vwap_data,
                kill_zone=kill_zone,
                atr=atr,
            )

            if result.rejected:
                logger.warning(f"{symbol}: {result.reject_reason}")
                return None

            # ORB score
            current_vol = float(df_5m['Volume'].iloc[-1])
            orb_score, orb_signals = self.orb.get_score(
                symbol, current_price, current_vol, result.bias
            )
            result.score = max(0, min(100, result.score + orb_score))
            result.signals.extend(orb_signals)

            # Score threshold
            if is_opening_grace_period():
                min_score = 90
            elif kill_zone == 'kz1_open':
                min_score = CONFIG['KZ1_MIN_SCORE']
            else:
                min_score = CONFIG['MIN_SCORE']

            if result.score < min_score:
                logger.debug(f"{symbol}: 評分 {result.score} < {min_score}，跳過")
                return None

            # 本 bot 只做多（Alpaca 下單固定 buy，停損/停利皆以多單計算）。
            # 因此 bias 不是 bullish 就一律拒絕，避免用 long 去打 bearish setup。
            if result.bias != 'bullish':
                logger.info(
                    f"{symbol}: 拒絕 — bias={result.bias}，本 bot 只做多 "
                    f"(Score={result.score})"
                )
                return None

            stop_price = current_price - atr * CONFIG['ATR_STOP_MULT']
            take_profit = current_price + atr * CONFIG['ATR_TP_MULT']

            qty = self.risk.calculate_position_size_usd(
                entry_price=current_price,
                stop_price=stop_price,
                smc_score=result.score,
            )
            if qty <= 0:
                logger.warning(f"{symbol}: 部位計算為 0，跳過")
                return None

            return {
                'symbol': symbol,
                'price': current_price,
                'qty': qty,
                'score': result.score,
                'bias': result.bias,
                'signals': result.signals,
                'stop_price': stop_price,
                'take_profit': take_profit,
                'atr': atr,
                'kill_zone': kill_zone,
            }
        except Exception as e:
            logger.error(f"{symbol} 分析失敗: {e}")
            return None

    def _enter_position(self, entry: Dict) -> bool:
        symbol = entry['symbol']

        ok, reason = self.risk.can_open_position(len(self.positions))
        if not ok:
            logger.info(f"{symbol}: 跳過進場 — {reason}")
            return False

        last_entry = self._entry_times.get(symbol, 0)
        if time.time() - last_entry < CONFIG['ENTRY_COOLDOWN_SEC']:
            logger.debug(f"{symbol}: 進場冷卻中")
            return False

        # 全域停損冷卻：任一標的剛停損，短時間內不換股再追
        since_stop = time.time() - self._last_stop_loss_ts
        if self._last_stop_loss_ts > 0 and since_stop < CONFIG['GLOBAL_STOP_COOLDOWN_SEC']:
            remaining = int(CONFIG['GLOBAL_STOP_COOLDOWN_SEC'] - since_stop)
            logger.info(f"{symbol}: 跳過進場 — 全域停損冷卻中({remaining}s)")
            return False

        order = alpaca_place_order(symbol, entry['qty'], 'buy')
        if not order:
            logger.warning(f"{symbol}: 下單失敗")
            return False

        order_id = order.get('id', '')
        filled_price, filled_qty = alpaca_poll_fill(order_id, timeout_sec=30)
        if filled_price is None or filled_qty <= 0:
            logger.warning(f"{symbol}: 等待成交超時")
            return False

        if filled_qty != entry['qty']:
            logger.warning(f"{symbol}: 實際成交股數 {filled_qty} != 預期 {entry['qty']}")
            entry = dict(entry)
            entry['qty'] = filled_qty

        # Stale fill check
        if abs(filled_price - entry['price']) / entry['price'] > 0.005:
            logger.warning(f"{symbol}: 成交價異常 {filled_price} vs 預期 {entry['price']:.2f}")

        trade_id = self.recorder.on_entry(
            symbol=symbol,
            entry_price=filled_price,
            qty=entry['qty'],
            score=entry['score'],
            signals=entry['signals'],
            stop_price=entry['stop_price'],
            take_profit=entry['take_profit'],
        )

        self.positions[symbol] = {
            'trade_id': trade_id,
            'entry_price': filled_price,
            'qty': entry['qty'],
            'stop_price': entry['stop_price'],
            'take_profit': entry['take_profit'],
            'atr': entry['atr'],
            'highest_price': filled_price,
            'entry_time': time.time(),
        }
        self._entry_times[symbol] = time.time()
        self.recorder.update_positions(self.positions)

        self.notifier.on_entry(
            symbol=symbol,
            price=filled_price,
            qty=entry['qty'],
            score=entry['score'],
            signals=entry['signals'],
            stop=entry['stop_price'],
            tp=entry['take_profit'],
            market='US',
        )
        logger.info(
            f"✅ 進場: {symbol} @ ${filled_price:.2f} × {entry['qty']}股 "
            f"| Score={entry['score']} | 停損=${entry['stop_price']:.2f}"
        )
        return True

    def _exit_position(self, symbol: str, reason: str) -> None:
        pos = self.positions.get(symbol)
        if not pos:
            return

        # 嘗試透過 Alpaca API 平倉
        result = alpaca_close_position(symbol)
        if result is None:
            # DELETE 失敗 → 嘗試市價賣出
            logger.warning(f"{symbol} DELETE close 失敗，嘗試市價賣出")
            sell_result = _alpaca_request('POST', '/v2/orders', body={
                'symbol': symbol,
                'qty': str(pos['qty']),
                'side': 'sell',
                'type': 'market',
                'time_in_force': 'ioc',
            })
            if sell_result:
                logger.info(f"{symbol} 市價賣出成功: {sell_result.get('id', '?')}")

        # 取得出場價格
        current_price, _ = get_quote_with_age(symbol)
        if current_price <= 0:
            current_price = pos['entry_price']

        pnl = (current_price - pos['entry_price']) * pos['qty']
        # 移動停損可能在獲利區出場；只有實際虧損的停損才算停損（觸發 ban / 全域冷卻）
        is_stop_exit = 'stop' in reason.lower() or '(sl)' in reason.lower()
        is_stop_loss = is_stop_exit and pnl < 0
        if is_stop_loss:
            self._last_stop_loss_ts = time.time()

        trade_id = pos.get('trade_id', f"{symbol}_unknown")
        self.recorder.on_exit(trade_id, current_price, pnl, reason)
        self.risk.record_trade(pnl, symbol=symbol, is_stop_loss=is_stop_loss)

        # Super circuit breaker
        if (abs(self.risk.daily_pnl) >= CONFIG['MAX_DAILY_LOSS_USD']
                or self.risk.daily_pnl <= -self.risk.capital * CONFIG['MAX_DAILY_LOSS_PCT']):
            if not self.risk.day_stopped:
                self.risk.day_stopped = True
                self.risk.day_stop_reason = (
                    f"Super Circuit Breaker: 日虧 ${abs(self.risk.daily_pnl):,.0f}"
                )

        if self.risk.day_stopped and not self._day_stop_notified:
            self.notifier.on_day_stop('US', self.risk.day_stop_reason)
            self._day_stop_notified = True

        self.notifier.on_exit(
            symbol=symbol,
            entry_price=pos['entry_price'],
            exit_price=current_price,
            qty=pos['qty'],
            pnl=pnl,
            reason=reason,
            market='US',
        )
        logger.info(
            f"{'✅' if pnl >= 0 else '❌'} 出場: {symbol} @ ${current_price:.2f} "
            f"| PnL=${pnl:+,.2f} | {reason}"
        )

        del self.positions[symbol]
        self.recorder.update_positions(self.positions)

    def force_close_all(self, reason: str = "強制平倉") -> None:
        if not self.positions:
            return
        logger.warning(f"US 強制平倉 ({len(self.positions)} 個): {reason}")
        for symbol in list(self.positions.keys()):
            try:
                self._exit_position(symbol, reason)
            except Exception as e:
                logger.error(f"{symbol} 強制平倉失敗: {e}")
                self.notifier.on_error(f"US {symbol} 強制平倉失敗: {e}", 'US')

    # -------------------------------------------------------------------------
    # Monitor positions
    # -------------------------------------------------------------------------
    def _monitor_positions(self) -> None:
        for symbol in list(self.positions.keys()):
            try:
                pos = self.positions[symbol]
                current_price, age = get_quote_with_age(symbol)
                if current_price <= 0 or age > 60:
                    continue

                # Trailing stop
                highest = pos.get('highest_price', pos['entry_price'])
                if current_price > highest:
                    pos['highest_price'] = current_price
                    new_stop = current_price - pos.get('atr', 1.0) * CONFIG['ATR_STOP_MULT']
                    if new_stop > pos.get('stop_price', 0):
                        pos['stop_price'] = new_stop
                        logger.debug(f"{symbol} 移動停損: ${new_stop:.2f}")

                if current_price <= pos.get('stop_price', 0):
                    self._exit_position(symbol, '停損(SL)')
                elif current_price >= pos.get('take_profit', float('inf')):
                    self._exit_position(symbol, '獲利了結(TP)')
            except Exception as e:
                logger.error(f"{symbol} 監控持倉失敗: {e}")
                continue

    # -------------------------------------------------------------------------
    # Main run loop
    # -------------------------------------------------------------------------
    def run(self) -> None:
        """Main daemon mode — NEVER crashes permanently."""
        restart_count = 0
        while not self._shutdown:
            try:
                restart_count += 1
                if restart_count > 1:
                    wait = min(60, restart_count * 5)
                    logger.warning(f"US Bot 重啟中（第{restart_count}次），等待 {wait}s...")
                    self.notifier.on_restart('US', '異常重啟', restart_count)
                    time.sleep(wait)

                self._run_loop()

                if self._shutdown:
                    break

                logger.info("US 交易日結束，Bot 停止")
                break

            except KeyboardInterrupt:
                logger.info("收到 Ctrl+C")
                self._shutdown = True
            except Exception as e:
                logger.error(f"US 主迴圈崩潰: {e}\n{traceback.format_exc()}")
                self.notifier.on_error(f"US 主迴圈崩潰: {e}", 'US')
                if self._shutdown:
                    break

        self._graceful_shutdown()

    def _run_loop(self) -> None:
        """Inner trading loop. Returns when market closes."""
        _ensure_deps()
        et = _et_now()
        logger.info("=" * 60)
        logger.info(f"US Bot v{__version__} 啟動 — {_today_str()} ({et.strftime('%H:%M ET')})")
        logger.info(f"監控標的: {WATCHLIST}")
        logger.info("=" * 60)

        screener_results = self.run_screener()
        logger.info(f"盤前篩選: {len(screener_results)} 支")
        for r in screener_results[:5]:
            logger.info(f"  {r['symbol']}: ${r['price']:.2f} vol={r['avg_vol']:,.0f}")

        self.notifier.on_startup('US', len(WATCHLIST), self.risk.capital)

        last_scan = 0.0
        last_monitor = 0.0
        last_orb_update = 0.0

        while not self._shutdown:
            write_heartbeat(len(self.positions))

            if is_force_close_time_us():
                if self.positions:
                    logger.warning("⏰ 15:50 ET 強制平倉")
                    self.force_close_all("收盤強制平倉")
                self.notifier.on_shutdown('US', self.risk.trade_count, self.risk.daily_pnl)
                logger.info("🏁 US 收盤，Bot 停止")
                return

            if self.risk.day_stopped:
                if self.positions:
                    self.force_close_all(self.risk.day_stop_reason)
                time.sleep(30)
                continue

            now_ts = time.time()

            # Update ORB (once after 09:45 ET)
            et_now = _et_now()
            if (not self._orb_updated
                    and et_now.time() >= dt_time(9, 46)
                    and et_now.time() <= dt_time(10, 30)):
                for symbol in WATCHLIST:
                    df_5m = get_kbars_us(symbol, interval='5m', period='1d')
                    self.orb.update(symbol, df_5m if not df_5m.empty else None)
                self._orb_updated = True
                logger.info("ORB 已更新")

            if self.positions and (now_ts - last_monitor >= CONFIG['MONITOR_INTERVAL_SEC']):
                try:
                    self._monitor_positions()
                except Exception as e:
                    logger.error(f"監控持倉失敗: {e}")
                last_monitor = now_ts

            if (
                not is_no_new_trade_time_us()
                and is_market_open_us()
                and get_kill_zone_us() is not None
                and len(self.positions) < CONFIG['MAX_POSITIONS']
                and (now_ts - last_scan >= CONFIG['SCAN_INTERVAL_SEC'])
            ):
                for symbol in WATCHLIST:
                    if symbol in self.positions:
                        continue
                    ok, reason = self.risk.can_trade(symbol)
                    if not ok:
                        continue
                    entry = self._analyze_entry(symbol)
                    if entry:
                        self._enter_position(entry)
                        if len(self.positions) >= CONFIG['MAX_POSITIONS']:
                            break
                last_scan = now_ts

            time.sleep(5)


# ============================================================================
# Subcommands
# ============================================================================
def cmd_screener():
    _ensure_deps()
    bot = USDaytradeBot.__new__(USDaytradeBot)
    bot.notifier = TelegramNotifier(CONFIG['NOTIFY_CHAT_ID'])
    bot.risk = RiskManager(capital=CONFIG['CAPITAL'])
    bot.smc = SMCEngine()
    bot.orb = ORBTracker()
    results = bot.run_screener()
    print(f"\n美股盤前篩選結果 ({_today_str()}, ET: {_et_now().strftime('%H:%M')}):")
    for r in results:
        print(f"  ✓ {r['symbol']:6s} ${r['price']:8.2f}  vol={r['avg_vol']:,.0f}")
    print(f"\n共 {len(results)} 支")


def cmd_log():
    recorder = TradeRecorder(
        'us', DATA_DIR,
        session_date_fn=_session_date_str,
        session_start_fn=_session_start_dt,
    )
    recorder.print_summary()


def cmd_status():
    recorder = TradeRecorder(
        'us', DATA_DIR,
        session_date_fn=_session_date_str,
        session_start_fn=_session_start_dt,
    )
    positions = recorder.load_positions()
    print(f"\n美股持倉狀態 ({_today_str()}, ET: {_et_now().strftime('%H:%M')}):")
    if not positions:
        print("  目前無持倉")
    else:
        for sym, pos in positions.items():
            price, _ = get_quote_with_age(sym)
            pnl = (price - pos.get('entry_price', 0)) * pos.get('qty', 0)
            print(f"  {sym}: 進場=${pos.get('entry_price', 0):.2f} 現價=${price:.2f} PnL=${pnl:+,.2f}")
    recorder.print_summary()


# ============================================================================
# Entry point
# ============================================================================
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'run'

    if cmd == 'screener':
        cmd_screener()
    elif cmd == 'log':
        cmd_log()
    elif cmd == 'status':
        cmd_status()
    elif cmd == 'run':
        bot = USDaytradeBot()
        bot.run()
    else:
        print(f"用法: {sys.argv[0]} [run|screener|log|status]")
        sys.exit(1)


if __name__ == '__main__':
    main()
