# -*- coding: utf-8 -*-
"""
可轉債(CB)資料端點探測腳本
============================
在 GitHub Actions(網路暢通環境)手動執行,用來找出 TPEx
「最近上櫃轉(交)換公司債」清單的正確 JSON API 端點與欄位結構。

不需要 pandas,只用 requests;約 1-2 分鐘跑完。
執行後把 Actions log 全文貼回給 Claude 即可完成端點確認。
"""

import json
import re
import time

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Language": "zh-TW,zh;q=0.9",
}
PAGE_HEADERS = {
    **HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.tpex.org.tw/zh-tw/index.html",
}
CB_PAGES = [
    "https://www.tpex.org.tw/zh-tw/bond/issue/cbond/listed.html",
    "https://www.tpex.org.tw/en-us/bond/issue/cbond/listed.html",
]
CANDIDATES = [
    "https://www.tpex.org.tw/www/zh-tw/bond/issue/cbond/listed",
    "https://www.tpex.org.tw/www/zh-tw/bond/cbond/listed",
    "https://www.tpex.org.tw/www/zh-tw/bond/cbondListed",
    "https://www.tpex.org.tw/www/zh-tw/bond/cbList",
    "https://www.tpex.org.tw/www/zh-tw/bond/cbListed",
    "https://www.tpex.org.tw/www/zh-tw/bond/cbIssue",
    "https://www.tpex.org.tw/www/zh-tw/bond/publish/listed",
]


def sep(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def show_response(url, params=None):
    """印出單一端點的狀態碼、Content-Type 與回應片段/結構。"""
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=60)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {url} 請求例外:{e}")
        return
    ct = r.headers.get("Content-Type", "?")
    print(f"  → {r.url}\n    HTTP {r.status_code} | Content-Type: {ct}")
    try:
        payload = r.json()
    except ValueError:
        print(f"    非 JSON。回應前 300 字:{r.text[:300]!r}")
        return
    if isinstance(payload, list):
        print(f"    JSON list,共 {len(payload)} 筆。")
        if payload and isinstance(payload[0], dict):
            print(f"    第一筆鍵名:{list(payload[0].keys())}")
            print(f"    第一筆內容:{json.dumps(payload[0], ensure_ascii=False)[:400]}")
    elif isinstance(payload, dict):
        print(f"    JSON dict,頂層鍵:{list(payload.keys())}")
        tables = payload.get("tables")
        if isinstance(tables, list):
            for i, t in enumerate(tables):
                if isinstance(t, dict):
                    print(f"    tables[{i}] fields:{t.get('fields')}")
                    data = t.get("data") or []
                    if data:
                        print(f"    tables[{i}] 第一列:{data[0]}")
        else:
            print(f"    內容前 400 字:{json.dumps(payload, ensure_ascii=False)[:400]}")
    time.sleep(2)


