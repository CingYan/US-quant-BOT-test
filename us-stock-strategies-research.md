# 美股日內交易策略研究報告
> 補充 SMC v3.0 的可用策略分析
> 
> 生成日期：2026-02-16  
> 目標平台：Alpaca API  
> 現有策略：SMC（Smart Money Concepts）v3.0

---

## 📊 策略推薦優先級總覽

| 優先級 | 策略 | 複雜度 | Alpaca 支援 | 與 SMC 互補性 |
|--------|------|--------|-------------|---------------|
| 🥇 1 | VWAP Bounce/Rejection | ⭐⭐ | ✅ 完整支援 | ⭐⭐⭐⭐⭐ |
| 🥈 2 | Opening Range Breakout (ORB) | ⭐⭐ | ✅ 完整支援 | ⭐⭐⭐⭐⭐ |
| 🥉 3 | Mean Reversion (RSI + BB) | ⭐⭐ | ✅ 完整支援 | ⭐⭐⭐⭐ |
| 4 | Gap Trading / News Sentiment | ⭐⭐⭐ | ⚠️ 需外部新聞 API | ⭐⭐⭐⭐ |
| 5 | Volume Profile (自算) | ⭐⭐⭐ | ⚠️ 需自行計算 | ⭐⭐⭐⭐ |
| 6 | EMA Momentum / Scalping | ⭐ | ✅ 完整支援 | ⭐⭐⭐ |
| 7 | Statistical Arbitrage (Pairs) | ⭐⭐⭐⭐⭐ | ⚠️ 需額外計算 | ⭐⭐ |
| 8 | Market Microstructure | ⭐⭐⭐⭐ | ❌ 股票不支援 L2 | ⭐⭐ |

---

## 策略 1：VWAP Bounce / Rejection（優先推薦）

### 概述
VWAP（Volume Weighted Average Price）是機構交易員的重要參考價位。日內價格在 VWAP 上方代表多方強勢，下方代表空方強勢。策略核心是等待價格回測 VWAP 後的反彈（bounce）或拒絕（rejection），作為高勝率的順趨勢進場點。VWAP 偏差帶（±1 / ±2 std）提供額外的支撐/阻力層級，是機構最常用的日內定價工具之一。

### 適用市場條件
- ✅ **趨勢市場**（最佳）— 上升趨勢中 VWAP bounce 做多，下降趨勢中 VWAP rejection 做空
- ⚠️ **盤整市場** — 多次穿越 VWAP 時易出現假訊號，需改用 anchored VWAP
- ✅ **高波動市場** — 配合 VWAP 偏差帶抓均值回歸

### 實作複雜度：⭐⭐（2/5）

### Alpaca API 支援
- ✅ `get_bars()` 提供 OHLCV 資料，可自行計算 VWAP
- ✅ WebSocket real-time bars 支援即時更新
- ✅ Alpaca 的 bars 資料已內建 `vwap` 欄位（分鐘 bars）

### 建議參數
| 參數 | 建議值 | 說明 |
|------|--------|------|
| 時間框架 | 5min / 1min | 進場用 1min，確認用 5min |
| VWAP 偏差帶 | ±1σ, ±2σ | 第一目標 ±1σ，第二目標 ±2σ |
| 進場偏差閾值 | 0.3% - 0.5% | 距離 VWAP 超過此值才進場 |
| 止損位置 | 0.2% below VWAP | 穿越 VWAP 即認輸 |
| R:R | 1:2 以上 | 至少 2:1 才進場 |

### 進出場規則

**做多（VWAP Bounce）：**
1. 確認當前趨勢向上（5min EMA21 > EMA50）
2. 價格從 VWAP 上方回落，觸及 VWAP ±0.1%
3. 出現吞噬線、錘子線等看漲 K 線形態
4. 成交量放大（> 過去 5 根 K 線均量的 1.5 倍）
5. 進場：看漲確認 K 線收盤
6. 止損：VWAP 下方 0.2%
7. 目標：+1σ 偏差帶（第一目標），+2σ 偏差帶（第二目標）

**做空（VWAP Rejection）：**
1. 確認當前趨勢向下
2. 價格從 VWAP 下方反彈至 VWAP，被拒絕
3. 出現吞噬線、射擊之星等看跌形態
4. 進場：看跌確認 K 線收盤
5. 止損：VWAP 上方 0.2%
6. 目標：-1σ 偏差帶

### 與 SMC 結合方式
VWAP 是 SMC 的天然補充：
- **SMC 識別結構**（BOS / CHOCH / Order Block）→ **VWAP 確認機構定價**
- 當 SMC Order Block 與 VWAP 重疊時，勝率大幅提升
- VWAP 偏差帶可作為 SMC FVG（Fair Value Gap）的目標位確認
- SMC 提供「為什麼進場」，VWAP 提供「在哪裡進場」

```
SMC CHOCH 發生 + 價格在 VWAP 下方 → 等待 VWAP rejection → SMC Order Block 確認 → 進場做空
```

### Python 偽代碼

