# US Quant BOT (Test)

美股日內交易模擬 Bot — Alpaca Paper Trading

## 策略
- **SMC v3.0** — Smart Money Concepts（OB/FVG/BOS/MSS/多時框分析）
- **VWAP** — Bounce/Rejection + ±1σ/±2σ 偏差帶
- **ORB** — Opening Range Breakout（開盤 15 分鐘高低突破）

## 評分系統
| 來源 | 最高分 |
|------|--------|
| SMC | ~70 |
| VWAP | +25 |
| ORB | +20 |
| **入場門檻** | **≥ 70** |

## 風控
- 日損上限：總資金 1.5%
- 部位大小：Kelly × 0.3
- 停損：ATR × 2.0
- 停利：ATR × 3.0
- 連虧 3 筆 → 冷靜期

## 使用方式
```bash
python3 us-daytrade-bot.py account    # 帳戶狀態
python3 us-daytrade-bot.py analyze NVDA  # 單股分析
python3 us-daytrade-bot.py screener   # 盤前篩選
python3 us-daytrade-bot.py run        # 啟動交易
python3 us-daytrade-bot.py log        # 今日交易日誌
```

## 交易時段（台灣時間）
- Kill Zone 1: 21:30-23:00（開盤）
- Kill Zone 2: 00:00-01:30（午盤）
- Kill Zone 3: 02:30-04:00（收盤）

## ⚠️ 注意
- 僅限 Paper Trading（模擬交易）
- API Key 從外部 credentials 檔案載入，不含在 repo 中
