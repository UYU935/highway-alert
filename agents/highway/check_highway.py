#!/usr/bin/env python3
"""
高速道路 通行止め監視エージェント
データソース: JARTIC（日本道路交通情報センター）
対象路線: 中国道・山陽道（広島〜三次方面）
通知先: Discord Webhook（専用チャンネル）
"""

import json
import os
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── 設定 ──────────────────────────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

STATE_FILE = Path(os.environ.get("STATE_FILE", "state_highway.json"))
JST = timezone(timedelta(hours=9))

# JARTIC 高速道路コード（中国道 = 2007、山陽道 = 2006）
# r8 = 高速道路規制情報エンドポイント
JARTIC_TARGET_URL = "https://www.jartic.or.jp/d/traffic_info/r8/target.json"
JARTIC_DATA_URL   = "https://www.jartic.or.jp/d/traffic_info/r8/{timestamp}/s/301/{road_code}.json"

# 監視対象路線: {JARTICコード: 表示名}
TARGET_ROADS = {
    "2007": "中国自動車道",
    "2006": "山陽自動車道",
}

# 通行止めに関係するキーワード（JARTIC の規制種別テキストに含まれる語）
CLOSURE_KEYWORDS = ["通行止", "全面通行止"]

# 対象区間キーワード（広島〜三次間に関係するIC名）
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

# ─── JARTIC データ取得 ──────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; highway-agent/1.0)",
    "Referer": "https://www.jartic.or.jp/",
}


def fetch_json(url: str) -> dict | list:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as res:
        raw = res.read()
        # JARTIC は Shift-JIS のことがある
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("shift_jis", errors="replace")
        return json.loads(text)


def get_latest_timestamp() -> str:
    data = fetch_json(JARTIC_TARGET_URL)
    # {"r8": "202406261200"} のような形式
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, str) and re.match(r"\d{12}", v):
                return v
    raise RuntimeError(f"タイムスタンプ取得失敗: {data}")


def fetch_road_closures(timestamp: str, road_code: str) -> list[dict]:
    """指定路線の規制情報リストを返す"""
    url = JARTIC_DATA_URL.format(timestamp=timestamp, road_code=road_code)
    try:
        data = fetch_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []  # 規制なし
        raise
    # JARTIC のレスポンス構造: {"uptime": "...", "item": [...]}
    items = data.get("item", []) if isinstance(data, dict) else []
    return items


# ─── フィルタリング ────────────────────────────────────────────────────
def is_target_closure(item: dict) -> bool:
    """通行止め かつ 対象区間かどうかを判定する"""
    # 規制種別テキスト
    regulation = item.get("regulation", "") or item.get("type", "") or str(item)

    # 通行止めチェック
    if not any(kw in regulation for kw in CLOSURE_KEYWORDS):
        return False

    # 区間チェック（開始IC・終了IC）
    start = item.get("start", "") or ""
    end   = item.get("end", "")   or ""
    section = start + end + item.get("section", "")

    return any(kw in section for kw in TARGET_IC_KEYWORDS)


def extract_closures(items: list[dict]) -> list[dict]:
    return [item for item in items if is_target_closure(item)]


def item_to_key(item: dict) -> str:
    """重複検知用のユニークキー"""
    return f"{item.get('start','')}_{item.get('end','')}_{item.get('regulation','')}"


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
        encoding="utf-8"
    )


# ─── Discord 通知 ──────────────────────────────────────────────────────
def send_discord(road_name: str, item: dict) -> None:
    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    start      = item.get("start", "不明")
    end        = item.get("end", "不明")
    regulation = item.get("regulation", item.get("type", "通行止め"))
    reason     = item.get("reason", item.get("cause", ""))
    detail     = f"（理由: {reason}）" if reason else ""

    content = (
        f"🚨 **高速道路 通行止め通知** 🚨\n"
        f"🛣️ 路線：{road_name}\n"
        f"📍 区間：{start} ～ {end}\n"
        f"⛔ 規制：{regulation}{detail}\n"
        f"🕐 確認時刻：{now} JST\n"
        f"\n**通勤ルートへの影響を確認してください。**\n"
        f"迂回路または出発時刻の変更を検討してください。\n"
        f"🔗 詳細: https://www.jartic.or.jp/"
    )

    payload = json.dumps({"content": content}).encode("utf-8")
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

    # JARTIC 最新タイムスタンプ取得
    try:
        timestamp = get_latest_timestamp()
        print(f"  JARTIC タイムスタンプ: {timestamp}")
    except Exception as e:
        print(f"[ERROR] タイムスタンプ取得失敗: {e}")
        return

    # 現在の通行止めを収集
    current_closures: dict[str, dict] = {}  # key -> item
    for road_code, road_name in TARGET_ROADS.items():
        try:
            items = fetch_road_closures(timestamp, road_code)
            closures = extract_closures(items)
            print(f"  [{road_name}] 規制件数: {len(items)}, 対象通行止め: {len(closures)}")
            for item in closures:
                key = f"{road_code}_{item_to_key(item)}"
                current_closures[key] = {"road_name": road_name, "item": item}
        except Exception as e:
            print(f"  [{road_name}] データ取得エラー: {e}")

    # 前回状態と比較
    previous_keys = load_state()
    current_keys  = set(current_closures.keys())
    new_keys      = current_keys - previous_keys

    print(f"  前回: {len(previous_keys)}件, 今回: {len(current_keys)}件, 新規: {len(new_keys)}件")

    # 新規通行止めを通知
    for key in new_keys:
        entry = current_closures[key]
        road_name = entry["road_name"]
        item      = entry["item"]
        print(f"  [新規通行止め] {road_name}: {item.get('start')} ～ {item.get('end')}")
        try:
            send_discord(road_name, item)
            print(f"  Discord 通知送信済み")
        except Exception as e:
            print(f"  Discord 通知失敗: {e}")

    if not new_keys:
        print("  通知なし（変化なし）")

    # 状態を保存
    save_state(current_keys)
    print("チェック完了")


if __name__ == "__main__":
    main()