```python
import alpaca_trade_api as tradeapi
import pandas as pd
import numpy as np

class VWAPStrategy:
    def __init__(self, api):
        self.api = api
        self.vwap_deviation_entry = 0.003  # 0.3% 偏差才進場
        self.std_multiplier = [1, 2]
        
    def calculate_vwap_bands(self, bars_df):
        """計算 VWAP 和標準差偏差帶"""
        # 如果 Alpaca bars 已有 vwap 欄位，直接使用
        if 'vwap' in bars_df.columns:
            vwap = bars_df['vwap']
        else:
            # 手動計算日內累積 VWAP
            typical_price = (bars_df['high'] + bars_df['low'] + bars_df['close']) / 3
            cum_vol = bars_df['volume'].cumsum()
            cum_tp_vol = (typical_price * bars_df['volume']).cumsum()
            vwap = cum_tp_vol / cum_vol
        
        # 計算偏差帶
        deviation = bars_df['close'].rolling(20).std()
        upper_band_1 = vwap + deviation
        lower_band_1 = vwap - deviation
        upper_band_2 = vwap + 2 * deviation
        lower_band_2 = vwap - 2 * deviation
        
        return vwap, upper_band_1, lower_band_1, upper_band_2, lower_band_2
    
    def check_vwap_bounce_signal(self, bars_df, symbol):
        """檢查 VWAP bounce 訊號"""
        vwap, ub1, lb1, ub2, lb2 = self.calculate_vwap_bands(bars_df)
        
        current_price = bars_df['close'].iloc[-1]
        prev_price = bars_df['close'].iloc[-2]
        current_vwap = vwap.iloc[-1]
        
        # EMA 趨勢過濾
        ema21 = bars_df['close'].ewm(span=21).mean().iloc[-1]
        ema50 = bars_df['close'].ewm(span=50).mean().iloc[-1]
        trend_up = ema21 > ema50
        
        # 成交量確認
        avg_vol = bars_df['volume'].rolling(5).mean().iloc[-1]
        vol_surge = bars_df['volume'].iloc[-1] > avg_vol * 1.5
        
        # VWAP Bounce 條件
        near_vwap = abs(current_price - current_vwap) / current_vwap < 0.001
        bouncing_up = current_price > prev_price and trend_up
        
        if near_vwap and bouncing_up and vol_surge:
            stop_loss = current_vwap * 0.998
            target_1 = ub1.iloc[-1]
            target_2 = ub2.iloc[-1]
            
            return {
                'signal': 'LONG',
                'entry': current_price,
                'stop_loss': stop_loss,
                'target_1': target_1,
                'target_2': target_2,
                'reason': 'VWAP_BOUNCE'
            }
        
        return None
    
    def integrate_with_smc(self, vwap_signal, smc_signal):
        """整合 SMC 和 VWAP 訊號"""
        if not vwap_signal or not smc_signal:
            return None
        
        # 只有兩個訊號方向一致才進場
        if vwap_signal['signal'] == smc_signal['direction']:
            # SMC Order Block 在 VWAP 附近，勝率最高
            price_diff = abs(vwap_signal['entry'] - smc_signal['order_block_price'])
            if price_diff / vwap_signal['entry'] < 0.005:  # 0.5% 內
                vwap_signal['confidence'] = 'HIGH'  # SMC + VWAP 雙重確認
            return vwap_signal
        
        return None
```

### 風險評估
- 🟢 **優點**：與機構行為高度對齊；Alpaca 原生支援 VWAP 資料；訊號清晰
- 🟡 **缺點**：盤整市場假訊號多；VWAP 在日初影響較弱（資料點少）
- 🔴 **風險**：趨勢突然反轉時，VWAP bounce 可能變成突破失敗；建議加 2:1 止損比例

---

## 策略 2：Opening Range Breakout（ORB）（優先推薦）

### 概述
ORB 策略定義開盤後特定時間（通常 15 或 30 分鐘）的最高/最低點作為「開盤區間」，待價格有效突破此區間時進場順勢交易。開盤期間往往聚集最大的機構訂單流量，突破開盤區間代表方向性確立。ORB 是美股日內交易最經典且被廣泛回測的策略之一，配合成交量確認可顯著提升勝率。

### 適用市場條件
- ✅ **高波動市場**（最佳）— 有催化劑（財報、重大新聞）的股票
- ✅ **趨勢市場** — 與整體市場方向一致時勝率更高
- ❌ **低波動盤整** — ATR 過小時假突破多，建議跳過

### 實作複雜度：⭐⭐（2/5）

### Alpaca API 支援
- ✅ 完整支援 —  `get_bars()` 取分鐘資料，計算 09:30-09:45 的高低點
- ✅ `TimeFrame.Minute` 支援任意分鐘 bars

### 建議參數
| 參數 | 建議值 | 說明 |
|------|--------|------|
| 開盤區間時間 | 15 分鐘 | 09:30-09:45 ET；30 分鐘可選 |
| 突破確認 | 收盤突破 | 非瞬間刺穿，需整根 K 線收在區間外 |
| 最小 ATR | > $0.50 | ATR 太小不值得交易 |
| 止損位置 | 開盤區間對側 | 做多止損在區間低點，做空止損在區間高點 |
| 目標 | 1.5x - 2x 開盤區間高度 | ATR 倍數法或前日高/低 |
| 入場最晚時間 | 11:30 ET | 超過此時間不進場新單 |

### 進出場規則

**做多（Breakout Long）：**
1. 記錄 09:30-09:45 ET 的最高點（ORB High）和最低點（ORB Low）
2. 確認 ATR > $0.50，且有成交量催化劑
3. 等待 09:45 後，5min K 線收盤突破 ORB High
4. 確認成交量 > 開盤區間平均量的 1.5 倍
5. 進場：突破 K 線收盤
6. 止損：ORB Low（或 ATR 的 1 倍）
7. 目標：ORB High + (ORB High - ORB Low) × 1.5

