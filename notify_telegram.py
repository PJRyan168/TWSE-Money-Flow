# -*- coding: utf-8 -*-
"""
Telegram 收盤通知
=================
推播三則獨立訊息:
  1. 盤後族群熱力    ← output/sector_stats.json
  2. 期貨標的熱力    ← output/futures_stats.json(母體 = 期交所股票期貨標的)
  3. 可轉債標的熱力  ← output/cb_stats.json(母體 = 發行可轉債之上市櫃公司)

需要環境變數(GitHub Secrets 提供):
  TELEGRAM_BOT_TOKEN : BotFather 給的機器人 token
  TELEGRAM_CHAT_ID   : 你的聊天室 ID

設計原則:通知失敗不影響主管線(永遠以 exit 0 結束);任一則無資料則略過該則。
"""

import json
import os
import sys
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
STATS_FILE = BASE / "output" / "sector_stats.json"
FUT_STATS_FILE = BASE / "output" / "futures_stats.json"
CB_STATS_FILE = BASE / "output" / "cb_stats.json"
SITE = "https://pjryan168.github.io/TWSE-Money-Flow"

W = {"chg": 0.42, "value": 0.28, "inst": 0.18, "breadth": 0.12}  # 複合強度權重


def composite_rank(items: list[dict], period: str = "daily") -> list[dict]:
    """與儀表板相同的複合強度計分(組內排名正規化)。"""
    def rank_scores(key):
        vals = [(items[i][period].get(key) if items[i][period].get(key) is not None
                 else float("-inf"), i) for i in range(len(items))]
        vals.sort()
        s = [0.0] * len(items)
        n = max(len(items) - 1, 1)
        for r, (_, i) in enumerate(vals):
            s[i] = r / n
        return s

    r1 = rank_scores("avg_change_pct")
    r2 = rank_scores("trading_value")
    r3 = rank_scores("inst_net_value")
    r4 = rank_scores("up_ratio")
    scored = []
    for i, x in enumerate(items):
        score = (W["chg"] * r1[i] + W["value"] * r2[i]
                 + W["inst"] * r3[i] + W["breadth"] * r4[i])
        scored.append({**x, "_score": score})
    return sorted(scored, key=lambda x: x["_score"], reverse=True)


def yi(v) -> str:
    return "—" if v is None else f"{v / 1e8:+.1f}"


def pct(v) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def _divergence_lines(themes: list[dict]) -> list[str]:
    """價量籌碼背離:漲但法人大賣 / 跌但法人大買。"""
    div_up = [t for t in themes
              if (t["daily"]["avg_change_pct"] or 0) > 1
              and (t["daily"].get("inst_net_value") or 0) < -10 * 1e8]
    div_dn = [t for t in themes
              if (t["daily"]["avg_change_pct"] or 0) < -1
              and (t["daily"].get("inst_net_value") or 0) > 10 * 1e8]
    lines = []
    if div_up or div_dn:
        lines.append("")
        lines.append("⚠️ <b>價量籌碼背離</b>")
        for t in div_up:
            lines.append(f"・{t['name']} 漲 {pct(t['daily']['avg_change_pct'])} "
                         f"但法人賣超 {yi(t['daily']['inst_net_value'])}億")
        for t in div_dn:
            lines.append(f"・{t['name']} 跌 {pct(t['daily']['avg_change_pct'])} "
                         f"但法人買超 {yi(t['daily']['inst_net_value'])}億")
    return lines


