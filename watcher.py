#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
伊東園ホテルズ 空室監視ツール
============================

対象ページ（ユーザー確認済みの実URL）:
  https://www5.489pro.com/asp/g/c/calendar.asp?kp=itoen&ty=&sp=&lan=JPN

やること：
  1. Playwright(ヘッドレスブラウザ)で上記の空室検索フォームを開く
  2. 「エリアまたは施設」プルダウンは1回につき1施設しか選べないため、
     監視したいホテルの数だけ検索を繰り返す
  3. 指定した宿泊日・人数構成（大人1名×2部屋、など）で検索実行
  4. 検索結果ページのスクリーンショットを保存し、テキストから
     空室有無を推定する
  5. 前回チェック時と比較し、「空きなし→空きあり」に変わったら
     ntfy.sh / Discordで通知する

前提：
  pip install playwright
  playwright install chromium

【重要な注意】
  - 「エリアまたは施設」プルダウンは単一選択なので、3施設を見るには
    このスクリプトは内部で3回検索を実行します（1回の実行で3施設分
    チェックします）。
  - 検索結果ページの正確なDOM構造は未確認のため、空室判定は
    「テキストからの正規表現抽出」＋「念のためスクリーンショット保存」
    の二段構えにしています。最初のうちはスクリーンショットも
    目視で確認し、誤判定がないか確かめてください。
  - このスクリプトは予約の自動化はしません。空きを見つけて通知する
    だけです。予約は必ず自分の手で行ってください。
  - アクセス間隔は15〜30分に1回程度を目安にしてください。
