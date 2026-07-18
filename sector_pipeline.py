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

輸出:
  output/sector_stats.json  (全市場,供 index.html 讀取)
  output/futures_stats.json (期貨標的母體,供 futures.html 讀取)
  output/cb_stats.json      (可轉債標的母體,供 cb.html 讀取)

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
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
THEMES_FILE = BASE_DIR / "themes.json"
FUTURES_UNIVERSE_FILE = BASE_DIR / "futures_universe.json"  # 期貨標的母體(可自動更新)
TAIFEX_STOCKLIST_URL = "https://www.taifex.com.tw/cht/2/stockLists"

# 可轉債標的母體(可自動更新)
CB_UNIVERSE_FILE = BASE_DIR / "cb_universe.json"
# 櫃買中心「最近上櫃轉(交)換公司債」清單頁(供自動探測 API 路徑用)
CB_LIST_PAGE = "https://www.tpex.org.tw/zh-tw/bond/issue/cbond/listed.html"
# 候選 JSON API 端點(依序嘗試;正確路徑以 Actions 執行 log 為準)
CB_API_CANDIDATES = [
    "https://www.tpex.org.tw/www/zh-tw/bond/issue/cbond/listed",
    "https://www.tpex.org.tw/www/zh-tw/bond/cbond/listed",
    "https://www.tpex.org.tw/www/zh-tw/bond/cbList",
    "https://www.tpex.org.tw/www/zh-tw/bond/cbListed",
]
CB_ID_RE = re.compile(r"^\d{5,6}$")   # 可轉債代號 = 股票代號4碼 + 期別(如 15132)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
REQ_INTERVAL = 3.0  # 官方網站有流量限制,每次請求間隔秒數

STOCK_ID_RE = re.compile(r"^\d{4}$")          # 主站沿用:4 碼普通股/ETF
SEC_ID_RE = re.compile(r"^\d{4,6}[A-Z]?$")    # 放寬:含 5-6 碼與債券 ETF(如 00679B)


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
            print(f"[warn] {url} 失敗 ({str(e)[:300]}),{wait}s 後重試...")
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
        if not SEC_ID_RE.match(sid):
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
        if not SEC_ID_RE.match(sid):
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
        if not SEC_ID_RE.match(sid):
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
        print(f"[warn] 上櫃法人資料抓取失敗({str(e)[:300]}),當日以 0 計。")
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
        if not SEC_ID_RE.match(sid):
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


INDUSTRY_CACHE_FILE = BASE_DIR / "industry_cache.json"


