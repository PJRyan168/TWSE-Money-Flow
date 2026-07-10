# 台股盤後族群資金統計管線

每個交易日收盤後自動統計 **當日 / 當周(WTD) / 當月(MTD)** 的:

- 官方 TWSE 產業分類 與 自訂題材族群 的平均漲跌幅、中位數漲跌幅、齊漲率
- 成交量、成交金額(當日與期間累計)
- 三大法人買賣超資金流向(估算金額 = 買賣超股數 × 收盤價)

輸出 `output/sector_stats.json`,可直接由「盤後族群資金熱力」React 儀表板讀取。

## 部署步驟

1. 建立一個 GitHub repo,把本資料夾所有檔案推上去。
2. 到 [FinMind](https://finmindtrade.com) 免費註冊,取得 API token。
3. Repo → Settings → Secrets and variables → Actions → 新增 secret:
   - 名稱:`FINMIND_TOKEN`,值:你的 token。
4. 完成。每個交易日台北時間 17:30 會自動執行並把結果 commit 回 repo。
   也可到 Actions 頁面按 **Run workflow** 手動補跑(可搭配 `--date` 改腳本參數回補歷史)。

## 儀表板串接

結果檔的固定網址(raw):

```
https://raw.githubusercontent.com/<你的帳號>/<repo>/main/output/sector_stats.json
```

React 端 `fetch()` 這個網址即可。若要更正式,可開 GitHub Pages 指向 repo。

## JSON 結構

```jsonc
{
  "trade_date": "2026-07-08",
  "base_dates": { "daily": "2026-07-07", "wtd": "2026-07-03", "mtd": "2026-06-30" },
  "official_sectors": [
    {
      "name": "半導體業",
      "stock_count": 120,
      "daily": {
        "avg_change_pct": 1.23,      // 平均漲跌幅 %
        "median_change_pct": 0.95,
        "up_ratio": 68.5,            // 齊漲率 %
        "trading_value": 123456789,  // 成交金額(元)
        "trading_volume": 987654,    // 成交量(股)
        "inst_net_value": 456789     // 三大法人買賣超估算金額(元)
      },
      "wtd": { ... },
      "mtd": { ... },
      "stocks": [ { "stock_id": "2330", "chg_daily": 1.5, ... } ]
    }
  ],
  "themes": [ ...同結構,依 themes.json 分組... ]
}
```

## 自訂題材

編輯 `themes.json` 增減題材與成分股(股票代號字串)。
同一檔股票可同時屬於多個題材。目前內含的成分股為**範例模板**,
請依你自己的追蹤清單校正後再上線。

## 注意事項

- 法人買賣超金額為估算值(股數 × 收盤價),與交易所公布的金額會有些微差異。
- FinMind 免費版有流量限制,腳本已內建節流(每次請求間隔 0.6 秒)與重試。
- 週基準 = 上週最後一個交易日收盤;月基準 = 上月最後一個交易日收盤。