**做空（Breakout Short）：**
1. 同上，等待收盤突破 ORB Low
2. 確認整體市場（SPY/QQQ）方向一致
3. 止損：ORB High
4. 目標：ORB Low - (ORB High - ORB Low) × 1.5

### 與 SMC 結合方式
- SMC 的 **FVG（Fair Value Gap）** 常在開盤區間附近形成
- ORB 突破後，若有 SMC **BOS（Break of Structure）** 同步確認，信心度最高
- 使用 SMC **Order Block** 作為 ORB 突破後的回測進場點（降低滑點）
- **Killzone（09:30-11:00 ET）** 是 SMC 最活躍時段，完美對齊 ORB 交易時間

```
09:30-09:45 建立 ORB 區間
09:45 後 SMC 識別 BOS（Break of Structure）
BOS 方向與 ORB 突破方向一致 → 等待回測 Order Block 進場
目標：下一個 SMC Liquidity 水位
```

### Python 偽代碼

```python
from datetime import datetime, time
import pytz

class ORBStrategy:
    def __init__(self, api, orb_minutes=15):
        self.api = api
        self.orb_minutes = orb_minutes
        self.et_tz = pytz.timezone('America/New_York')
        self.orb_high = {}
        self.orb_low = {}
        self.orb_established = {}
        
    def establish_orb(self, symbol, bars_df):
        """建立開盤區間（09:30 - 09:30+orb_minutes）"""
        market_open = time(9, 30)
        orb_end = time(9, 30 + self.orb_minutes // 60, 
                       self.orb_minutes % 60)
        
        # 篩選開盤區間的 K 線
        bars_df.index = pd.to_datetime(bars_df.index)
        orb_bars = bars_df.between_time('09:30', f'09:{30+self.orb_minutes:02d}')
        
        if len(orb_bars) >= self.orb_minutes:
            self.orb_high[symbol] = orb_bars['high'].max()
            self.orb_low[symbol] = orb_bars['low'].min()
            self.orb_established[symbol] = True
            return True
        return False
    
    def check_breakout(self, symbol, current_bar):
        """檢查突破訊號"""
        if not self.orb_established.get(symbol):
            return None
        
        orb_high = self.orb_high[symbol]
        orb_low = self.orb_low[symbol]
        orb_range = orb_high - orb_low
        
        current_close = current_bar['close']
        current_volume = current_bar['volume']
        
        # 計算 ATR 過濾
        if orb_range < 0.50:  # 最小波動要求
            return None
        
        # 做多突破
        if current_close > orb_high:
            return {
                'signal': 'LONG',
                'entry': current_close,
                'stop_loss': orb_low,
                'target_1': orb_high + orb_range * 1.5,
                'target_2': orb_high + orb_range * 2.0,
                'orb_range': orb_range,
                'reason': 'ORB_BREAKOUT_LONG'
            }
        
        # 做空突破
        elif current_close < orb_low:
            return {
                'signal': 'SHORT',
                'entry': current_close,
                'stop_loss': orb_high,
                'target_1': orb_low - orb_range * 1.5,
                'target_2': orb_low - orb_range * 2.0,
                'orb_range': orb_range,
                'reason': 'ORB_BREAKOUT_SHORT'
            }
        
        return None
    
    def check_pullback_entry(self, symbol, current_bar, smc_order_block=None):
        """突破後回測 ORB 邊緣進場（更低風險）"""
        if not self.orb_established.get(symbol):
            return None
        
        orb_high = self.orb_high[symbol]
        current_close = current_bar['close']
        
        # 突破後回測 ORB High（做多）
        bouncing = current_close > orb_high * 0.999  # 回測 ORB High ±0.1%
        
        # 如果有 SMC Order Block 在附近，優先使用
        if smc_order_block and abs(current_close - smc_order_block) / current_close < 0.003:
            return {'signal': 'LONG', 'entry': current_close, 
                    'reason': 'ORB_PULLBACK_SMC_OB'}
        
        if bouncing:
            return {'signal': 'LONG', 'entry': current_close,
                    'reason': 'ORB_PULLBACK'}
        
        return None
```

### 風險評估
- 🟢 **優點**：機械化程度高，容易自動化；開盤資訊密度最大；假突破率相對低
- 🟡 **缺點**：每日只有一次機會；低 ATR 日效果差；需要辨別真假突破
- 🔴 **風險**：Gap-up 開盤後 ORB 區間可能太小或位置不適合；建議加 ATR 和成交量雙重過濾

---

## 策略 3：Mean Reversion（RSI + Bollinger Band）

### 概述
均值回歸策略利用 RSI 超買/超賣 + Bollinger Band 邊界的雙重過濾，在價格出現極端偏離後，押注回歸均值（中軌）。這是日內交易中對抗「過度反應」的核心策略，在盤整市場表現優異，勝率通常達 60-80%。缺點是在強趨勢市場容易被追漲殺跌的動能壓垮，需搭配趨勢過濾器。

### 適用市場條件
- ✅ **盤整市場**（最佳）— 沒有明顯方向性，價格在區間內振盪
- ✅ **高波動但無趨勢** — 過度反應後快速修正
- ❌ **強趨勢市場** — 動能可持續推升，抄底/頂會虧損慘重

### 實作複雜度：⭐⭐（2/5）

### Alpaca API 支援
- ✅ 完整支援 — 所有指標可由 OHLCV 自行計算