def build_message(data: dict) -> str:
    """盤後族群(全市場)摘要。"""
    themes = [x for x in data.get("themes", [])
              if x.get("daily", {}).get("avg_change_pct") is not None]
    if not themes:
        return ""

    ranked = composite_rank(themes)
    top3 = ranked[:3]
    weak = min(themes, key=lambda x: x["daily"]["avg_change_pct"])
    inst_buy = max(themes, key=lambda x: x["daily"].get("inst_net_value") or 0)
    inst_sell = min(themes, key=lambda x: x["daily"].get("inst_net_value") or 0)

    lines = [f"📊 <b>盤後族群熱力|{data.get('trade_date', '')}</b>", ""]
    lines.append("🔥 <b>資金焦點 TOP3</b>  <i>(漲幅+量能+法人+齊漲綜合)</i>")
    medals = ["🥇", "🥈", "🥉"]
    for i, t in enumerate(top3):
        d = t["daily"]
        lines.append(
            f"{medals[i]} {t['name']}  {pct(d['avg_change_pct'])}"
            f"|法人 {yi(d['inst_net_value'])}億|齊漲 {d['up_ratio']}%")
    lines.append("")
    lines.append(f"💰 法人最捧:{inst_buy['name']} "
                 f"{yi(inst_buy['daily']['inst_net_value'])}億")
    lines.append(f"🧊 法人最倒:{inst_sell['name']} "
                 f"{yi(inst_sell['daily']['inst_net_value'])}億")
    lines.append(f"📉 最弱族群:{weak['name']} {pct(weak['daily']['avg_change_pct'])}")

    lines += _divergence_lines(themes)
    lines.append("")
    lines.append(f"📱 完整熱力圖 → {SITE}/")
    return "\n".join(lines)


def build_futures_message(data: dict) -> str:
    """期貨標的(個股/ETF)摘要。"""
    themes = [x for x in data.get("themes", [])
              if x.get("daily", {}).get("avg_change_pct") is not None]

    lines = [f"⚙️ <b>期貨標的熱力|{data.get('trade_date', '')}</b>",
             f"<i>母體:期交所股票期貨標的 {data.get('universe_count', '—')} 檔"
             f"(命中行情 {data.get('matched_count', '—')})</i>", ""]

    # 商品類型:個股 vs ETF 當日表現
    for pt in data.get("product_types", []):
        d = pt.get("daily", {})
        if d.get("avg_change_pct") is None:
            continue
        icon = "📈" if pt["name"] == "個股" else "🧺"
        lines.append(f"{icon} {pt['name']}({pt['stock_count']}檔)  "
                     f"{pct(d['avg_change_pct'])}|法人 {yi(d.get('inst_net_value'))}億"
                     f"|齊漲 {d.get('up_ratio')}%")

    # 焦點產業 TOP3(僅計期貨標的成分)
    if themes:
        lines.append("")
        lines.append("🔥 <b>期貨標的族群 TOP3</b>  <i>(漲幅+量能+法人+齊漲綜合)</i>")
        medals = ["🥇", "🥈", "🥉"]
        for i, t in enumerate(composite_rank(themes)[:3]):
            d = t["daily"]
            lines.append(
                f"{medals[i]} {t['name']}  {pct(d['avg_change_pct'])}"
                f"|法人 {yi(d['inst_net_value'])}億|齊漲 {d['up_ratio']}%")

        weak = min(themes, key=lambda x: x["daily"]["avg_change_pct"])
        inst_buy = max(themes, key=lambda x: x["daily"].get("inst_net_value") or 0)
        inst_sell = min(themes, key=lambda x: x["daily"].get("inst_net_value") or 0)
        lines.append("")
        lines.append(f"💰 法人最捧:{inst_buy['name']} "
                     f"{yi(inst_buy['daily']['inst_net_value'])}億")
        lines.append(f"🧊 法人最倒:{inst_sell['name']} "
                     f"{yi(inst_sell['daily']['inst_net_value'])}億")
        lines.append(f"📉 最弱族群:{weak['name']} {pct(weak['daily']['avg_change_pct'])}")
        lines += _divergence_lines(themes)

    if len(lines) <= 3:  # 只有標頭,無任何內容
        return ""
    lines.append("")
    lines.append(f"📱 期貨標的熱力圖 → {SITE}/futures.html")
    return "\n".join(lines)


