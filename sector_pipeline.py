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
    """證交所上市三大法人買賣超(股數),拆分外資 / 投信 / 自營商。"""
    payload = http_get_json(
        "https://www.twse.com.tw/fund/T86",
        {"response": "json", "date": d.strftime("%Y%m%d"), "selectType": "ALLBUT0999"},
    )
    if not payload or payload.get("stat") not in (None, "OK"):
        return []
    fields, rows = find_table(payload, "證券代號")
    if not fields:
        return []
    print(f"[fields] TWSE T86 欄位:{fields}")

    i_id = col_idx(fields, "證券代號")
    i_net = col_idx(fields, "三大法人買賣超股數")
    if i_net is None:
        i_net = len(fields) - 1
    # 外資:優先取「外陸資買賣超股數(不含外資自營商)」,其次任何含外資的買賣超欄
    i_fore = col_idx(fields, "外陸資買賣超股數(不含外資自營商)", "外資買賣超股數")
    if i_fore is None:
        for i, f in enumerate(fields):
            if "外" in f and "買賣超" in f and "自營" not in f:
                i_fore = i
                break
    i_trust = col_idx(fields, "投信買賣超股數")
    if i_trust is None:
        for i, f in enumerate(fields):
            if "投信" in f and "買賣超" in f:
                i_trust = i
                break

    out = []
    need = max(x for x in (i_id, i_net) if x is not None)
    for r in rows:
        if len(r) <= need:
            continue
        sid = str(r[i_id]).strip()
        if not STOCK_ID_RE.match(sid):
            continue
        net = to_num(r[i_net])
        if net is None:
            continue
        rec = {"date": d.isoformat(), "stock_id": sid, "net_shares": net,
               "foreign_shares": 0.0, "trust_shares": 0.0}
        if i_fore is not None and len(r) > i_fore:
            rec["foreign_shares"] = to_num(r[i_fore]) or 0.0
        if i_trust is not None and len(r) > i_trust:
            rec["trust_shares"] = to_num(r[i_trust]) or 0.0
        out.append(rec)
    return out


def fetch_tpex_inst(d: date) -> list[dict]:
    """
    櫃買中心上櫃三大法人買賣超(股數),拆分外資 / 投信。

    TPEx 欄位名稱不含法人前綴(皆為「買進股數/賣出股數/買賣超股數」重複六組),
    只能依固定順序判讀。實際欄位順序:
      [0]代號 [1]名稱
      [2:5]   外資及陸資(不含外資自營商)   → 買賣超在 index 4
      [5:8]   外資自營商
      [8:11]  投信                          → 買賣超在 index 10
      [11:14] 自營商(自行買賣)
      [14:17] 自營商(避險)
      [17:20] 自營商合計
      [-1]    三大法人買賣超股數合計
    以「合計 ≈ 外資+投信+自營」交叉驗證,對不上則該日退回僅取合計。
    """
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
        if "三大法人" in f:
            i_net = i
    if i_net is None:
        i_net = len(fields) - 1

    # 依固定順序定位;若欄位數不符預期則不拆分,僅取合計
    i_fore, i_trust = (4, 10) if i_net >= 20 else (None, None)
    if i_fore is None:
        print(f"[warn] 上櫃法人欄位數 {len(fields)} 與預期不符,{d} 僅取合計不拆分。")

    out, checked = [], False
    for r in rows:
        if len(r) <= i_net:
            continue
        sid = str(r[i_id]).strip()
        if not STOCK_ID_RE.match(sid):
            continue
        net = to_num(r[i_net])
        if net is None:
            continue
        rec = {"date": d.isoformat(), "stock_id": sid, "net_shares": net,
               "foreign_shares": 0.0, "trust_shares": 0.0}
        if i_fore is not None:
            fore = to_num(r[i_fore]) or 0.0
            trust = to_num(r[i_trust]) or 0.0
            # 首筆交叉驗證:外資+外資自營+投信+自營合計 應等於三大法人合計
            if not checked:
                checked = True
                parts = sum((to_num(r[i]) or 0.0) for i in (4, 7, 10, 19)
                            if len(r) > i)
                if abs(parts - net) > max(abs(net) * 0.02, 1000):
                    print(f"[warn] 上櫃法人欄位交叉驗證失敗"
                          f"(分項合計 {parts:.0f} vs 官方合計 {net:.0f}),"
                          f"{d} 起停用拆分,僅取合計。")
                    i_fore = i_trust = None
            if i_fore is not None:
                rec["foreign_shares"] = fore
                rec["trust_shares"] = trust
        out.append(rec)
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