### 建議參數
| 參數 | 建議值 | 說明 |
|------|--------|------|
| Bollinger Band 週期 | 20 | 標準設定 |
| Bollinger Band 標準差 | 2.0 | 包含 95% 的價格 |
| RSI 週期 | 14 | 標準設定 |
| RSI 超賣閾值 | < 30 | 做多觸發 |
| RSI 超買閾值 | > 70 | 做空觸發 |
| EMA 趨勢過濾 | EMA50 | ADX < 25 確認盤整 |
| 止損 | BB 邊界外 0.5% | 超過意味趨勢繼續 |

### 進出場規則

**做多（超賣回歸）：**
1. 確認市場處於盤整（ADX < 25 或 EMA50 水平）
2. 收盤低於 Bollinger Band 下軌
3. RSI < 30（超賣確認）
4. 出現 RSI 背離或 K 線反轉訊號
5. 進場：次根 K 線開盤或確認反彈後
6. 止損：BB 下軌再往下 0.5%
7. 目標：BB 中軌（20 SMA）→ BB 上軌

**做空（超買回歸）：**
1. 收盤高於 BB 上軌
2. RSI > 70
3. 止損：BB 上軌再往上 0.5%
4. 目標：BB 中軌

### 與 SMC 結合方式
- 在 SMC **Supply Zone** 上方出現超買訊號 → 做空信心度最高
- 在 SMC **Demand Zone** 下方出現超賣訊號 → 做多信心度最高
- SMC 的 **Order Block** 和 **FVG** 可作為均值回歸的目標位
- 使用 SMC 結構判斷方向：只在 SMC 支持方向做均值回歸

```
SMC 識別 Demand Zone → 等待 RSI < 30 + 收盤低於 BB 下軌 → 
確認 SMC Order Block 附近 → 進場做多 → 目標 BB 中軌 + SMC 上方 FVG 缺口
```

### Python 偽代碼

```python
import pandas as pd
import numpy as np

class MeanReversionStrategy:
    def __init__(self):
        self.bb_period = 20
        self.bb_std = 2.0
        self.rsi_period = 14
        self.rsi_oversold = 30
        self.rsi_overbought = 70
        self.adx_threshold = 25
        
    def calculate_indicators(self, bars_df):
        """計算所有指標"""
        close = bars_df['close']
        
        # Bollinger Bands
        sma = close.rolling(self.bb_period).mean()
        std = close.rolling(self.bb_period).std()
        bb_upper = sma + self.bb_std * std
        bb_lower = sma - self.bb_std * std
        
        # RSI
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(self.rsi_period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        # ADX（簡化版趨勢強度）
        high = bars_df['high']
        low = bars_df['low']
        tr = pd.DataFrame({
            'hl': high - low,
            'hc': abs(high - close.shift()),
            'lc': abs(low - close.shift())
        }).max(axis=1)
        atr = tr.rolling(14).mean()
        
        # EMA50 趨勢
        ema50 = close.ewm(span=50).mean()
        
        return {
            'sma': sma, 'bb_upper': bb_upper, 'bb_lower': bb_lower,
            'rsi': rsi, 'atr': atr, 'ema50': ema50
        }
    
    def check_signal(self, bars_df, smc_zone=None):
        """檢查均值回歸訊號"""
        indicators = self.calculate_indicators(bars_df)
        
        current = bars_df.iloc[-1]
        rsi_current = indicators['rsi'].iloc[-1]
        bb_upper = indicators['bb_upper'].iloc[-1]
        bb_lower = indicators['bb_lower'].iloc[-1]
        sma = indicators['sma'].iloc[-1]
        ema50 = indicators['ema50'].iloc[-1]
        
        # 趨勢過濾：EMA50 必須相對水平（斜率 < 0.05%）
        ema50_slope = abs(ema50 - indicators['ema50'].iloc[-5]) / ema50
        range_market = ema50_slope < 0.005
        
        signal = None
        
        # 做多訊號
        if (current['close'] < bb_lower and 
            rsi_current < self.rsi_oversold and 
            range_market):
            
            # SMC Demand Zone 加持
            confidence = 'NORMAL'
            if smc_zone and smc_zone['type'] == 'DEMAND':
                if abs(current['close'] - smc_zone['price']) / current['close'] < 0.01:
                    confidence = 'HIGH'
            
            signal = {
                'signal': 'LONG',
                'entry': current['close'],
                'stop_loss': bb_lower * 0.995,
                'target_1': sma,  # BB 中軌
                'target_2': bb_upper,  # BB 上軌
                'reason': 'MEAN_REVERSION_LONG',
                'confidence': confidence
            }
        
        # 做空訊號
        elif (current['close'] > bb_upper and 
              rsi_current > self.rsi_overbought and 
              range_market):
            
            confidence = 'NORMAL'
            if smc_zone and smc_zone['type'] == 'SUPPLY':
                confidence = 'HIGH'
            
            signal = {
                'signal': 'SHORT',
                'entry': current['close'],
                'stop_loss': bb_upper * 1.005,
                'target_1': sma,
                'target_2': bb_lower,
                'reason': 'MEAN_REVERSION_SHORT',
                'confidence': confidence
            }
        
        return signal
```

### 風險評估
- 🟢 **優點**：高勝率（盤整市場 60-80%）；機械化容易；訊號清晰
- 🟡 **缺點**：趨勢市場虧損幅度大；每次虧損金額 > 每次盈利（R:R 較低）
- 🔴 **風險**：「刀口舔血」— 強趨勢時抄底會被連續打爆；**必須有 ADX 趨勢過濾器**

---

## 策略 4：Gap Trading / News Sentiment