def build_cb_message(data: dict) -> str:
    """可轉債標的(發債公司股票)摘要。"""
    themes = [x for x in data.get("themes", [])
              if x.get("daily", {}).get("avg_change_pct") is not None]

    lines = [f"🧾 <b>可轉債標的熱力|{data.get('trade_date', '')}</b>",
             f"<i>母體:發行可轉債之上市櫃公司 {data.get('universe_count', '—')} 檔"
             f"(可轉債 {data.get('cb_count', '—')} 檔,"
             f"命中行情 {data.get('matched_count', '—')})</i>", ""]

    # 市場別:上市 vs 上櫃發債公司當日表現
    for mk in data.get("markets", []):
        d = mk.get("daily", {})
        if d.get("avg_change_pct") is None:
            continue
        icon = "🏛️" if mk["name"] == "上市" else "🏪"
        lines.append(f"{icon} {mk['name']}({mk['stock_count']}檔)  "
                     f"{pct(d['avg_change_pct'])}|法人 {yi(d.get('inst_net_value'))}億"
                     f"|齊漲 {d.get('up_ratio')}%")

    # 焦點產業 TOP3(僅計可轉債標的成分)
    if themes:
        lines.append("")
        lines.append("🔥 <b>可轉債標的族群 TOP3</b>  <i>(漲幅+量能+法人+齊漲綜合)</i>")
        medals = ["🥇", "🥈", "🥉"]
        for i, t in enumerate(composite_rank(themes)[:3]):
            d = t["daily"]
            lines.append(
                f"{medals[i]} {t['name']}  {pct(d['avg_change_pct'])}"
                f"|法人 {yi(d['inst_net_value'])}億|齊漲 {d['up_ratio']}%")

        weak = min(themes, key=lambda x: x["daily"]["avg_change_pct"])
        inst_buy = max(themes, key=lambda x: x["daily"].get("inst_net_value") or 0)
        inst_sell = min(themes, key=lambda x: x["daily"].get("inst_net_value") or 0)
        lines.append("")
        lines.append(f"💰 法人最捧:{inst_buy['name']} "
                     f"{yi(inst_buy['daily']['inst_net_value'])}億")
        lines.append(f"🧊 法人最倒:{inst_sell['name']} "
                     f"{yi(inst_sell['daily']['inst_net_value'])}億")
        lines.append(f"📉 最弱族群:{weak['name']} {pct(weak['daily']['avg_change_pct'])}")
        lines += _divergence_lines(themes)

    if len(lines) <= 3:  # 只有標頭,無任何內容
        return ""
    lines.append("")
    lines.append(f"📱 可轉債標的熱力圖 → {SITE}/cb.html")
    return "\n".join(lines)


def send(token: str, chat_id: str, msg: str, label: str) -> None:
    if not msg:
        print(f"[info] {label}:無資料,略過。")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=30,
        )
        ok = r.json().get("ok", False)
        print(f"{label} 已送出 ✅" if ok else f"[warn] {label} 回應異常:{r.text[:300]}")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {label} 送出失敗:{e}")


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[info] 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID,略過通知。")
        return

    sector = _load(STATS_FILE)
    if sector:
        send(token, chat_id, build_message(sector), "族群通知")
    else:
        print("[info] 找不到 sector_stats.json,略過族群通知。")

    futures = _load(FUT_STATS_FILE)
    if futures:
        send(token, chat_id, build_futures_message(futures), "期貨標的通知")
    else:
        print("[info] 找不到 futures_stats.json,略過期貨標的通知。")

    cb = _load(CB_STATS_FILE)
    if cb:
        send(token, chat_id, build_cb_message(cb), "可轉債標的通知")
    else:
        print("[info] 找不到 cb_stats.json,略過可轉債標的通知。")


if __name__ == "__main__":
    main()
    sys.exit(0)
