# -*- coding: utf-8 -*-
"""
Telegram 收盤通知
=================
讀取 output/sector_stats.json,整理當日重點摘要後推播到 Telegram。

需要環境變數(GitHub Secrets 提供):
  TELEGRAM_BOT_TOKEN : BotFather 給的機器人 token
  TELEGRAM_CHAT_ID   : 你的聊天室 ID

設計原則:通知失敗不影響主管線(永遠以 exit 0 結束)。
"""

import json
import os
import sys
from pathlib import Path

import requests

STATS_FILE = Path(__file__).resolve().parent / "output" / "sector_stats.json"

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


def build_message(data: dict) -> str:
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
    lines.append("🔥 <b>強勢族群 TOP3</b>")
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

    # 背離提醒:漲但法人大賣 / 跌但法人大買
    div_up = [t for t in themes
              if (t["daily"]["avg_change_pct"] or 0) > 1
              and (t["daily"]["inst_net_value"] or 0) < -10 * 1e8]
    div_dn = [t for t in themes
              if (t["daily"]["avg_change_pct"] or 0) < -1
              and (t["daily"]["inst_net_value"] or 0) > 10 * 1e8]
    if div_up or div_dn:
        lines.append("")
        lines.append("⚠️ <b>價量籌碼背離</b>")
        for t in div_up:
            lines.append(f"・{t['name']} 漲 {pct(t['daily']['avg_change_pct'])} "
                         f"但法人賣超 {yi(t['daily']['inst_net_value'])}億")
        for t in div_dn:
            lines.append(f"・{t['name']} 跌 {pct(t['daily']['avg_change_pct'])} "
                         f"但法人買超 {yi(t['daily']['inst_net_value'])}億")

    lines.append("")
    lines.append("📱 完整熱力圖 → https://pjryan168.github.io/TWSE-Money-Flow/")
    return "\n".join(lines)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[info] 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID,略過通知。")
        return
    if not STATS_FILE.exists():
        print("[info] 找不到統計結果檔,略過通知。")
        return

    data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
    msg = build_message(data)
    if not msg:
        print("[info] 無題材資料,略過通知。")
        return

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=30,
        )
        ok = r.json().get("ok", False)
        print("通知已送出 ✅" if ok else f"[warn] Telegram 回應異常:{r.text[:300]}")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 通知送出失敗:{e}")


if __name__ == "__main__":
    main()
    sys.exit(0)