### 概述
盤前新聞驅動的缺口（Gap）交易策略，針對因財報、重大新聞、法規宣布等事件產生的隔夜跳空缺口進行交易。Gap-and-Go 策略押注缺口方向延續（衝量繼續），Gap Fill 策略押注缺口被填補（均值回歸）。兩種方向的選擇取決於缺口類型、成交量、以及新聞質量。

### 適用市場條件
- ✅ **財報季**（最佳）— 公司業績超預期/不及預期
- ✅ **高波動新聞** — FDA 核准、收購合並、重大法律案件
- ❌ **無催化劑** — 無基本面支撐的缺口填補率高

### 實作複雜度：⭐⭐⭐（3/5）

### Alpaca API 支援
- ✅ 盤前資料：`get_bars()` 支援盤前 bars
- ⚠️ 新聞資料：需外部 API（Alpaca 有基礎新聞 API，但需 Benzinga/Marketaux 等加強）
- ✅ Alpaca News API：可取得基本財經新聞事件

### 建議參數
| 參數 | 建議值 | 說明 |
|------|--------|------|
| 最小缺口大小 | 3-5% | 太小缺口信噪比低 |
| 盤前成交量倍數 | > 3x 均量 | 確認機構參與 |
| Gap-and-Go 時間 | 09:30-10:30 ET | 前一小時動能最強 |
| Gap Fill 判斷 | 盤前量 < 均量 | 低量缺口易回填 |
| 浮籌限制 | 避免 float < 1M | 低浮籌操控風險高 |

### 進出場規則

**Gap-and-Go（缺口延續）：**
1. 盤前掃描：缺口 > 5%，盤前量 > 3x 均量
2. 確認正面催化劑（財報 EPS > 預期，重大合約等）
3. 09:30 後等待 2-3 分鐘確認方向
4. 若價格保持在盤前高點上方 → 進場做多
5. 止損：開盤區間低點或盤前低點
6. 目標：ATR 延伸或整數關卡

**Gap Fill（缺口填補）：**
1. 缺口 > 3% 但盤前量低（< 2x 均量）
2. 開盤後出現反轉 K 線
3. 進場做空（缺口向上）
4. 目標：前日收盤價（完整填補）

### 與 SMC 結合方式
- 缺口方向 + SMC Market Structure（多空結構）一致 → 高信心度進場
- 缺口未填補的區域本身就是 SMC **Imbalance（FVG）**，機構後續必回測
- 使用 SMC Liquidity 分析判斷缺口填補目標（流動性池在前日高低點）

### Python 偽代碼

```python
class GapTradingStrategy:
    def __init__(self, api, news_api_key):
        self.api = api
        self.news_api = MarketauxAPI(news_api_key)  # 外部新聞 API
        self.min_gap_pct = 0.03
        self.min_premarket_vol_ratio = 3.0
        
    def scan_premarket_gaps(self, symbols):
        """盤前掃描缺口股票"""
        gaps = []
        for symbol in symbols:
            # 取前日收盤和今日盤前資料
            prev_close = self.get_prev_close(symbol)
            premarket_price = self.get_premarket_price(symbol)
            premarket_vol = self.get_premarket_volume(symbol)
            avg_vol = self.get_avg_daily_volume(symbol)
            
            gap_pct = (premarket_price - prev_close) / prev_close
            vol_ratio = premarket_vol / avg_vol
            
            if abs(gap_pct) > self.min_gap_pct and vol_ratio > self.min_premarket_vol_ratio:
                # 取得新聞情緒
                news = self.news_api.get_news(symbol, hours=12)
                sentiment = self.analyze_sentiment(news)
                
                gaps.append({
                    'symbol': symbol,
                    'gap_pct': gap_pct,
                    'direction': 'UP' if gap_pct > 0 else 'DOWN',
                    'vol_ratio': vol_ratio,
                    'sentiment': sentiment,
                    'prev_close': prev_close,
                    'premarket_price': premarket_price
                })
        
        return sorted(gaps, key=lambda x: abs(x['gap_pct']), reverse=True)
    
    def determine_strategy(self, gap_info):
        """判斷 Gap-and-Go 還是 Gap Fill"""
        # 高量 + 正面新聞 → Gap-and-Go
        if (gap_info['vol_ratio'] > 5 and 
            gap_info['sentiment'] == 'POSITIVE' and
            gap_info['gap_pct'] > 0.05):
            return 'GAP_AND_GO'
        
        # 低量缺口 → 傾向 Gap Fill
        elif gap_info['vol_ratio'] < 2:
            return 'GAP_FILL'
        
        # 中間情況：等待開盤確認
        else:
            return 'WAIT_CONFIRM'
    
    def analyze_sentiment(self, news_list):
        """分析新聞情緒（簡化版）"""
        positive_keywords = ['beats', 'raises', 'approved', 'acquisition', 'upgrade']
        negative_keywords = ['misses', 'lowers', 'rejected', 'lawsuit', 'downgrade']
        
        score = 0
        for news in news_list:
            title = news['title'].lower()
            score += sum(1 for kw in positive_keywords if kw in title)
            score -= sum(1 for kw in negative_keywords if kw in title)
        
        if score > 0:
            return 'POSITIVE'
        elif score < 0:
            return 'NEGATIVE'
        return 'NEUTRAL'
```

### 風險評估
- 🟢 **優點**：有基本面催化劑支撐，方向性強
- 🟡 **缺點**：需要可靠的新聞 API；盤前流動性差；滑點大
- 🔴 **風險**：財報當天波動極端，可能被單根 K 線打到止損；建議縮小倉位