def fetch_industry_map() -> pd.DataFrame:
    """
    產業分類對照表(FinMind 免費版)。
    FinMind 以 IP 計流量,GitHub Actions 共用 IP 偶爾會被限流;
    成功時寫入 industry_cache.json 快取,失敗時改讀快取,
    避免官方類股檢視整個消失。
    """
    empty = pd.DataFrame(columns=["stock_id", "sector"])
    try:
        params = {"dataset": "TaiwanStockInfo"}
        token = os.environ.get("FINMIND_TOKEN", "")
        if token:
            params["token"] = token
        r = requests.get("https://api.finmindtrade.com/api/v4/data",
                         params=params, timeout=60)
        r.raise_for_status()
        df = pd.DataFrame(r.json().get("data", []))
        if not df.empty:
            df = df[["stock_id", "industry_category"]].drop_duplicates("stock_id")
            df = df.rename(columns={"industry_category": "sector"})
            try:  # 寫回快取
                INDUSTRY_CACHE_FILE.write_text(json.dumps({
                    "source": "FinMind TaiwanStockInfo",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "map": df.set_index("stock_id")["sector"].to_dict(),
                }, ensure_ascii=False), encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 產業分類快取寫入失敗({str(e)[:150]})。")
            print(f"[ind] 產業分類 {len(df)} 檔(FinMind 即時)。")
            return df
        print("[warn] FinMind 產業分類回傳空資料,改用快取備援。")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 產業分類抓取失敗({str(e)[:300]}),改用快取備援。")
    if INDUSTRY_CACHE_FILE.exists():
        try:
            m = json.loads(INDUSTRY_CACHE_FILE.read_text(encoding="utf-8"))                     .get("map", {})
            if m:
                df = pd.DataFrame([{"stock_id": k, "sector": v}
                                   for k, v in m.items()])
                print(f"[ind] 快取備援產業分類 {len(df)} 檔。")
                return df
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 產業分類快取讀取失敗({str(e)[:150]})。")
    print("[warn] 無產業分類備援,官方類股統計將為空。")
    return empty


def _mark(cell) -> bool:
    """期交所標的表用 ● / ◎ 表示「是」。"""
    s = str(cell)
    return ("●" in s) or ("◎" in s) or ("是" in s)


def fetch_futures_universe() -> pd.DataFrame:
    """
    期貨標的母體:即時抓取期交所「股票期貨/股票選擇權交易標的」表。

    只保留「是股票期貨標的」的證券,並依 ◎ 欄位判斷:
      kind   = 個股 / ETF
      market = 上市 / 上櫃
    大型/小型契約(2,000 股 vs 100 股)會對應同一證券,以代號去重。

    成功時將結果寫回 futures_universe.json(自動更新快取);
    失敗時改讀該檔備援,確保主管線不中斷。
    回傳欄位:stock_id, fut_name, kind, market。
    """
    try:
        taifex_headers = {
            **HEADERS,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9",
            "Referer": "https://www.taifex.com.tw/cht/2/stockLists",
        }
        r = requests.get(TAIFEX_STOCKLIST_URL, headers=taifex_headers, timeout=60)
        r.raise_for_status()
        r.encoding = "utf-8"
        tables = pd.read_html(StringIO(r.text))  # 需要 lxml;StringIO 相容 pandas 3.x
        target = None
        for t in tables:
            cols = ["".join(str(c).split()) for c in t.columns]
            if any("證券代號" in c for c in cols) and any("股票期貨" in c for c in cols):
                t.columns = cols
                target = t
                break
        if target is None:
            raise ValueError("找不到標的表格")

        def find(*keys):
            for c in target.columns:
                if all(k in c for k in keys):
                    return c
            return None

        c_id = find("證券代號")
        c_name = find("標的證券", "簡稱") or find("簡稱")
        c_fut = find("是否為", "股票期貨")
        c_ls = find("上市普通股")
        c_os = find("上櫃普通股")
        c_le = find("上市ETF")
        c_oe = find("上櫃ETF")

        rows, seen = [], set()
        for _, row in target.iterrows():
            if c_fut is not None and not _mark(row[c_fut]):
                continue
            sid = re.sub(r"\s", "", str(row[c_id]))
            if not SEC_ID_RE.match(sid) or sid in seen:
                continue
            if c_le is not None and _mark(row[c_le]):
                kind, market = "ETF", "上市"
            elif c_oe is not None and _mark(row[c_oe]):
                kind, market = "ETF", "上櫃"
            elif c_os is not None and _mark(row[c_os]):
                kind, market = "個股", "上櫃"
            else:
                kind, market = "個股", "上市"
            seen.add(sid)
            rows.append({"stock_id": sid,
                         "fut_name": str(row[c_name]).strip() if c_name else "",
                         "kind": kind, "market": market})
        if not rows:
            raise ValueError("解析後無資料")

        df = pd.DataFrame(rows)
        # 寫回快取(供前端顯示筆數與下次失敗時備援)
        try:
            payload = {
                "source": "TAIFEX stockLists 股票期貨標的",
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "count": len(df),
                "universe": [
                    {"stock_id": r.stock_id, "stock_name": r.fut_name,
                     "kind": r.kind, "market": r.market}
                    for r in df.itertuples()
                ],
            }
            FUTURES_UNIVERSE_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 期貨母體快取寫入失敗({str(e)[:300]}),不影響本次統計。")

        print(f"[futures] 期交所即時標的 {len(df)} 檔"
              f"(個股 {(df.kind == '個股').sum()} / ETF {(df.kind == 'ETF').sum()})")
        return df
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 期交所標的即時抓取失敗({str(e)[:300]}),改用 futures_universe.json 備援。")
        if FUTURES_UNIVERSE_FILE.exists():
            cache = json.loads(FUTURES_UNIVERSE_FILE.read_text(encoding="utf-8"))
            uni = cache.get("universe", [])
            df = pd.DataFrame(uni)
            if not df.empty:
                df = df.rename(columns={"stock_name": "fut_name"})
                print(f"[futures] 備援母體 {len(df)} 檔")
                return df[["stock_id", "fut_name", "kind", "market"]]
        print("[warn] 無備援母體,期貨標的統計將略過。")
        return pd.DataFrame(columns=["stock_id", "fut_name", "kind", "market"])


# --------------------------------------------------------------- 可轉債標的母體


def _discover_cb_api_paths() -> list[str]:
    """
    抓取 CB 清單頁 HTML + 其引用的 JS bundle,自動探測可能的
    /www/zh-tw/... JSON API 路徑(只保留與債券相關者)。
    新版官網的 API 路徑通常寫在外部 JS 檔內,故需一併掃描。
    """
    found: list[str] = []
    page_headers = {
        **HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Referer": "https://www.tpex.org.tw/zh-tw/index.html",
    }

    def scan(text: str):
        for p in re.findall(r"/www/zh-tw/[A-Za-z0-9_/\-]+", text):
            low = p.lower()
            if "bond" in low or "cb" in low:
                u = "https://www.tpex.org.tw" + p
                if u not in found:
                    found.append(u)

    try:
        r = requests.get(CB_LIST_PAGE, headers=page_headers, timeout=60)
        r.raise_for_status()
        scan(r.text)
        # 掃描頁面引用的同站 JS 檔
        srcs = re.findall(r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']', r.text)
        print(f"[cb] 清單頁引用 JS 檔 {len(srcs)} 個,逐一掃描...")
        for s in srcs[:12]:
            if s.startswith("//"):
                s = "https:" + s
            elif s.startswith("/"):
                s = "https://www.tpex.org.tw" + s
            elif not s.startswith("http"):
                s = "https://www.tpex.org.tw/" + s.lstrip("./")
            if "tpex.org.tw" not in s:
                continue
            try:
                js = requests.get(s, headers=page_headers, timeout=60)
                if js.status_code == 200:
                    scan(js.text)
                time.sleep(1)
            except Exception as e:  # noqa: BLE001
                print(f"[cb] JS 檔掃描失敗 {s}({str(e)[:300]})")
    except Exception as e:  # noqa: BLE001
        print(f"[cb] 清單頁原始碼探測失敗({str(e)[:300]})。")

    if found:
        print(f"[cb] 探測到債券相關 API 候選:{found}")
    else:
        print("[cb] 頁面與 JS 檔中均未探測到債券相關 API 路徑。")
    return found


def _discover_cb_openapi() -> list[str]:
    return _discover_openapi(("bond", "cb"), label="cb")


def _discover_openapi(keywords: tuple, label: str = "openapi") -> list[str]:
    """
    從 TPEx OpenAPI(https://www.tpex.org.tw/openapi/)規格檔中,
    尋找路徑名稱含任一 keyword 的端點,轉為完整 URL 候選。
    """
    base = "https://www.tpex.org.tw/openapi"
    spec_candidates = [f"{base}/swagger.json", f"{base}/openapi.json",
                       f"{base}/v1/swagger.json", f"{base}/apis/swagger.json"]
    try:  # 先從 Swagger UI 首頁找規格檔實際路徑
        r = requests.get(base + "/", headers=HEADERS, timeout=60)
        if r.status_code == 200:
            for m in re.findall(r'["\']([^"\']+\.(?:json|ya?ml))["\']', r.text):
                u = m if m.startswith("http") else (
                    "https://www.tpex.org.tw" + m if m.startswith("/")
                    else f"{base}/{m.lstrip('./')}")
                if u not in spec_candidates:
                    spec_candidates.insert(0, u)
        time.sleep(1)
    except Exception:  # noqa: BLE001
        pass

    for spec_url in spec_candidates:
        try:
            r = requests.get(spec_url, headers=HEADERS, timeout=60)
            time.sleep(1)
            if r.status_code != 200:
                continue
            spec = r.json()
            paths = spec.get("paths", {})
            if not paths:
                continue
            server = "https://www.tpex.org.tw/openapi/v1"
            servers = spec.get("servers") or []
            if servers and isinstance(servers[0], dict) and servers[0].get("url"):
                server = servers[0]["url"].rstrip("/")
                if server.startswith("/"):
                    server = "https://www.tpex.org.tw" + server
            hits = [server + p for p in paths
                    if any(k in p.lower() for k in keywords)]
            print(f"[{label}] OpenAPI 規格 {spec_url} 內符合 {keywords} 的端點:{hits}")
            return hits
        except Exception:  # noqa: BLE001
            continue
    print(f"[{label}] 未能取得 TPEx OpenAPI 規格檔。")
    return []


def _parse_cb_payload(payload) -> list[dict]:
    """
    解析 CB 清單回應,盡量涵蓋 TPEx 兩種常見格式:
      A. OpenAPI 風格:list[dict],鍵名含「債券代號 / 代號 / Code」
      B. 網站 JSON:tables/fields+data(沿用 find_table)
    回傳 [{cb_id, cb_name, stock_id}],stock_id = 債券代號前 4 碼。
    """
    recs = []

    def add(code, name):
        code = re.sub(r"\s", "", str(code))
        if not CB_ID_RE.match(code):
            return
        recs.append({"cb_id": code, "cb_name": str(name).strip(),
                     "stock_id": code[:4]})

    if isinstance(payload, list):                       # 格式 A
        keys_logged = False
        for row in payload:
            if not isinstance(row, dict):
                continue
            if not keys_logged:
                print(f"[cb] 回應為 list[dict],鍵名:{list(row.keys())}")
                keys_logged = True
            code = name = None
            # 第一輪:精確優先(債券代號),避免誤抓「股票代號」等欄位
            for k, v in row.items():
                kk = str(k)
                if code is None and ("債券代號" in kk or "BondCode" in kk
                                     or kk.lower() in ("code", "bond_code", "cb_id")):
                    code = v
                if name is None and ("債券簡稱" in kk or "債券名稱" in kk):
                    name = v
            # 第二輪:寬鬆備援
            if code is None:
                for k, v in row.items():
                    kk = str(k)
                    if "代號" in kk and "股票" not in kk and "標的" not in kk:
                        code = v
                        break
            if name is None:
                for k, v in row.items():
                    kk = str(k)
                    if ("簡稱" in kk or "名稱" in kk or "Name" in kk) \
                            and "股票" not in kk and "標的" not in kk:
                        name = v
                        break
            if code is not None:
                add(code, name or "")
        return recs

    if isinstance(payload, dict):                       # 格式 B
        fields, rows = find_table(payload, "債券代號")
        if not fields:
            fields, rows = find_table(payload, "代號")
        if fields:
            print(f"[cb] 解析欄位:{fields}")
            i_id = col_idx(fields, "債券代號", "代號")
            i_name = col_idx(fields, "債券簡稱", "簡稱", "名稱")
            for r in rows:
                if i_id is None or len(r) <= i_id:
                    continue
                name = r[i_name] if (i_name is not None and len(r) > i_name) else ""
                add(r[i_id], name)
    return recs


def fetch_cb_universe() -> pd.DataFrame:
    """
    可轉債標的母體:即時抓取櫃買中心「最近上櫃轉(交)換公司債」清單,
    以債券代號前 4 碼對回發行公司股票代號(例:15132 → 1513 中興電)。

    正確 API 端點尚待線上環境確認,策略:
      1. 依序嘗試 CB_API_CANDIDATES 候選端點
      2. 再嘗試從清單頁 HTML 自動探測到的 API 路徑
      3. 全部失敗 → 改讀 cb_universe.json 快取備援
    成功時將結果寫回 cb_universe.json(自動更新快取)。
    回傳每檔股票一列:stock_id, cb_ids(list), cb_names(list)。
    """
    empty = pd.DataFrame(columns=["stock_id", "cb_ids", "cb_names"])

    def group(recs: list[dict]) -> pd.DataFrame:
        df = pd.DataFrame(recs).drop_duplicates("cb_id")
        return (df.groupby("stock_id")
                  .agg(cb_ids=("cb_id", list), cb_names=("cb_name", list))
                  .reset_index())

    def try_url(url: str, params: dict | None) -> list[dict]:
        """單一端點嘗試:非 200 / 非 JSON 時印出回應片段供除錯。"""
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=60)
            time.sleep(REQ_INTERVAL)
        except Exception as e:  # noqa: BLE001
            print(f"[cb] {url} 請求失敗({str(e)[:300]}),換下一個候選。")
            return []
        if r.status_code != 200:
            print(f"[cb] {url} HTTP {r.status_code},換下一個候選。")
            return []
        try:
            payload = r.json()
        except ValueError:
            print(f"[cb] {url} 非 JSON 回應,前 150 字:{r.text[:150]!r}")
            return []
        got = _parse_cb_payload(payload)
        if not got:
            print(f"[cb] {url} 回應中無可辨識的債券清單。"
                  f"回應片段:{str(payload)[:200]!r}")
        return got

    recs = []
    # 第一輪:網站 JSON 候選(含頁面/JS 探測結果),帶 response=json 參數
    for url in dict.fromkeys(CB_API_CANDIDATES + _discover_cb_api_paths()):
        recs = try_url(url, {"response": "json"})
        if recs:
            print(f"[cb] ✔ {url} 解析成功:可轉債 {len(recs)} 檔。")
            break
    # 第二輪:TPEx OpenAPI 端點(直接回傳 JSON list,不需參數)
    if not recs:
        for url in _discover_cb_openapi():
            recs = try_url(url, None)
            if recs:
                print(f"[cb] ✔ {url} 解析成功:可轉債 {len(recs)} 檔。")
                break

    if recs:
        uni = group(recs)
        try:   # 寫回快取(供前端顯示與下次失敗時備援)
            CB_UNIVERSE_FILE.write_text(json.dumps({
                "source": "TPEx 最近上櫃轉(交)換公司債",
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "cb_count": len(recs),
                "stock_count": int(len(uni)),
                "bonds": recs,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 可轉債母體快取寫入失敗({str(e)[:300]}),不影響本次統計。")
        print(f"[cb] 母體 = 發債公司 {len(uni)} 檔(可轉債 {len(recs)} 檔)。")
        return uni

    print("[warn] 可轉債清單線上抓取全數失敗,改用 cb_universe.json 備援。")
    if CB_UNIVERSE_FILE.exists():
        try:
            bonds = json.loads(CB_UNIVERSE_FILE.read_text(encoding="utf-8")) \
                        .get("bonds", [])
            if bonds:
                uni = group(bonds)
                print(f"[cb] 備援母體:發債公司 {len(uni)} 檔(可轉債 {len(bonds)} 檔)。")
                return uni
        except Exception as e:  # noqa: BLE001
            print(f"[warn] cb_universe.json 讀取失敗({str(e)[:300]})。")
    print("[warn] 無可轉債備援母體,可轉債標的統計將略過。")
    return empty


# ------------------------------------------------------------- 日期與統計計算


# --------------------------------------------------------------- 外資持股比例


TPEX_FOREIGN_CANDIDATES = [
    # Actions log 已驗證:OpenAPI 端點鍵名含 PercentageOfSharesOC/FMIHeld
    "https://www.tpex.org.tw/openapi/v1/tpex_3insti_qfii",
    # Actions log 已驗證:網站端點回傳「僑外資及陸資持股比例排行表」
    "https://www.tpex.org.tw/www/zh-tw/insti/qfii",
    "https://www.tpex.org.tw/www/zh-tw/insti/qfiiStat",
    "https://www.tpex.org.tw/www/zh-tw/insti/qfiiPct",
]


def _parse_foreign_payload(payload) -> dict:
    """
    解析外資及陸資持股統計回應 → {stock_id: 全體外資持股比率(%)}。
    支援 OpenAPI list[dict] 與網站 fields/data 兩種格式。
    """
    out = {}
    if isinstance(payload, list):
        keys_logged = False
        for row in payload:
            if not isinstance(row, dict):
                continue
            if not keys_logged:
                print(f"[fore] 回應為 list[dict],鍵名:{list(row.keys())}")
                keys_logged = True
            code = ratio = None
            for k, v in row.items():
                kk = str(k)
                if ratio is None and ("持股比率" in kk or "持股比例" in kk
                                      or "SharesRatio" in kk
                                      or "PercentageOfShares" in kk) \
                        and "上限" not in kk and "尚可" not in kk \
                        and "Available" not in kk and "UpperLimit" not in kk:
                    ratio = v
                if code is None and ("代號" in kk or "Code" in kk):
                    code = v
            if code is None or ratio is None:
                continue
            sid = re.sub(r"\s", "", str(code))
            val = to_num(ratio)
            if SEC_ID_RE.match(sid) and val is not None:
                out[sid] = val
        return out
    if isinstance(payload, dict):
        fields, rows = find_table(payload, "證券代號")
        if not fields:
            fields, rows = find_table(payload, "代號")
        if not fields:
            return out
        i_id = col_idx(fields, "證券代號", "代號")
        i_ratio = col_idx(fields, "全體外資及陸資持股比率", "全體外資持股比率")
        if i_ratio is None:  # 備援:任何含「持股比率」且非上限/尚可的欄位
            for i, f in enumerate(fields):
                if ("持股比率" in f or "持股比例" in f) \
                        and "上限" not in f and "尚可" not in f:
                    i_ratio = i
                    break
        if i_id is None or i_ratio is None:
            print(f"[fore] 找不到持股比率欄位,欄位清單:{fields}")
            return out
        for r in rows:
            if len(r) <= max(i_id, i_ratio):
                continue
            sid = str(r[i_id]).strip()
            if not SEC_ID_RE.match(sid):
                continue
            val = to_num(r[i_ratio])
            if val is not None:
                out[sid] = val
        if not out and rows:
            print(f"[fore] 表格解析 0 筆。欄位:{fields}")
            print(f"[fore] 首列樣本:{rows[0]}")
    return out


def fetch_twse_foreign(d: date) -> dict:
    """證交所「外資及陸資投資持股統計」(MI_QFIIS)→ {stock_id: 持股比率%}。"""
    try:
        payload = http_get_json(
            "https://www.twse.com.tw/fund/MI_QFIIS",
            {"response": "json", "date": d.strftime("%Y%m%d"),
             "selectType": "ALLBUT0999"}, retries=2)
    except Exception as e:  # noqa: BLE001
        print(f"[fore] TWSE MI_QFIIS {d} 抓取失敗({str(e)[:200]})")
        return {}
    if not payload or payload.get("stat") not in (None, "OK"):
        return {}
    return _parse_foreign_payload(payload)


def fetch_tpex_foreign(d: date) -> dict:
    """
    櫃買中心「外資及陸資投資持股統計」→ {stock_id: 持股比率%}。
    正確端點待線上驗證:依序嘗試候選網站 API,再以 OpenAPI 規格檔探測備援。
    """
    for url in TPEX_FOREIGN_CANDIDATES:
        try:
            r = requests.get(url, params={"response": "json",
                                          "date": d.strftime("%Y/%m/%d")},
                             headers=HEADERS, timeout=60)
            time.sleep(REQ_INTERVAL)
        except Exception as e:  # noqa: BLE001
            print(f"[fore] {url} 請求失敗({str(e)[:200]})")
            continue
        if r.status_code != 200:
            print(f"[fore] {url} HTTP {r.status_code}")
            continue
        try:
            payload = r.json()
        except ValueError:
            print(f"[fore] {url} 非 JSON,前 100 字:{r.text[:100]!r}")
            continue
        got = _parse_foreign_payload(payload)
        if got:
            print(f"[fore] ✔ {url}:上櫃 {len(got)} 檔")
            return got
        print(f"[fore] {url} 無可辨識持股比率,片段:{str(payload)[:150]!r}")
    # OpenAPI 備援(回傳最新一日資料,不需日期參數)
    for url in _discover_openapi(("qfii",), label="fore"):
        try:
            r = requests.get(url, headers=HEADERS, timeout=60)
            time.sleep(REQ_INTERVAL)
            if r.status_code != 200:
                continue
            got = _parse_foreign_payload(r.json())
            if got:
                print(f"[fore] ✔ {url}(OpenAPI):上櫃 {len(got)} 檔")
                return got
        except Exception as e:  # noqa: BLE001
            print(f"[fore] {url} 失敗({str(e)[:200]})")
    print("[warn] 上櫃外資持股統計抓取失敗,上櫃標的外資比例將留空。")
    return {}


def fetch_foreign_ratios(t: date):
    """
    合併上市+上櫃外資持股比率,自動往回找最近一個已公布日。
    回傳 (dict{stock_id: 比率%}, 基準日字串或 None)。
    """
    for back in range(6):
        d = t - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        tw = fetch_twse_foreign(d)
        if not tw:
            print(f"[fore] {d} 上市外資持股統計尚未公布或無資料,往前一天...")
            continue
        merged = dict(tw)
        merged.update(fetch_tpex_foreign(d))
        print(f"[fore] 外資持股統計基準日 {d}:上市+上櫃共 {len(merged)} 檔")
        return merged, d.isoformat()
    print("[warn] 近日外資持股統計皆無法取得,外資比例欄將留空。")
    return {}, None


def safe_fetch(fn, d, label):
    """單日資料抓取失敗時記錄警告並跳過,不中斷整體統計。"""
    try:
        return fn(d)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {label} {d} 抓取失敗({str(e)[:300]}),略過該日該來源。")
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
                "foreign_ratio": num(r.get("foreign_ratio")),
                "trust_net_d": int(r["trust_net_daily"]) if not pd.isna(r["trust_net_daily"]) else 0,
                "value_w": num(r.get("value_wtd")),
                "value_m": num(r.get("value_mtd")),
                "inst_net_w": num(r.get("inst_net_wtd")),
                "inst_net_m": num(r.get("inst_net_mtd")),
                "fore_net_w": num(r.get("fore_net_wtd")),
                "fore_net_m": num(r.get("fore_net_mtd")),
                "trust_net_w": num(r.get("trust_net_wtd")),
                "trust_net_m": num(r.get("trust_net_mtd")),
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

    # 外資持股比例(上市 MI_QFIIS + 上櫃對應報表,自動回溯最近公布日)
    fore_ratios, fore_date = fetch_foreign_ratios(t)
    stats["foreign_ratio"] = stats["stock_id"].map(fore_ratios)

    # 各期間交易日數(供前端把期間加總換算為日均)
    period_days = {"daily": 1}
    for pkey in ("wtd", "mtd"):
        b = bases.get(pkey)
        period_days[pkey] = (int(px.loc[(px["date"] > b) &
                                        (px["date"] <= today_s), "date"].nunique())
                             if b else None)
    print(f"[+] 期間交易日數:{period_days}")
    # 主站沿用原母體(4 碼普通股);ETF 只用於期貨標的頁
    stats_main = stats[stats["stock_id"].str.match(r"^\d{4}$")]

    industry = fetch_industry_map()
    official = aggregate(stats_main, industry, "sector") if not industry.empty else []

    themes_cfg = {}
    themes_result = []
    if THEMES_FILE.exists():
        themes_cfg = json.loads(THEMES_FILE.read_text(encoding="utf-8"))
        rows = [{"stock_id": sid, "theme": theme}
                for theme, sids in themes_cfg.items() for sid in sids]
        themes_result = aggregate(stats_main, pd.DataFrame(rows), "theme")

    print("[6/6] 輸出 JSON...")
    OUTPUT_DIR.mkdir(exist_ok=True)
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": today_s,
        "base_dates": bases,
        "period_days": period_days,
        "note": "法人金額為估算值:買賣超股數 × 當日收盤價;foreign=外資、trust=投信、inst=三大法人合計;"
                "外資比例=全體外資及陸資持股比率(官方統計)",
        "foreign_ratio_date": fore_date,
        "official_sectors": official,
        "themes": themes_result,
    }
    (OUTPUT_DIR / "sector_stats.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUTPUT_DIR / f"sector_stats_{today_s}.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print("完成 → output/sector_stats.json")

    # ---------------------------------------------------- 期貨標的母體(另存)
    print("[+] 產出期貨標的統計...")
    universe = fetch_futures_universe()
    if universe.empty:
        print("[info] 期貨母體為空,略過 futures_stats.json。")
    else:
        fut_ids = set(universe["stock_id"])
        stats_fut = stats[stats["stock_id"].isin(fut_ids)]
        # 商品類型(個股/ETF)分組對照
        kind_map = universe[["stock_id", "kind"]].drop_duplicates("stock_id")
        fut_official = (aggregate(stats_fut, industry, "sector")
                        if not industry.empty else [])
        fut_themes = []
        if themes_cfg:
            rows = [{"stock_id": sid, "theme": theme}
                    for theme, sids in themes_cfg.items() for sid in sids]
            fut_themes = aggregate(stats_fut, pd.DataFrame(rows), "theme")
        fut_types = aggregate(stats_fut, kind_map, "kind")

        fut_result = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "trade_date": today_s,
            "base_dates": bases,
        "period_days": period_days,
            "universe_count": int(len(fut_ids)),
            "foreign_ratio_date": fore_date,
            "matched_count": int(stats_fut["stock_id"].nunique()),
            "note": "母體 = 期交所股票期貨標的(個股/ETF);法人金額為估算值:買賣超股數 × 當日收盤價",
            "official_sectors": fut_official,
            "themes": fut_themes,
            "product_types": fut_types,
        }
        (OUTPUT_DIR / "futures_stats.json").write_text(
            json.dumps(fut_result, ensure_ascii=False, indent=1), encoding="utf-8")
        (OUTPUT_DIR / f"futures_stats_{today_s}.json").write_text(
            json.dumps(fut_result, ensure_ascii=False), encoding="utf-8")
        print(f"完成 → output/futures_stats.json"
              f"(母體 {len(fut_ids)} 檔,命中行情 {stats_fut['stock_id'].nunique()} 檔)")

    # ---------------------------------------------------- 可轉債標的母體(另存)
    print("[+] 產出可轉債標的統計...")
    cb_uni = fetch_cb_universe()
    if cb_uni.empty:
        print("[info] 可轉債母體為空,略過 cb_stats.json。")
    else:
        cb_stock_ids = set(cb_uni["stock_id"])
        stats_cb = stats[stats["stock_id"].isin(cb_stock_ids)]

        # 市場別(上市/上櫃):以當日證交所行情是否出現該代號判斷
        twse_ids = {r["stock_id"] for r in today_rows}
        market_map = pd.DataFrame(
            [{"stock_id": sid, "market": ("上市" if sid in twse_ids else "上櫃")}
             for sid in cb_stock_ids])

        cb_official = (aggregate(stats_cb, industry, "sector")
                       if not industry.empty else [])
        cb_themes = []
        if themes_cfg:
            rows = [{"stock_id": sid, "theme": theme}
                    for theme, sids in themes_cfg.items() for sid in sids]
            cb_themes = aggregate(stats_cb, pd.DataFrame(rows), "theme")
        cb_markets = aggregate(stats_cb, market_map, "market")

        # 個股 → 發行中可轉債對照(供前端浮層顯示)
        cb_of_stock = {
            r.stock_id: [{"cb_id": i, "cb_name": n}
                         for i, n in zip(r.cb_ids, r.cb_names)]
            for r in cb_uni.itertuples()
        }
        cb_count = int(cb_uni["cb_ids"].apply(len).sum())

        cb_result = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "trade_date": today_s,
            "base_dates": bases,
        "period_days": period_days,
            "universe_count": int(len(cb_stock_ids)),
            "foreign_ratio_date": fore_date,
            "cb_count": cb_count,
            "matched_count": int(stats_cb["stock_id"].nunique()),
            "note": "母體 = 發行台灣可轉換公司債之上市櫃公司(依 TPEx 上櫃轉(交)換公司債"
                    "清單,債券代號前4碼對回股票代號);法人金額為估算值:買賣超股數 × 當日收盤價",
            "official_sectors": cb_official,
            "themes": cb_themes,
            "markets": cb_markets,
            "cb_of_stock": cb_of_stock,
        }
        (OUTPUT_DIR / "cb_stats.json").write_text(
            json.dumps(cb_result, ensure_ascii=False, indent=1), encoding="utf-8")
        (OUTPUT_DIR / f"cb_stats_{today_s}.json").write_text(
            json.dumps(cb_result, ensure_ascii=False), encoding="utf-8")
        print(f"完成 → output/cb_stats.json"
              f"(發債公司 {len(cb_stock_ids)} 檔 / 可轉債 {cb_count} 檔,"
              f"命中行情 {stats_cb['stock_id'].nunique()} 檔)")


if __name__ == "__main__":
    main()