def main():
    discovered = []

    # 1. 掃描清單頁 HTML 與其 JS bundle,列出所有 /www/zh-tw/ 路徑(不過濾)
    sep("STEP 1|清單頁 HTML + JS bundle 掃描")
    for page in CB_PAGES:
        try:
            r = requests.get(page, headers=PAGE_HEADERS, timeout=60)
            print(f"{page} → HTTP {r.status_code},HTML {len(r.text)} 字")
            if r.status_code != 200:
                continue
            paths = list(dict.fromkeys(
                re.findall(r"/www/[a-z\-]+/[A-Za-z0-9_/\-]+", r.text)))
            print(f"  HTML 內 /www/ 路徑:{paths or '(無)'}")
            srcs = re.findall(r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']',
                              r.text)
            print(f"  引用 JS 檔:{srcs}")
            for s in srcs[:15]:
                if s.startswith("//"):
                    s = "https:" + s
                elif s.startswith("/"):
                    s = "https://www.tpex.org.tw" + s
                elif not s.startswith("http"):
                    s = "https://www.tpex.org.tw/" + s.lstrip("./")
                if "tpex.org.tw" not in s:
                    continue
                try:
                    js = requests.get(s, headers=PAGE_HEADERS, timeout=60)
                    jpaths = list(dict.fromkeys(
                        re.findall(r"/www/[a-z\-]+/[A-Za-z0-9_/\-]+", js.text)))
                    hits = [p for p in jpaths
                            if "bond" in p.lower() or "cb" in p.lower()]
                    print(f"  {s} → HTTP {js.status_code},"
                          f"債券相關路徑:{hits or '(無)'}"
                          f",全部路徑數:{len(jpaths)}")
                    if jpaths and not hits:
                        print(f"    (全部路徑供參考:{jpaths[:40]})")
                    discovered += ["https://www.tpex.org.tw" + p for p in hits]
                    time.sleep(1)
                except Exception as e:  # noqa: BLE001
                    print(f"  {s} 掃描失敗:{e}")
        except Exception as e:  # noqa: BLE001
            print(f"{page} 抓取失敗:{e}")
        time.sleep(2)

    # 2. TPEx OpenAPI 規格檔:列出所有含 bond/cb 的端點
    sep("STEP 2|TPEx OpenAPI 規格檔掃描")
    base = "https://www.tpex.org.tw/openapi"
    spec_urls = [f"{base}/swagger.json", f"{base}/openapi.json",
                 f"{base}/v1/swagger.json", f"{base}/apis/swagger.json"]
    try:
        r = requests.get(base + "/", headers=PAGE_HEADERS, timeout=60)
        print(f"{base}/ → HTTP {r.status_code}")
        if r.status_code == 200:
            for m in re.findall(r'["\']([^"\']+\.(?:json|ya?ml))["\']', r.text):
                u = m if m.startswith("http") else (
                    "https://www.tpex.org.tw" + m if m.startswith("/")
                    else f"{base}/{m.lstrip('./')}")
                if u not in spec_urls:
                    spec_urls.insert(0, u)
            print(f"  首頁中發現的規格檔參照:{spec_urls[:6]}")
    except Exception as e:  # noqa: BLE001
        print(f"{base}/ 抓取失敗:{e}")
    time.sleep(2)

    openapi_hits = []
    for su in spec_urls:
        try:
            r = requests.get(su, headers=HEADERS, timeout=60)
            if r.status_code != 200:
                print(f"  {su} → HTTP {r.status_code}")
                continue
            spec = r.json()
            paths = spec.get("paths", {})
            print(f"  ✔ {su} 取得規格,共 {len(paths)} 個端點。")
            server = "https://www.tpex.org.tw/openapi/v1"
            servers = spec.get("servers") or []
            if servers and isinstance(servers[0], dict) and servers[0].get("url"):
                server = servers[0]["url"].rstrip("/")
                if server.startswith("/"):
                    server = "https://www.tpex.org.tw" + server
            print(f"  server base:{server}")
            for p, meta in paths.items():
                low = p.lower()
                if "bond" in low or "cb" in low:
                    summ = ""
                    if isinstance(meta, dict):
                        get = meta.get("get") or {}
                        summ = get.get("summary") or get.get("description") or ""
                    print(f"    債券相關端點:{p}  |  {summ[:60]}")
                    openapi_hits.append(server + p)
            break
        except Exception as e:  # noqa: BLE001
            print(f"  {su} 失敗:{e}")
        finally:
            time.sleep(2)

    # 3. 逐一實測:手動候選 + 探測所得 + OpenAPI 端點
    sep("STEP 3|端點實測(含回應結構)")
    for url in dict.fromkeys(CANDIDATES + discovered):
        show_response(url, {"response": "json"})
    for url in dict.fromkeys(openapi_hits):
        show_response(url, None)

    sep("探測完成。請將本 log 全文貼回給 Claude。")


if __name__ == "__main__":
    main()