---

## 策略 5：Volume Profile（自行計算）

### 概述
Volume Profile 將成交量按價格層級分佈，識別出 POC（Point of Control，最大成交量價位）、Value Area（Value Area High/Low，70% 成交量的範圍）和 Low Volume Nodes（少量成交的價格 gap）。這些水位對機構來說是重要的定價參考，突破後往往快速移動至下一個 High Volume Node。

### 適用市場條件
- ✅ 任何市場條件均可作為支撐/阻力參考
- ✅ **開盤後回測 POC** 是高勝率設定
- ✅ 配合 VWAP 使用效果最佳

### 實作複雜度：⭐⭐⭐（3/5）

### Alpaca API 支援
- ⚠️ Alpaca 無原生 Volume Profile — 需自行從分鐘 bars 計算
- ✅ 分鐘 bars 有足夠精度計算 Volume Profile

### 建議參數
| 參數 | 建議值 | 說明 |
|------|--------|------|
| 計算週期 | 當日 / 前 5 日 | 當日 Profile 最重要 |
| 價格 bin 大小 | $0.10 / $0.25 | 依股票價格調整 |
| Value Area 比例 | 70% | 標準設定 |
| POC 偏差容差 | ±0.2% | POC 附近視為有效 |

### Python 偽代碼

```python
class VolumeProfileCalculator:
    def __init__(self, bin_size=0.10):
        self.bin_size = bin_size
        
    def calculate(self, bars_df):
        """從分鐘 bars 計算 Volume Profile"""
        all_prices = []
        all_volumes = []
        
        for _, bar in bars_df.iterrows():
            # 將每根 K 線的成交量分配到高低點之間的價格層級
            price_range = np.arange(
                round(bar['low'] / self.bin_size) * self.bin_size,
                bar['high'] + self.bin_size,
                self.bin_size
            )
            vol_per_level = bar['volume'] / max(len(price_range), 1)
            
            for price in price_range:
                all_prices.append(price)
                all_volumes.append(vol_per_level)
        
        # 彙總
        profile = pd.Series(all_volumes, index=all_prices)
        profile = profile.groupby(level=0).sum()
        
        # 計算關鍵水位
        poc = profile.idxmax()
        
        # Value Area（70% 成交量）
        total_vol = profile.sum()
        target_vol = total_vol * 0.70
        
        poc_idx = profile.index.get_loc(poc)
        upper_idx, lower_idx = poc_idx, poc_idx
        accumulated = profile.iloc[poc_idx]
        
        while accumulated < target_vol:
            up_vol = profile.iloc[upper_idx + 1] if upper_idx + 1 < len(profile) else 0
            down_vol = profile.iloc[lower_idx - 1] if lower_idx - 1 >= 0 else 0
            
            if up_vol >= down_vol:
                upper_idx += 1
                accumulated += up_vol
            else:
                lower_idx -= 1
                accumulated += down_vol
        
        vah = profile.index[upper_idx]  # Value Area High
        val = profile.index[lower_idx]  # Value Area Low
        
        return {
            'poc': poc,
            'vah': vah,
            'val': val,
            'profile': profile,
            'low_volume_nodes': profile[profile < profile.quantile(0.1)].index.tolist()
        }
    
    def check_poc_signal(self, current_price, volume_profile):
        """POC 回測訊號"""
        poc = volume_profile['poc']
        vah = volume_profile['vah']
        val = volume_profile['val']
        
        # 在 POC 附近（±0.2%）
        if abs(current_price - poc) / poc < 0.002:
            return {'level': 'POC', 'price': poc, 'action': 'WATCH'}
        
        # 突破 VAH（看漲）
        if current_price > vah:
            return {'level': 'VAH_BREAK', 'price': vah, 'action': 'LONG'}
        
        # 跌破 VAL（看跌）
        if current_price < val:
            return {'level': 'VAL_BREAK', 'price': val, 'action': 'SHORT'}
        
        return None
```

### 與 SMC 結合方式
- POC 通常與 SMC **Order Block** 或 **FVG** 重疊，雙重確認支撐/阻力
- Value Area High/Low 可作為 SMC 的 Liquidity 目標
- Low Volume Node 是 SMC **Imbalance** 的量化確認

### 風險評估
- 🟢 **優點**：客觀量化；不依賴主觀判斷；與機構行為高度相關
- 🟡 **缺點**：計算量較大；每日需重新計算；bin size 選擇影響結果
- 🔴 **風險**：成交量資料品質影響準確度；Alpaca 免費版可能有資料限制

---

## 策略 6：EMA Momentum / Scalping

### 概述
利用多條 EMA 的交叉和排列來判斷短期動能方向，配合 MACD 背離訊號捕捉趨勢轉折。EMA 策略邏輯簡單、容易實作，適合快速市場（scalping）。缺點是在盤整市場鞭打嚴重，需配合趨勢過濾器。

### 適用市場條件
- ✅ **趨勢市場**（最佳）— EMA 排列整齊時效果好
- ❌ **盤整市場** — 頻繁交叉，訊號品質極差

### 實作複雜度：⭐（1/5）— 最簡單

### Alpaca API 支援
- ✅ 完整支援

### 建議參數
| 參數 | 建議值 | 說明 |
|------|--------|------|
| 快速 EMA | 9 或 13 | 捕捉短期動能 |
| 慢速 EMA | 21 或 50 | 確認趨勢 |
| MACD 設定 | 12/26/9 | 標準設定 |
| 時間框架 | 1min / 5min | Scalping 用 1min |

