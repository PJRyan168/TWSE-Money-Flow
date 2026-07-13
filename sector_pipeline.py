# -*- coding: utf-8 -*-
"""
台股盤後族群資金統計管線 v2
============================
資料來源改為官方免費公開 API(不需 token、無付費限制):
  - 上市:證交所 TWSE  MI_INDEX(每日收盤行情)、T86(三大法人買賣超)
  - 上櫃:櫃買中心 TPEx dailyQuotes(每日收盤行情)、insti/dailyTrade(三大法人)
  - FinMind 僅用於「產業分類對照表」(免費版可用;失敗時自動略過)

每日收盤後執行,統計「當日 / 當周(WTD) / 當月(MTD)」:
  1. 官方產業分類 與 自訂題材族群 的平均漲跌幅、齊漲率
  2. 成交量 / 成交金額(期間累計)
  3. 三大法人買賣超(資金流向,金額為估算值 = 買賣超股數 × 收盤價)

輸出:output/sector_stats.json (供 React 儀表板讀取)

用法:
  python sector_pipeline.py [--date 2026-07-08]
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
THEMES_FILE = BASE_DIR / "themes.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
REQ_INTERVAL = 3.0  # 官方網站有流量限制,每次請求間隔秒數

STOCK_ID_RE = re.compile(r"^\d{4}$")  # 只統計 4 碼普通股/ETF


# ------------------------------------------------------------------ 工具函式


def http_get_json(url: str, params: dict, retries: int = 3):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=60)
            r.raise_for_status()
            time.sleep(REQ_INTERVAL)
            return r.json()
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            wait = 15 * (attempt + 1)
            print(f"[warn] {url} 失敗 ({e}),{wait}s 後重試...")
            time.sleep(wait)
    return None


def to_num(s):
    """'1,234.56' → 1234.56;'--'、'---'、空值 → None"""
    if s is None:
        return None
    s = str(s).replace(",", "").replace("+", "").strip()
    if s in ("", "--", "---", "----", "-----", "N/A", "除息", "除權"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def find_table(payload: dict, key_field: str):
    """
    TWSE/TPEx 的 JSON 有兩種格式:
      新版 {"tables": [{"fields": [...], "data": [...]}, ...]}
      舊版 {"fields9": [...], "data9": [...]} 或 {"fields": [...], "data": [...]}
    回傳 (fields, rows),找不到回傳 (None, None)。
    """
    def norm(f):
        return str(f).replace(" ", "").replace("　", "")

    tables = []
    if isinstance(payload, dict):
        if "tables" in payload and isinstance(payload["tables"], list):
            tables = payload["tables"]
        else:
            # 舊版:掃描 fieldsN/dataN 配對
            for k in payload:
                if k.startswith("fields") and isinstance(payload[k], list):
                    dk = "data" + k[len("fields"):]
                    if dk in payload:
                        tables.append({"fields": payload[k], "data": payload[dk]})
    for t in tables:
        fields = t.get("fields") or []
        if any(norm(f).startswith(key_field) for f in fields):
            return [norm(f) for f in fields], t.get("data") or []
    return None, None


def col_idx(fields, *candidates):
    """依欄位名稱前綴尋找欄位位置。"""
    for c in candidates:
        for i, f in enumerate(fields):
            if f.startswith(c):
                return i
    return None


# ---------------------------------------------------------------- 官方資料抓取


def fetch_twse_prices(d: date) -> list[dict]:
    """證交所上市每日收盤行情(全市場)。"""
    payload = http_get_json(
        "https://www.twse.com.tw/exchangeReport/MI_INDEX",
        {"response": "json", "date": d.strftime("%Y%m%d"), "type": "ALLBUT0999"},
    )
    if not payload or payload.get("stat") not in (None, "OK"):
        return []
    fields, rows = find_table(payload, "證券代號")
    if not fields:
        return []
    i_id = col_idx(fields, "證券代號")
    i_name = col_idx(fields, "證券名稱")
    i_vol = col_idx(fields, "成交股數")
    i_val = col_idx(fields, "成交金額")
    i_close = col_idx(fields, "收盤價")
    out = []
    need = max(i_id, i_name, i_close, i_vol, i_val)
    for r in rows:
        if len(r) <= need:
            continue  # 欄位不完整的資料列(官方偶發格式差異),略過
        sid = str(r[i_id]).strip()
        if not STOCK_ID_RE.match(sid):
            continue
        out.append({
            "date": d.isoformat(), "stock_id": sid,
            "stock_name": str(r[i_name]).strip(),
            "close": to_num(r[i_close]),
            "volume": to_num(r[i_vol]) or 0,
            "value": to_num(r[i_val]) or 0,
        })
    return out


def fetch_tpex_prices(d: date) -> list[dict]:
    """櫃買中心上櫃每日收盤行情(全市場)。"""
    payload = http_get_json(
        "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes",
        {"response": "json", "date": d.strftime("%Y/%m/%d")},
    )
    if not payload:
        return []
    fields, rows = find_table(payload, "代號")
    if not fields:
        return []
    i_id = col_idx(fields, "代號")
    i_name = col_idx(fields, "名稱")
    i_close = col_idx(fields, "收盤")
    i_vol = col_idx(fields, "成交股數")
    i_val = col_idx(fields, "成交金額")
    out = []
    need = max(i_id, i_name, i_close, i_vol, i_val)
    for r in rows:
        if len(r) <= need:
            continue  # 欄位不完整的資料列,略過
        sid = str(r[i_id]).strip()
        if not STOCK_ID_RE.match(sid):
            continue
        out.append({
            "date": d.isoformat(), "stock_id": sid,
            "stock_name": str(r[i_name]).strip(),
            "close": to_num(r[i_close]),
            "volume": to_num(r[i_vol]) or 0,
            "value": to_num(r[i_val]) or 0,
        })
    return out


def fetch_twse_inst(d: date) -> list[dict]:
    """證交所上市三大法人買賣超(股數)。"""
    payload = http_get_json(
        "https://www.twse.com.tw/fund/T86",
        {"response": "json", "date": d.strftime("%Y%m%d"), "selectType": "ALLBUT0999"},
    )
    if not payload or payload.get("stat") not in (None, "OK"):
        return []
    fields, rows = find_table(payload, "證券代號")
    if not fields:
        return []
    i_id = col_idx(fields, "證券代號")
    i_net = col_idx(fields, "三大法人買賣超股數")
    if i_net is None:
        i_net = len(fields) - 1  # 慣例上為最後一欄
    out = []
    for r in rows:
        if len(r) <= max(i_id, i_net):
            continue  # 欄位不完整的資料列,略過
        sid = str(r[i_id]).strip()
        if not STOCK_ID_RE.match(sid):
            continue
        net = to_num(r[i_net])
        if net is not None:
            out.append({"date": d.isoformat(), "stock_id": sid, "net_shares": net})
    return out


def fetch_tpex_inst(d: date) -> list[dict]:
    """櫃買中心上櫃三大法人買賣超(股數)。端點若異動則略過,不影響整體。"""
    try:
        payload = http_get_json(
            "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade",
            {"type": "Daily", "sect": "EW", "response": "json",
             "date": d.strftime("%Y/%m/%d")},
            retries=1,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 上櫃法人資料抓取失敗({e}),當日以 0 計。")
        return []
    if not payload:
        return []
    fields, rows = find_table(payload, "代號")
    if not fields:
        return []
    i_id = col_idx(fields, "代號")
    i_net = None
    for i, f in enumerate(fields):
        if "三大法人" in f and ("買賣超" in f or "合計" in f):
            i_net = i
    if i_net is None:
        i_net = len(fields) - 1
    out = []
    for r in rows:
        if len(r) <= max(i_id, i_net):
            continue  # 欄位不完整的資料列,略過
        sid = str(r[i_id]).strip()
        if not STOCK_ID_RE.match(sid):
            continue
        net = to_num(r[i_net])
        if net is not None:
            out.append({"date": d.isoformat(), "stock_id": sid, "net_shares": net})
    return out


def fetch_industry_map() -> pd.DataFrame:
    """產業分類對照表(FinMind 免費版可用;失敗則回傳空表)。"""
    try:
        params = {"dataset": "TaiwanStockInfo"}
        token = os.environ.get("FINMIND_TOKEN", "")
        if token:
            params["token"] = token
        r = requests.get("https://api.finmindtrade.com/api/v4/data",
                         params=params, timeout=60)
        r.raise_for_status()
        df = pd.DataFrame(r.json().get("data", []))
        if df.empty:
            return pd.DataFrame(columns=["stock_id", "sector"])
        df = df[["stock_id", "industry_category"]].drop_duplicates("stock_id")
        return df.rename(columns={"industry_category": "sector"})
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 產業分類抓取失敗({e}),官方類股統計將標為「未分類」。")
        return pd.DataFrame(columns=["stock_id", "sector"])


# ------------------------------------------------------------- 日期與統計計算


def safe_fetch(fn, d, label):
    """單日資料抓取失敗時記錄警告並跳過,不中斷整體統計。"""
    try:
        return fn(d)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {label} {d} 抓取失敗({e}),略過該日該來源。")
        return []


def ref_dates(trade_dates: list[str], today: str) -> dict:
    ds = sorted(d for d in trade_dates if d <= today)
    idx = ds.index(today)
    prev_day = ds[idx - 1] if idx >= 1 else today
    t = datetime.strptime(today, "%Y-%m-%d").date()
    week_start = (t - timedelta(days=t.weekday())).isoformat()
    month_start = t.replace(day=1).isoformat()
    wtd = max((d for d in ds if d < week_start), default=prev_day)
    mtd = max((d for d in ds if d < month_start), default=prev_day)
    return {"daily": prev_day, "wtd": wtd, "mtd": mtd}


def stock_period_stats(px: pd.DataFrame, inst: pd.DataFrame,
                       today: str, bases: dict) -> pd.DataFrame:
    today_px = px[px["date"] == today].drop_duplicates("stock_id").set_index("stock_id")
    close_today = today_px["close"]

    out = pd.DataFrame({
        "stock_id": close_today.index,
        "stock_name": today_px["stock_name"].values,
        "close": close_today.values,
        "volume_d": today_px["volume"].values,
        "value_d": today_px["value"].values,
    }).set_index("stock_id")

    for period, base_date in bases.items():
        base_close = px[px["date"] == base_date].drop_duplicates("stock_id") \
            .set_index("stock_id")["close"]
        out[f"chg_{period}"] = (close_today / base_close - 1) * 100

    for period, base_date in bases.items():
        mask = (px["date"] > base_date) & (px["date"] <= today)
        agg = px[mask].groupby("stock_id")[["value", "volume"]].sum()
        out[f"value_{period}"] = agg["value"]
        out[f"vol_{period}"] = agg["volume"]

    if not inst.empty:
        for period, base_date in bases.items():
            mask = (inst["date"] > base_date) & (inst["date"] <= today)
            net = inst[mask].groupby("stock_id")["net_shares"].sum()
            out[f"inst_net_{period}"] = (net * close_today).round(0)
    for period in bases:
        col = f"inst_net_{period}"
        if col not in out:
            out[col] = 0.0

    return out.reset_index()


def aggregate(stats: pd.DataFrame, mapping: pd.DataFrame, group_col: str) -> list[dict]:
    df = stats.merge(mapping, on="stock_id", how="inner")
    results = []
    for name, g in df.groupby(group_col):
        if not str(name).strip():
            continue
        item = {"name": name, "stock_count": int(len(g)), "stocks": []}
        for p in ("daily", "wtd", "mtd"):
            chg = g[f"chg_{p}"].dropna()
            item[p] = {
                "avg_change_pct": round(chg.mean(), 2) if len(chg) else None,
                "median_change_pct": round(chg.median(), 2) if len(chg) else None,
                "up_ratio": round((chg > 0).mean() * 100, 1) if len(chg) else None,
                "trading_value": int(g[f"value_{p}"].fillna(0).sum()),
                "trading_volume": int(g[f"vol_{p}"].fillna(0).sum()),
                "inst_net_value": int(g[f"inst_net_{p}"].fillna(0).sum()),
            }
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
    results.sort(key=lambda x: (x["daily"]["avg_change_pct"]
                                if x["daily"]["avg_change_pct"] is not None else -999),
                 reverse=True)
    return results


# ---------------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="統計基準日 YYYY-MM-DD,預設今天")
    args = parser.parse_args()

    today_s = args.date or date.today().isoformat()
    t = datetime.strptime(today_s, "%Y-%m-%d").date()

    print(f"=== 台股盤後族群統計 v2 | 指定日 {today_s} ===")

    # 從指定日往回找最近一個「有收盤資料」的交易日
    # (自動涵蓋假日、颱風假、資料尚未公布等狀況)
    print("[1/6] 尋找最近交易日資料...")
    today_rows = []
    for back in range(10):  # 最多往回找 10 天
        probe = t - timedelta(days=back)
        if probe.weekday() == 6:  # 週日直接跳過
            continue
        rows = fetch_twse_prices(probe)
        if rows:
            t = probe
            today_s = t.isoformat()
            today_rows = rows
            break
        print(f"  {probe} 無交易資料(假日/颱風假/未公布),往前一天...")
    if not today_rows:
        print("[info] 近 10 天皆無交易資料,結束。")
        sys.exit(0)
    print(f"  → 使用最近交易日:{today_s}")
    fetch_start = t.replace(day=1) - timedelta(days=10)

    # 逐日抓取價格(上市 + 上櫃)
    print("[2/6] 抓取全市場每日行情(上市+上櫃)...")
    price_rows = []
    d = fetch_start
    while d <= t:
        if d.weekday() < 5:
            rows = today_rows if d == t else safe_fetch(fetch_twse_prices, d, "上市行情")
            rows_otc = safe_fetch(fetch_tpex_prices, d, "上櫃行情")
            if rows:
                price_rows.extend(rows)
                price_rows.extend(rows_otc)
                print(f"  {d} 上市 {len(rows)} 檔 / 上櫃 {len(rows_otc)} 檔")
        d += timedelta(days=1)
    px = pd.DataFrame(price_rows)
    px = px[px["close"].notna()]

    trade_dates = sorted(px["date"].unique())
    bases = ref_dates(trade_dates, today_s)
    print(f"[3/6] 基準日 → 日:{bases['daily']} 週:{bases['wtd']} 月:{bases['mtd']}")

    # 三大法人(僅需 mtd 基準日之後的區間)
    print("[4/6] 抓取三大法人買賣超...")
    inst_rows = []
    for ds in [x for x in trade_dates if x > bases["mtd"]]:
        dd = datetime.strptime(ds, "%Y-%m-%d").date()
        a = safe_fetch(fetch_twse_inst, dd, "上市法人")
        b = safe_fetch(fetch_tpex_inst, dd, "上櫃法人")
        inst_rows.extend(a)
        inst_rows.extend(b)
        print(f"  {ds} 上市 {len(a)} 筆 / 上櫃 {len(b)} 筆")
    inst = pd.DataFrame(inst_rows) if inst_rows else pd.DataFrame(
        columns=["date", "stock_id", "net_shares"])

    print("[5/6] 計算個股期間統計與族群聚合...")
    stats = stock_period_stats(px, inst, today_s, bases)

    industry = fetch_industry_map()
    official = aggregate(stats, industry, "sector") if not industry.empty else []

    themes_result = []
    if THEMES_FILE.exists():
        themes_cfg = json.loads(THEMES_FILE.read_text(encoding="utf-8"))
        rows = [{"stock_id": sid, "theme": theme}
                for theme, sids in themes_cfg.items() for sid in sids]
        themes_result = aggregate(stats, pd.DataFrame(rows), "theme")

    print("[6/6] 輸出 JSON...")
    OUTPUT_DIR.mkdir(exist_ok=True)
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": today_s,
        "base_dates": bases,
        "note": "inst_net_value 為估算值:買賣超股數 × 當日收盤價",
        "official_sectors": official,
        "themes": themes_result,
    }
    (OUTPUT_DIR / "sector_stats.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUTPUT_DIR / f"sector_stats_{today_s}.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print("完成 → output/sector_stats.json")


if __name__ == "__main__":
    main()