"""

import json
import re
import smtplib
import sys
import urllib.request
from dataclasses import dataclass, asdict
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ============================================================
# CONFIG - ここを自分の状況に合わせて書き換える
# ============================================================

SEARCH_URL = "https://www5.489pro.com/asp/g/c/calendar.asp?kp=itoen&ty=&sp=&lan=JPN"

# 「エリアまたは施設」プルダウンの value 属性（実ページのHTMLから確認済み）
HOTEL_VALUES = {
    "伊東園ホテル": "y22440801_a15",
    "伊東園ホテル別館": "y22440804_a15",
    "伊東園ホテル松川館": "y22440838_a15",
}

# 監視したいホテル名（上のHOTEL_VALUESのキーと一致させる）
TARGET_HOTELS = list(HOTEL_VALUES.keys())
TARGET_DATE = "2026-07-25"

# 人数・部屋構成
ADULTS_PER_ROOM = 1   # 「大人(1部屋あたり)」の数
NIGHTS = 1            # 泊数
ROOMS = 1             # 部屋数（大人1名・1部屋で検索）

# 空室ありとみなす記号／キーワード
AVAILABLE_MARKS = {"○", "◎", "△"}
FULL_MARKS = {"×", "－", "―", "満室", "×満室"}

# 通知方法: "ntfy" (お手軽・無料・登録不要) / "discord" / "email" (Gmail経由)
NOTIFY_METHOD = "email"
NTFY_TOPIC = "itoen-watch-CHANGE-ME"
DISCORD_WEBHOOK_URL = ""

# --- email(Gmail)を使う場合 ---
# 送信元Gmailアカウントと、Googleの「アプリパスワード」（通常のログイン
# パスワードとは別物）が必要。取得方法はREADME.md参照。
GMAIL_ADDRESS = "your-sending-account@gmail.com"   # 送信元アカウント
GMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"          # 16桁のアプリパスワード
EMAIL_TO = "zakusenwig@gmail.com"                   # 通知の送り先

# 状態保存・スクリーンショット保存先
STATE_FILE = Path(__file__).parent / "state.json"
SHOT_DIR = Path(__file__).parent / "screenshots"
SHOT_DIR.mkdir(exist_ok=True)


# ============================================================
# 通知処理
# ============================================================

def notify(message: str) -> None:
    print(f"[NOTIFY] {message}")
    try:
        if NOTIFY_METHOD == "ntfy":
            req = urllib.request.Request(
                url=f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": "伊東園ホテルズ 空室あり".encode("utf-8")},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        elif NOTIFY_METHOD == "discord" and DISCORD_WEBHOOK_URL:
            payload = json.dumps({"content": message}).encode("utf-8")
            req = urllib.request.Request(
                url=DISCORD_WEBHOOK_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        elif NOTIFY_METHOD == "email":
            msg = MIMEText(message)
            msg["Subject"] = "伊東園ホテルズ 空室あり"
            msg["From"] = GMAIL_ADDRESS
            msg["To"] = EMAIL_TO
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
                server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                server.sendmail(GMAIL_ADDRESS, [EMAIL_TO], msg.as_string())
        else:
            print("通知方法が正しく設定されていません。CONFIGを確認してください。")
    except Exception as e:
        print(f"通知の送信に失敗しました: {e}")


# ============================================================
# 状態の保存・読み込み
# ============================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================
# スクレイピング本体
# ============================================================

@dataclass
class CheckResult:
    hotel: str
    date: str
    available: bool
    raw_mark: str
    screenshot: str


def run_search_for_hotel(page, hotel_name: str, date_str: str) -> CheckResult:
    page.goto(SEARCH_URL, wait_until="networkidle", timeout=30000)

    y, m, d = date_str.split("-")
    m, d = str(int(m)), str(int(d))  # 先頭ゼロを落とす（"07"→"7"など、表記ゆれ対策）

    try:
        # --- 宿泊日（実体はテキスト入力欄。datepicker(thickbox)がついているが
        #     .fill()で直接値をセットすればポップアップは開かず干渉しない）---
        page.fill("#s_year", y)
        page.fill("#s_month", m)
        page.fill("#s_day", d)

        # --- エリアまたは施設（表示テキストに全角スペースが入っているため
        #     labelではなくvalue属性で選択する）---
        hotel_value = HOTEL_VALUES.get(hotel_name)
        if not hotel_value:
            raise ValueError(f"HOTEL_VALUESに「{hotel_name}」の定義がありません。")
        page.select_option("select[name='area_yado_id']", value=hotel_value)

        # --- 人数・泊数・部屋数 ---
        page.select_option("select[name='obj_per_num']", value=str(ADULTS_PER_ROOM))
        page.select_option("select[name='obj_stay_num']", value=str(NIGHTS))
        page.select_option("select[name='obj_room_num']", value=str(ROOMS))
    except Exception as e:
        # うまく選べなかった場合は、原因調査用にその時点の画面とHTMLを保存する
        safe_name = re.sub(r"[^\w]", "_", hotel_name)
        debug_png = SHOT_DIR / f"DEBUG_{safe_name}.png"
        debug_html = SHOT_DIR / f"DEBUG_{safe_name}.html"
        try:
            page.screenshot(path=str(debug_png), full_page=True)
            debug_html.write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError(
            f"フォーム入力に失敗しました。{debug_png} と {debug_html} を確認してください。"
            f" 元のエラー: {e}"
        )

    # --- 検索実行 ---
    # 実際のボタンは <input type="button" value="この条件で空室状況を表示" ...>
    search_button = page.locator("input[value='この条件で空室状況を表示']").first
    search_button.click()
    page.wait_for_load_state("networkidle", timeout=30000)

    # --- 結果を保存・判定 ---
    # 結果ページの構造がまだ確認できていないため、判定用の正規表現が外れても
    # 後から見返せるよう、スクリーンショットとHTMLを毎回保存しておく
    safe_name = re.sub(r"[^\w]", "_", hotel_name)
    shot_path = SHOT_DIR / f"{safe_name}_{date_str}.png"
    html_path = SHOT_DIR / f"{safe_name}_{date_str}.html"
    page.screenshot(path=str(shot_path), full_page=True)
    html_path.write_text(page.content(), encoding="utf-8")

    body_text = page.locator("body").inner_text()

    day_int = int(d)
    pattern = re.compile(rf"{day_int}\D{{0,6}}([○◎△×－―]|満室)")
    match = pattern.search(body_text)

    if not match:
        return CheckResult(hotel=hotel_name, date=date_str, available=False,
                            raw_mark="不明", screenshot=str(shot_path))

    mark = match.group(1)
    is_available = mark in AVAILABLE_MARKS
    return CheckResult(hotel=hotel_name, date=date_str, available=is_available,
                        raw_mark=mark, screenshot=str(shot_path))


def run_once() -> None:
    state = load_state()
    changed_any = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="ja-JP")

        for hotel_name in TARGET_HOTELS:
            key = f"{hotel_name}_{TARGET_DATE}"
            try:
                result = run_search_for_hotel(page, hotel_name, TARGET_DATE)
            except Exception as e:
                print(f"[ERROR] {hotel_name} のチェック中にエラー: {e}")
                continue

            prev_available = state.get(key, {}).get("available", False)
            print(f"{hotel_name} / {TARGET_DATE}: 記号={result.raw_mark} "
                  f"空きあり={result.available} (前回: {prev_available}) "
                  f"[{result.screenshot}]")

            if result.available and not prev_available:
                notify(
                    f"{hotel_name} {TARGET_DATE} 大人{ADULTS_PER_ROOM}名×{ROOMS}部屋"
                    f" の空室が見つかりました！（記号: {result.raw_mark}）\n{SEARCH_URL}"
                )
                changed_any = True

            state[key] = asdict(result)

        browser.close()

    save_state(state)

    if not changed_any:
        print("変化なし。")


if __name__ == "__main__":
    run_once()