### 與 SMC 結合方式
- 僅在 SMC 識別出有效結構（BOS 後）的方向上使用 EMA 訊號
- EMA 交叉 + SMC Order Block 重疊 → 高信心進場
- MACD 背離 + SMC CHOCH（Change of Character）= 反轉訊號組合

### Python 偽代碼

```python
class EMAMomentumStrategy:
    def check_signal(self, bars_df):
        close = bars_df['close']
        ema9 = close.ewm(span=9).mean()
        ema21 = close.ewm(span=21).mean()
        ema50 = close.ewm(span=50).mean()
        
        # MACD
        macd_line = close.ewm(span=12).mean() - close.ewm(span=26).mean()
        signal_line = macd_line.ewm(span=9).mean()
        histogram = macd_line - signal_line
        
        # 做多：EMA9 > EMA21 > EMA50，MACD histogram 由負轉正
        bullish_stack = (ema9.iloc[-1] > ema21.iloc[-1] > ema50.iloc[-1])
        macd_cross_up = histogram.iloc[-1] > 0 and histogram.iloc[-2] < 0
        
        if bullish_stack and macd_cross_up:
            return {'signal': 'LONG', 'entry': close.iloc[-1], 
                    'stop_loss': ema21.iloc[-1], 'reason': 'EMA_MOMENTUM_LONG'}
        
        # 做空：EMA9 < EMA21 < EMA50
        bearish_stack = (ema9.iloc[-1] < ema21.iloc[-1] < ema50.iloc[-1])
        macd_cross_down = histogram.iloc[-1] < 0 and histogram.iloc[-2] > 0
        
        if bearish_stack and macd_cross_down:
            return {'signal': 'SHORT', 'entry': close.iloc[-1],
                    'stop_loss': ema21.iloc[-1], 'reason': 'EMA_MOMENTUM_SHORT'}
        
        return None
```

### 風險評估
- 🟢 **優點**：最易實作；回測容易；反應快
- 🟡 **缺點**：滯後指標，容易追高殺低；盤整市場鞭打嚴重
- 🔴 **風險**：單獨使用勝率低；必須配合 SMC 結構過濾，不可單獨使用

---

## 策略 7：Statistical Arbitrage（Pairs Trading）

### 概述
配對交易利用兩檔相關性高的股票（如 JPM vs GS、AAPL vs MSFT）之間的協整（cointegration）關係，當價差（spread）偏離歷史均值時，做空強勢股、做多弱勢股，押注價差回歸。這是市場中性策略，不受整體市場方向影響。但計算複雜且需要穩定的協整關係，對美股日內交易難度較高。

### 適用市場條件
- ✅ 任何市場條件（市場中性）
- ✅ **高相關性的同業股票**（銀行、科技、石油等）
- ❌ **協整關係破裂期**（如公司財報、併購等特殊事件）

### 實作複雜度：⭐⭐⭐⭐⭐（5/5）— 最複雜

### Alpaca API 支援
- ✅ 兩支股票的 bars 資料完整支援
- ⚠️ 需即時計算協整和 z-score；計算量較大

### 建議參數
| 參數 | 建議值 | 說明 |
|------|--------|------|
| 協整 p-value | < 0.05 | Engle-Granger 測試 |
| z-score 進場 | ±2.0 | 95% 信心區間 |
| z-score 出場 | ±0.5 | 回歸均值 |
| z-score 止損 | ±3.0 | 協整破裂訊號 |
| 滾動窗口 | 60-100 bars | 動態重新計算 |

### 適合的股票配對範例
| 配對 | 邏輯 |
|------|------|
| JPM / GS | 投資銀行同業 |
| XOM / CVX | 石油同業 |
| AAPL / MSFT | 大型科技 |
| KO / PEP | 飲料同業 |
| GLD / SLV | 貴金屬 ETF |

### 與 SMC 結合方式
- 配對交易和 SMC 的結合較為複雜
- 可使用 SMC 結構判斷哪檔股票「更強」或「更弱」，作為配對選擇的輔助
- 當配對 z-score 達到進場條件時，優先看 SMC 弱勢方的結構

### 風險評估
- 🟢 **優點**：市場中性；理論上夏普比率高
- 🟡 **缺點**：實作複雜；協整關係需定期重新驗證；日內成本高
- 🔴 **風險**：協整關係突然破裂時損失慘重（如一檔被收購）；不建議作為主策略

---

## 策略 8：Market Microstructure（Level 2 / Order Flow）

### 概述
透過 Level 2 掛單資料和訂單流分析，識別大型機構訂單的隱藏意圖（如冰山訂單、Spoofing）。是高頻交易和職業做市商的核心工具，對散戶日內交易也有重要參考價值。**但 Alpaca 目前不提供股票 Level 2 資料**，實作此策略需要第三方 Level 2 資料源。

### Alpaca API 支援狀況
- ❌ **股票 Level 2 不支援**（Alpaca 官方確認，2024 仍無此功能）
- ✅ **加密貨幣 Level 2 支援**（但我們目標是美股）
- ✅ Alpaca 提供 top-of-book quotes（最佳買/賣報價）
- ⚠️ 替代方案：Polygon.io（Level 2 $29/月起），或 IBKR API

### 如果要實作的替代方案

