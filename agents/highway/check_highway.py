#!/usr/bin/env python3
"""
高速道路 通行止め監視エージェント
データソース: iHighway（NEXCO）
対象路線: 中国道・山陽道（広島〜三次方面）
通知先: Discord Webhook（専用チャンネル）
"""

import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── 設定 ──────────────────────────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

STATE_FILE = Path(os.environ.get("STATE_FILE", "state_highway.json"))
JST = timezone(timedelta(hours=9))

# iHighway 交通情報 JSON
IHIGHWAY_TRAFFIC_URL = "https://ihighway.jp/datas/json/traffic.json"

# 中国道・山陽道を含むエリア
TARGET_AREA = "area07"

# 監視対象路線名（iHighway の roadName と一致させる）
# 広島〜三次の通勤ルートに関係する路線
TARGET_ROADS = {"中国道", "山陽道", "広島道"}

# 対象区間キーワード（広島〜三次間に関係するIC・JCT名）
# 広島道: 広島JCT ↔ 広島北IC
# 中国道: 広島北IC ↔ 三次東JCT（千代田JCT, 高田IC, 甲立IC, 吉田掛合IC 経由）
TARGET_IC_KEYWORDS = [
    "広島",
    "三次",
    "甲立",
    "高田",
    "千代田",
    "吉田",
    "志和",
    "西条",
    "東広島",
    "河内",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; highway-agent/1.0)",
    "Referer": "https://ihighway.jp/",
}


# ─── データ取得 ─────────────────────────────────────────────────────────
def fetch_traffic() -> dict:
    req = urllib.request.Request(IHIGHWAY_TRAFFIC_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.loads(res.read().decode("utf-8"))


# ─── フィルタリング ────────────────────────────────────────────────────
def is_target_closure(road_name: str, info: dict) -> bool:
    """対象路線かつ対象区間の通行止めかどうかを判定"""
    if road_name not in TARGET_ROADS:
        return False
    title = info.get("title", "")
    return any(kw in title for kw in TARGET_IC_KEYWORDS)


def extract_closures(data: dict) -> dict[str, dict]:
    """key（serialID）-> {road_name, info} の辞書を返す"""
    result = {}
    area_data = data.get(TARGET_AREA, {})
    closed_list = area_data.get("trafficInfo", {}).get("closed", [])

    for entry in closed_list:
        road_name = entry.get("roadName", "")
        for info in entry.get("info", []):
            if is_target_closure(road_name, info):
                serial_id = str(info.get("serialID", f"{road_name}_{info.get('title','')}"))
                result[serial_id] = {"road_name": road_name, "info": info}

    return result


# ─── State 管理 ────────────────────────────────────────────────────────
def load_state() -> set[str]:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return set(data.get("active_keys", []))
        except Exception:
            pass
    return set()


def save_state(active_keys: set[str]) -> None:
    STATE_FILE.write_text(
        json.dumps({"active_keys": list(active_keys)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ─── Discord 通知 ──────────────────────────────────────────────────────
def send_discord(road_name: str, info: dict) -> None:
    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    title     = info.get("title", "不明")
    reason    = info.get("reason", "")
    direction = info.get("direction", "")

    lines = [
        "🚨 **高速道路 通行止め通知** 🚨",
        f"🛣️ 路線：{road_name}",
        f"📍 区間：{title}",
    ]
    if direction:
        lines.append(f"🔀 方向：{direction}")
    if reason:
        lines.append(f"⚠️ 理由：{reason}")
    lines += [
        f"🕐 確認時刻：{now} JST",
        "",
        "**通勤ルートへの影響を確認してください。**",
        "迂回路または出発時刻の変更を検討してください。",
        "🔗 詳細: https://ihighway.jp/pcsite/",
    ]

    payload = json.dumps({"content": "\n".join(lines)}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        if res.status not in (200, 204):
            raise RuntimeError(f"Discord webhook 失敗: {res.status}")


# ─── メイン処理 ────────────────────────────────────────────────────────
def main():
    now_str = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    print(f"[{now_str}] 高速道路通行止めチェック開始")

    # iHighway から交通情報取得
    try:
        data = fetch_traffic()
        print("  iHighway データ取得成功")
    except Exception as e:
        print(f"[ERROR] データ取得失敗: {e}")
        return

    # 対象通行止めを抽出
    current_closures = extract_closures(data)
    current_keys = set(current_closures.keys())
    print(f"  対象通行止め件数: {len(current_keys)}")

    # 前回状態と比較
    previous_keys = load_state()
    new_keys = current_keys - previous_keys
    print(f"  前回: {len(previous_keys)}件, 今回: {len(current_keys)}件, 新規: {len(new_keys)}件")

    # 新規通行止めを通知
    for key in new_keys:
        entry = current_closures[key]
        road_name = entry["road_name"]
        info      = entry["info"]
        print(f"  [新規通行止め] {road_name}: {info.get('title')}")
        try:
            send_discord(road_name, info)
            print("  Discord 通知送信済み")
        except Exception as e:
            print(f"  Discord 通知失敗: {e}")

    if not new_keys:
        print("  通知なし（変化なし）")

    # 状態を保存
    save_state(current_keys)
    print("チェック完了")


if __name__ == "__main__":
    main()