SUM_PERIODS = ("daily", "wtd", "mtd")  # 需要逐日加總的期間(季/年僅計算漲跌幅)


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
        if not base_date:
            out[f"chg_{period}"] = float("nan")
            continue
        base_close = px[px["date"] == base_date].drop_duplicates("stock_id") \
            .set_index("stock_id")["close"]
        out[f"chg_{period}"] = (close_today / base_close - 1) * 100

    for period, base_date in bases.items():
        if period in SUM_PERIODS and base_date:
            mask = (px["date"] > base_date) & (px["date"] <= today)
            agg = px[mask].groupby("stock_id")[["value", "volume"]].sum()
            out[f"value_{period}"] = agg["value"]
            out[f"vol_{period}"] = agg["volume"]
        else:
            out[f"value_{period}"] = float("nan")
            out[f"vol_{period}"] = float("nan")

    INST_COLS = {"inst_net": "net_shares", "fore_net": "foreign_shares",
                 "trust_net": "trust_shares"}
    for period, base_date in bases.items():
        for prefix, col in INST_COLS.items():
            if period in SUM_PERIODS and base_date and not inst.empty and col in inst:
                mask = (inst["date"] > base_date) & (inst["date"] <= today)
                net = inst[mask].groupby("stock_id")[col].sum()
                out[f"{prefix}_{period}"] = (net * close_today).round(0)
            else:
                out[f"{prefix}_{period}"] = float("nan")

    return out.reset_index()


def aggregate(stats: pd.DataFrame, mapping: pd.DataFrame, group_col: str) -> list[dict]:
    df = stats.merge(mapping, on="stock_id", how="inner")

    def opt_sum(g, col):
        return int(g[col].sum(skipna=True)) if g[col].notna().any() else None

    results = []
    for name, g in df.groupby(group_col):
        if not str(name).strip():
            continue
        item = {"name": name, "stock_count": int(len(g)), "stocks": []}
        for p in ("daily", "wtd", "mtd", "qtd", "ytd"):
            if f"chg_{p}" not in g.columns:
                item[p] = {"avg_change_pct": None, "median_change_pct": None,
                           "up_ratio": None, "trading_value": None,
                           "trading_volume": None, "inst_net_value": None}
                continue
            chg = g[f"chg_{p}"].dropna()
            item[p] = {
                "avg_change_pct": round(chg.mean(), 2) if len(chg) else None,
                "median_change_pct": round(chg.median(), 2) if len(chg) else None,
                "up_ratio": round((chg > 0).mean() * 100, 1) if len(chg) else None,
                "trading_value": opt_sum(g, f"value_{p}"),
                "trading_volume": opt_sum(g, f"vol_{p}"),
                "inst_net_value": opt_sum(g, f"inst_net_{p}"),
                "foreign_net_value": opt_sum(g, f"fore_net_{p}"),
                "trust_net_value": opt_sum(g, f"trust_net_{p}"),
            }
        for _, r in g.sort_values("chg_daily", ascending=False).iterrows():
            def num(v, nd=2):
                return None if pd.isna(v) else round(float(v), nd)
            item["stocks"].append({
                "stock_id": r["stock_id"],
                "stock_name": r.get("stock_name", ""),
                "close": num(r["close"]),
                "chg_daily": num(r["chg_daily"]),
                "chg_wtd": num(r["chg_wtd"]),
                "chg_mtd": num(r["chg_mtd"]),
                "chg_qtd": num(r.get("chg_qtd")),
                "chg_ytd": num(r.get("chg_ytd")),
                "value_d": int(r["value_d"]) if not pd.isna(r["value_d"]) else 0,
                "inst_net_d": int(r["inst_net_daily"]) if not pd.isna(r["inst_net_daily"]) else 0,
                "fore_net_d": int(r["fore_net_daily"]) if not pd.isna(r["fore_net_daily"]) else 0,
                "trust_net_d": int(r["trust_net_daily"]) if not pd.isna(r["trust_net_daily"]) else 0,
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

    # 季基準:本季第一天之前的最後交易日(七月時 = 六月底,與月基準相同)
    q_month = ((t.month - 1) // 3) * 3 + 1
    q_start = t.replace(month=q_month, day=1)

    def probe_base(target: date, label: str):
        """往回探測某個基準日的收盤資料,回傳 (日期字串, 資料列)。"""
        for back in range(12):
            probe = target - timedelta(days=back)
            if probe.weekday() >= 5:
                continue
            rows = safe_fetch(fetch_twse_prices, probe, f"{label}基準行情")
            if rows:
                rows += safe_fetch(fetch_tpex_prices, probe, f"{label}基準行情(櫃)")
                return probe.isoformat(), rows
        print(f"[warn] 找不到{label}基準日資料,該期間統計將略過。")
        return None, []

    extra_rows = []
    qtd_base = max((d for d in trade_dates if d < q_start.isoformat()), default=None)
    if qtd_base is None:
        qtd_base, rows = probe_base(q_start - timedelta(days=1), "季")
        extra_rows += rows
    bases["qtd"] = qtd_base

    ytd_base, rows = probe_base(date(t.year - 1, 12, 31), "年")
    extra_rows += rows
    bases["ytd"] = ytd_base

    if extra_rows:
        px = pd.concat([px, pd.DataFrame(extra_rows)], ignore_index=True)
        px = px[px["close"].notna()]

    print(f"[3/6] 基準日 → 日:{bases['daily']} 週:{bases['wtd']} 月:{bases['mtd']}"
          f" 季:{bases['qtd']} 年:{bases['ytd']}")

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
        columns=["date", "stock_id", "net_shares", "foreign_shares", "trust_shares"])

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
        "note": "法人金額為估算值:買賣超股數 × 當日收盤價;foreign=外資、trust=投信、inst=三大法人合計",
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