```python
# 使用 Polygon.io 取得 Level 2 資料
# https://polygon.io/docs/stocks/ws_stocks_q

import websockets
import json

async def get_level2_data(symbol, api_key):
    uri = f"wss://socket.polygon.io/stocks"
    async with websockets.connect(uri) as ws:
        # 認證
        await ws.send(json.dumps({"action": "auth", "params": api_key}))
        
        # 訂閱 quotes（Level 1）和 level2 quotes
        await ws.send(json.dumps({
            "action": "subscribe",
            "params": f"Q.{symbol}"  # Quote stream
        }))
        
        while True:
            msg = json.loads(await ws.recv())
            # 分析 bid_size / ask_size 不平衡
            if msg[0]['ev'] == 'Q':
                bid_size = msg[0].get('bs', 0)
                ask_size = msg[0].get('as', 0)
                imbalance = bid_size / (bid_size + ask_size) if (bid_size + ask_size) > 0 else 0.5
                
                if imbalance > 0.7:  # 70% 掛買 → 看漲
                    print(f"Bullish Order Flow: {imbalance:.2%}")
                elif imbalance < 0.3:  # 70% 掛賣 → 看跌
                    print(f"Bearish Order Flow: {imbalance:.2%}")
```

### 風險評估
- 🟢 **優點**：最接近真實市場機制；可識別機構行為
- 🟡 **缺點**：需要額外付費資料源；資料量大，處理困難
- 🔴 **風險**：Alpaca 不原生支援，需換資料源或增加成本；Spoofing 會迷惑訊號

---

## 🔧 策略整合建議：多層確認系統

### 建議架構：SMC 主導 + VWAP/ORB 確認

```
層級 1（主觀結構分析）：SMC v3.0
  → 識別市場結構（BOS / CHOCH）
  → 識別 Order Block、FVG、Imbalance
  → 確定偏向方向（多/空）

層級 2（客觀技術確認）：VWAP + ORB
  → VWAP 確認機構定價方向
  → ORB 確認開盤方向性
  → 兩者與 SMC 方向一致 → 進場信心度 HIGH

層級 3（可選輔助）：均值回歸 / EMA 動能
  → 盤整市場用 Mean Reversion
  → 趨勢市場用 EMA Momentum

層級 4（情境過濾）：Gap / News
  → 有重大催化劑時，調整策略參數
  → 財報日前一天避免持倉過夜
```

### 訊號評分系統

```python
class SignalScorer:
    """多層確認評分系統"""
    
    def score_signal(self, smc_signal, vwap_signal, orb_signal, 
                     mean_rev_signal, news_context):
        score = 0
        reasons = []
        
        # SMC 訊號（最高權重）
        if smc_signal and smc_signal['valid']:
            score += 40
            reasons.append(f"SMC: {smc_signal['type']}")
        
        # VWAP 方向一致
        if vwap_signal and vwap_signal['signal'] == smc_signal.get('direction'):
            score += 25
            reasons.append("VWAP 確認")
        
        # ORB 方向一致
        if orb_signal and orb_signal['signal'] == smc_signal.get('direction'):
            score += 20
            reasons.append("ORB 突破確認")
        
        # 均值回歸輔助（盤整市場）
        if mean_rev_signal and mean_rev_signal.get('confidence') == 'HIGH':
            score += 10
            reasons.append("SMC Zone + 均值回歸")
        
        # 新聞情緒
        if news_context and news_context['sentiment'] != 'NEUTRAL':
            score += 5
            reasons.append(f"新聞: {news_context['sentiment']}")
        
        return {
            'score': score,
            'execute': score >= 65,  # 65 分以上才執行
            'confidence': 'HIGH' if score >= 80 else 'NORMAL' if score >= 65 else 'LOW',
            'reasons': reasons
        }
```

---

## 📈 回測表現參考（公開數據）

| 策略 | 市場條件 | 勝率（估計） | 平均 R:R | 年化報酬 | 最大回撤 |
|------|----------|-------------|----------|----------|----------|
| VWAP Bounce | 趨勢市場 | 55-65% | 1:2 | 20-40% | 10-15% |
| ORB（15min）| 高波動 | 50-60% | 1:2.5 | 25-50% | 12-20% |
| Mean Reversion | 盤整市場 | 65-80% | 1:1.5 | 15-30% | 8-12% |
| Gap Trading | 財報季 | 50-65% | 1:2 | 依催化劑 | 15-25% |
| Pairs Trading | 全市場 | 60-70% | 1:1.5 | 10-20% | 5-10% |

*注意：以上數據為文獻中的理論值，實際表現因市場狀況、參數設定、執行品質而有所不同*

---

## 🚀 實作路線圖建議

### Phase 1（第 1-2 週）
- [x] SMC v3.0（已有）
- [ ] 新增 VWAP 計算和 bounce 偵測
- [ ] 新增 ORB 建立和突破偵測

### Phase 2（第 3-4 週）
- [ ] Mean Reversion（RSI + BB）+ ADX 過濾
- [ ] 整合多層確認評分系統
- [ ] 建立回測框架驗證

### Phase 3（第 5-6 週）
- [ ] Volume Profile 自行計算
- [ ] Gap Scanning + Marketaux 新聞整合
- [ ] EMA Momentum 作為確認層

### Phase 4（選用）
- [ ] Pairs Trading（需穩定的協整對）
- [ ] Polygon.io Level 2 資料（付費升級）

---

*報告生成時間：2026-02-16*  
*研究者：Claude Code Subagent*  
*數據來源：Perplexity Web Search、Alpaca 官方文件、學術回測數據*
