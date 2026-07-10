# -*- coding: utf-8 -*-
"""
台股盤後族群資金統計管線
=========================
每日收盤後執行,統計「當日 / 當周(WTD) / 當月(MTD)」:
  1. 官方 TWSE 產業分類 與 自訂題材族群 的平均漲跌幅
  2. 成交量 / 成交金額
  3. 三大法人買賣超(籌碼資金流向,金額為估算值 = 買賣超股數 × 收盤價)

資料來源:FinMind 免費 API (https://finmindtrade.com)
輸出:output/sector_stats.json (供 React 儀表板讀取)

用法:
  export FINMIND_TOKEN=你的token   # 免費註冊即可,可提高流量限制
  python sector_pipeline.py [--date 2026-07-08]
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

API_URL = "https://api.finmindtrade.com/api/v4/data"
TOKEN = os.environ.get("FINMIND_TOKEN", "")
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
THEMES_FILE = BASE_DIR / "themes.json"

# ---------------------------------------------------------------- FinMind API


def fm_get(dataset: str, retries: int = 3, **params) -> pd.DataFrame:
    """呼叫 FinMind API,回傳 DataFrame。"""
    payload = {"dataset": dataset, **params}
    if TOKEN:
        payload["token"] = TOKEN
    for attempt in range(retries):
        try:
            r = requests.get(API_URL, params=payload, timeout=60)
            r.raise_for_status()
            data = r.json()
            if data.get("status") == 200 or "data" in data:
                return pd.DataFrame(data.get("data", []))
            raise RuntimeError(data.get("msg", "unknown FinMind error"))
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            wait = 10 * (attempt + 1)
            print(f"[warn] {dataset} 失敗 ({e}),{wait}s 後重試...")
            time.sleep(wait)
    return pd.DataFrame()


# ------------------------------------------------------------- 日期參考點計算


def ref_dates(trade_dates: list[str], today: str) -> dict:
    """
    以實際交易日序列推算三個基準日(比較基準 = 基準日收盤價):
      daily : 前一交易日
      wtd   : 上週最後一個交易日(本週第一天的前一交易日)
      mtd   : 上月最後一個交易日
    """
    ds = sorted(d for d in trade_dates if d <= today)
    if today not in ds:
        raise RuntimeError(f"{today} 非交易日或資料尚未更新")
    idx = ds.index(today)
    prev_day = ds[idx - 1] if idx >= 1 else today

    t = datetime.strptime(today, "%Y-%m-%d").date()
    week_start = t - timedelta(days=t.weekday())          # 本週一
    month_start = t.replace(day=1)

    wtd_base = max((d for d in ds if d < week_start.isoformat()), default=prev_day)
    mtd_base = max((d for d in ds if d < month_start.isoformat()), default=prev_day)
    return {"daily": prev_day, "wtd": wtd_base, "mtd": mtd_base}


# ------------------------------------------------------------------ 資料抓取


def fetch_prices(start: str, end: str) -> pd.DataFrame:
    """抓取全市場日成交資料(逐日抓取,避免單次回應過大)。"""
    frames = []
    d = datetime.strptime(start, "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    while d <= end_d:
        if d.weekday() < 5:  # 週一~週五
            df = fm_get("TaiwanStockPrice", start_date=d.isoformat(), end_date=d.isoformat())
            if not df.empty:
                frames.append(df)
                print(f"  價格 {d} : {len(df)} 檔")
            time.sleep(0.6)  # 禮貌性節流,避免撞免費版流量限制
        d += timedelta(days=1)
    if not frames:
        raise RuntimeError("抓不到任何價格資料")
    px = pd.concat(frames, ignore_index=True)
    px["close"] = pd.to_numeric(px["close"], errors="coerce")
    px["Trading_Volume"] = pd.to_numeric(px["Trading_Volume"], errors="coerce")
    px["Trading_money"] = pd.to_numeric(px["Trading_money"], errors="coerce")
    return px


def fetch_institutional(dates: list[str]) -> pd.DataFrame:
    """抓取三大法人買賣超(逐日)。"""
    frames = []
    for d in dates:
        df = fm_get(
            "TaiwanStockInstitutionalInvestorsBuySell",
            start_date=d, end_date=d,
        )
        if not df.empty:
            frames.append(df)
            print(f"  法人 {d} : {len(df)} 筆")
        time.sleep(0.6)
    if not frames:
        return pd.DataFrame(columns=["date", "stock_id", "buy", "sell", "name"])
    inst = pd.concat(frames, ignore_index=True)
    inst["buy"] = pd.to_numeric(inst["buy"], errors="coerce").fillna(0)
    inst["sell"] = pd.to_numeric(inst["sell"], errors="coerce").fillna(0)
    inst["net_shares"] = inst["buy"] - inst["sell"]
    return inst


# ------------------------------------------------------------------ 統計計算


def stock_period_stats(px: pd.DataFrame, inst: pd.DataFrame,
                       today: str, bases: dict) -> pd.DataFrame:
    """計算每檔個股三個期間的漲跌幅、量能與法人買賣超估算金額。"""
    today_px = px[px["date"] == today].set_index("stock_id")
    close_today = today_px["close"]

    def close_at(d):
        return px[px["date"] == d].set_index("stock_id")["close"]

    rows = {"stock_id": close_today.index, "close": close_today.values,
            "volume_d": today_px["Trading_Volume"].values,
            "value_d": today_px["Trading_money"].values}
    out = pd.DataFrame(rows).set_index("stock_id")

    for period, base_date in bases.items():
        base_close = close_at(base_date)
        out[f"chg_{period}"] = (close_today / base_close - 1) * 100

    # 期間累計成交金額 / 量
    for period, base_date in bases.items():
        mask = (px["date"] > base_date) & (px["date"] <= today)
        agg = px[mask].groupby("stock_id")[["Trading_money", "Trading_Volume"]].sum()
        out[f"value_{period}"] = agg["Trading_money"]
        out[f"vol_{period}"] = agg["Trading_Volume"]

    # 法人買賣超:期間內 net_shares 加總 × 今日收盤價 ≈ 資金流向估算(元)
    if not inst.empty:
        for period, base_date in bases.items():
            mask = (inst["date"] > base_date) & (inst["date"] <= today)
            net = inst[mask].groupby("stock_id")["net_shares"].sum()
            out[f"inst_net_{period}"] = (net * close_today).round(0)
    else:
        for period in bases:
            out[f"inst_net_{period}"] = 0.0

    return out.reset_index()


def aggregate(stats: pd.DataFrame, mapping: pd.DataFrame,
              group_col: str) -> list[dict]:
    """依分類欄位聚合出族群層級統計。"""
    df = stats.merge(mapping, on="stock_id", how="inner")
    results = []
    for name, g in df.groupby(group_col):
        item = {"name": name, "stock_count": int(len(g)), "stocks": []}
        for p in ("daily", "wtd", "mtd"):
            chg = g[f"chg_{p}"].dropna()
            item[p] = {
                "avg_change_pct": round(chg.mean(), 2) if len(chg) else None,
                "median_change_pct": round(chg.median(), 2) if len(chg) else None,
                "up_ratio": round((chg > 0).mean() * 100, 1) if len(chg) else None,  # 齊漲率
                "trading_value": int(g[f"value_{p}"].fillna(0).sum()),
                "trading_volume": int(g[f"vol_{p}"].fillna(0).sum()),
                "inst_net_value": int(g[f"inst_net_{p}"].fillna(0).sum()),
            }
        # 個股明細(供儀表板 drill-down)
        for _, r in g.sort_values("chg_daily", ascending=False).iterrows():
            item["stocks"].append({
                "stock_id": r["stock_id"],
                "stock_name": r.get("stock_name", ""),
                "close": None if pd.isna(r["close"]) else float(r["close"]),
                "chg_daily": None if pd.isna(r["chg_daily"]) else round(r["chg_daily"], 2),
                "chg_wtd": None if pd.isna(r["chg_wtd"]) else round(r["chg_wtd"], 2),
                "chg_mtd": None if pd.isna(r["chg_mtd"]) else round(r["chg_mtd"], 2),
                "value_d": int(r["value_d"]) if not pd.isna(r["value_d"]) else 0,
                "inst_net_d": int(r["inst_net_daily"]) if not pd.isna(r["inst_net_daily"]) else 0,
            })
        results.append(item)
    # 依當日平均漲幅排序
    results.sort(key=lambda x: (x["daily"]["avg_change_pct"] or -999), reverse=True)
    return results


# ---------------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="統計基準日 YYYY-MM-DD,預設為今天")
    args = parser.parse_args()

    today = args.date or date.today().isoformat()
    t = datetime.strptime(today, "%Y-%m-%d").date()
    # 往前抓到上月底再往前緩衝 10 天,確保基準日收盤價齊全
    fetch_start = (t.replace(day=1) - timedelta(days=10)).isoformat()

    print(f"=== 台股盤後族群統計 | 基準日 {today} ===")
    print("[1/5] 抓取全市場價格資料...")
    px = fetch_prices(fetch_start, today)
    trade_dates = sorted(px["date"].unique())

    if today not in trade_dates:
        print(f"[info] {today} 無交易資料(假日或資料未更新),結束。")
        sys.exit(0)

    bases = ref_dates(trade_dates, today)
    print(f"[2/5] 基準日 → 日:{bases['daily']} 週:{bases['wtd']} 月:{bases['mtd']}")

    period_dates = [d for d in trade_dates if d > bases["mtd"]]
    print("[3/5] 抓取三大法人買賣超...")
    inst = fetch_institutional(period_dates)

    print("[4/5] 計算個股期間統計...")
    stats = stock_period_stats(px, inst, today, bases)

    # 官方產業分類
    info = fm_get("TaiwanStockInfo")
    info = info[["stock_id", "stock_name", "industry_category"]].drop_duplicates("stock_id")
    stats = stats.merge(info[["stock_id", "stock_name"]], on="stock_id", how="left")

    official_map = info.rename(columns={"industry_category": "sector"})[["stock_id", "sector"]]
    official = aggregate(stats, official_map, "sector")

    # 自訂題材分類
    themes_result = []
    if THEMES_FILE.exists():
        themes_cfg = json.loads(THEMES_FILE.read_text(encoding="utf-8"))
        rows = [{"stock_id": sid, "theme": theme}
                for theme, sids in themes_cfg.items() for sid in sids]
        theme_map = pd.DataFrame(rows)
        themes_result = aggregate(stats, theme_map, "theme")

    print("[5/5] 輸出 JSON...")
    OUTPUT_DIR.mkdir(exist_ok=True)
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": today,
        "base_dates": bases,
        "note": "inst_net_value 為估算值:買賣超股數 × 當日收盤價",
        "official_sectors": official,
        "themes": themes_result,
    }
    out_file = OUTPUT_DIR / "sector_stats.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    # 另存歷史檔,方便回溯
    (OUTPUT_DIR / f"sector_stats_{today}.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(f"完成 → {out_file}")


if __name__ == "__main__":
    main()
