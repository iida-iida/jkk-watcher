#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JKKねっと「あき家検索」自動監視スクリプト
==========================================

指定した住宅（既定：コーシャハイム久我山）のあき家状況を定期的にチェックし、
"新しく出てきた部屋" だけを Discord に通知します。

【JKKねっとの特殊な作りと、その攻略法】
1) URLを直接入力したアクセスはエラーで弾かれる
   → JKK公式の物件ページを先に開き、そこのリンクを辿る（順路を再現）
2) 検索結果は「別ウィンドウ」に表示される
   （中継ページの JavaScript が window.open で新しい窓を開いている）
   → 新しく開いたウィンドウを捕まえて、そちらの中身を読む
   → 開かなかった場合は、送信先を同じページに書き換えて送り直す
3) サイトの応答がとても遅いことがある
   → 焦って押し直さず、じっくり待つ

【必要な環境変数】GitHub Actions の Secrets / env から渡します
  DISCORD_WEBHOOK_URL  … 必須。Discord の Webhook URL
  TARGET_BUILDING      … 住宅名（表示用）     既定: コーシャハイム久我山
  TARGET_BUILDING_KANA … 住宅名のカタカナ表記 既定: コーシャハイムクガヤマ
  TARGET_PROPERTY_PAGE … JKK公式のその住宅のページURL（経由地として使う）
  HIGHLIGHT_TOU        … 本命の棟（任意）     既定: D
  HIGHLIGHT_FLOOR      … 本命の階（任意）     既定: 4
  HIGHLIGHT_MADORI     … 本命の間取り（任意） 既定: 3LDK
  STRICT_MODE          … "true" で本命条件に一致した部屋だけ通知（既定 false）
  SEND_DAILY_HEARTBEAT … "true" で1日1回「稼働中です」を通知（既定 false）
"""

import datetime
import hashlib
import json
import os
import pathlib
import random
import re
import sys
import time

import requests
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# 1. 設定の読み込み
# ---------------------------------------------------------------------------

JST = datetime.timezone(datetime.timedelta(hours=9))

BUILDING = os.getenv("TARGET_BUILDING", "コーシャハイム久我山").strip()
BUILDING_KANA = os.getenv("TARGET_BUILDING_KANA", "コーシャハイムクガヤマ").strip()

HIGHLIGHT_TOU = os.getenv("HIGHLIGHT_TOU", "D").strip().upper()
HIGHLIGHT_FLOOR = os.getenv("HIGHLIGHT_FLOOR", "4").strip()
HIGHLIGHT_MADORI = os.getenv("HIGHLIGHT_MADORI", "3LDK").strip().upper()

STRICT_MODE = os.getenv("STRICT_MODE", "false").lower() == "true"
SEND_DAILY_HEARTBEAT = os.getenv("SEND_DAILY_HEARTBEAT", "false").lower() == "true"

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# JKK公式の物件ページ。ここを経由することで「URL直接入力」扱いを回避する
PROPERTY_PAGE = os.getenv(
    "TARGET_PROPERTY_PAGE",
    "https://www.to-kousya.or.jp/chintai/reco/kh_kugayama.html",
).strip()

STATE_PATH = pathlib.Path("state.json")
DEBUG_DIR = pathlib.Path("debug")

# 一般的な Chrome のふりをする（見慣れないアクセスとして弾かれないため）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


def build_direct_url(kana: str) -> str:
    """
    JKK公式の物件ページにある「最新の空室状況を確認する」と同じURLを組み立てる。

    jutaku_name パラメータの正体は、カタカナ住宅名を UTF-16BE の16進数にしたもの。
      コーシャハイムクガヤマ -> 30B330FC30B730E330CF30A430E030AF30AC30E430DE
    """
    param = kana.encode("utf-16-be").hex().upper()
    return (
        "https://jhomes.to-kousya.or.jp/search/jkknet/service/"
        f"akiyaJyokenDirect?sen_flg=1&jutaku_name={param}"
    )


TARGET_URL = build_direct_url(BUILDING_KANA)


# ---------------------------------------------------------------------------
# 2. 小さな道具たち
# ---------------------------------------------------------------------------

def now_jst() -> datetime.datetime:
    return datetime.datetime.now(JST)


def log(message: str) -> None:
    print(f"[{now_jst():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def normalize(text: str) -> str:
    """全角スペースや改行をならして、比較しやすい1行の文字列にする。"""
    text = text.replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            log("state.json が壊れていたので新しく作り直します")
    return {"active": {}, "fail_streak": 0, "heartbeat_date": "", "date": ""}


def save_state(state: dict) -> None:
    # 毎回同じ並び順で書き出す（無駄な差分＝無駄なコミットを防ぐため）
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 3. Discord への通知
# ---------------------------------------------------------------------------

def post_discord(content: str, embeds=None) -> None:
    if not WEBHOOK:
        log("DISCORD_WEBHOOK_URL が未設定のため通知をスキップします")
        log(f"（本来送るはずだった内容）{content}")
        return

    payload = {"username": "JKKあき家ウォッチャー", "content": content[:1900]}
    if embeds:
        payload["embeds"] = embeds[:10]

    for attempt in range(3):
        try:
            res = requests.post(WEBHOOK, json=payload, timeout=20)
            if res.status_code in (200, 204):
                log("Discord へ通知しました")
                return
            log(f"Discord 応答エラー: {res.status_code} {res.text[:200]}")
        except Exception as e:  # noqa: BLE001
            log(f"Discord 送信失敗（{attempt + 1}回目）: {e}")
        time.sleep(3 * (attempt + 1))
    log("Discord への通知を3回試しましたが失敗しました")


# ---------------------------------------------------------------------------
# 4. ページを読む道具
# ---------------------------------------------------------------------------

# 「まだ結果が出ていない画面」の目印
WAITING_HINTS = (
    "自動で次の画面",
    "しばらくたっても",
    "こちらをクリック",
    "お待ちください",
)

# ブラウザの中で「結果画面まで進んだか」を判定するための JavaScript
ARRIVED_JS = """() => {
    const t = document.body ? document.body.innerText : '';
    if (!t.trim()) return false;
    if (t.includes('自動で次の画面')) return false;
    if (t.includes('こちらをクリック')) return false;
    if (t.includes('お待ちください')) return false;
    return true;
}"""


def collect_text(page) -> str:
    """メインページと、その中の全フレームの文字を集めて1本につなぐ。"""
    chunks = []
    for frame in page.frames:
        try:
            chunks.append(frame.inner_text("body"))
        except Exception:  # 読み込み中のフレームなどは無視
            continue
    return "\n".join(chunks)


def collect_rows(page) -> list:
    """
    表（<tr>）の中から「あき家1件ぶん」に見える行だけを拾う。
    判定条件：家賃（◯◯,◯◯◯円）と 面積（◯◯.◯㎡）の両方が入っている行。
    """
    raw_rows = []
    for frame in page.frames:
        try:
            elements = frame.query_selector_all("tr")
        except Exception:
            continue
        for el in elements:
            try:
                text = normalize(el.inner_text())
            except Exception:
                continue
            if not text:
                continue
            has_rent = re.search(r"[0-9]{1,3}(?:,[0-9]{3})+\s*円", text)
            has_area = re.search(r"[0-9]+\.[0-9]+\s*(?:㎡|m2|平方)", text)
            if has_rent and has_area:
                raw_rows.append(text)

    # 入れ子テーブル対策：短い行（＝いちばん内側の行）を優先して残す
    raw_rows.sort(key=len)
    kept = []
    for text in raw_rows:
        if text in kept:
            continue
        if any(inner in text for inner in kept):
            continue
        kept.append(text)
    return kept


def is_waiting(page) -> bool:
    """まだ「お待ちください」系の画面にいるか。"""
    try:
        return any(hint in collect_text(page) for hint in WAITING_HINTS)
    except Exception:
        return False


def wait_until_arrived(page, seconds: int) -> bool:
    """
    結果画面にたどり着くまで、何も触らずに待つ。着いたら True。

    JKKねっとは応答がとても遅いことがある。ここで焦って押し直すと、
    送信中のリクエストが取り消されて永久に進まなくなる。待つのが正解。
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        remaining = max(1, int(deadline - time.time()))
        try:
            page.wait_for_function(ARRIVED_JS, timeout=min(remaining, 10) * 1000)
            return True
        except Exception:
            # 画面遷移の最中は判定に失敗することがあるので、直接もう一度確かめる
            try:
                if not is_waiting(page) and normalize(collect_text(page)):
                    return True
            except Exception:
                pass
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)
    return False


def describe_page(page) -> str:
    """いま画面に何があるのかを調べてログに残す（原因特定の手がかり）。"""
    try:
        info = page.evaluate(
            r"""() => {
                const forms = Array.from(document.forms).map(f => ({
                    name: f.name || null,
                    action: f.action || null,
                    method: f.method || null,
                    target: f.target || null,
                    fields: Array.from(f.elements).map(e => e.name).filter(Boolean).slice(0, 12)
                }));
                return {
                    url: location.href,
                    title: document.title,
                    forms: forms,
                    text: (document.body ? document.body.innerText : '')
                        .replace(/\s+/g, ' ').trim().slice(0, 200)
                };
            }"""
        )
        return json.dumps(info, ensure_ascii=False)[:1500]
    except Exception as e:  # noqa: BLE001
        return f"(調べられませんでした: {e})"


def reach_results(page, popups: list, patient: bool = True):
    """
    中継ページから結果ページへ進み、"結果が表示されているページ" を返す。

    JKKねっとは検索結果を別ウィンドウ（window.open した "JKKnet"）に出す。
    そのため、元のページを見続けても永久に変わらない。
    """
    if not is_waiting(page):
        return page  # すでに結果が出ている

    popup_wait = 25 if patient else 15
    long_wait = 90 if patient else 50

    # --- 手段1：別ウィンドウが開くのを待って、そちらへ乗り換える ---
    log(f"中継ページを検出。別ウィンドウが開くか待ちます（最大{popup_wait}秒）")
    deadline = time.time() + popup_wait
    while time.time() < deadline and not popups:
        try:
            page.wait_for_timeout(500)
        except Exception:
            time.sleep(0.5)

    if popups:
        target = popups[-1]
        log(f"別ウィンドウを検出しました（{len(popups)}個）。そちらを読みます")
        try:
            target.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:
            pass
        if wait_until_arrived(target, long_wait):
            log(f"別ウィンドウに結果が表示されました: {target.url}")
            return target
        log("別ウィンドウが結果まで進みませんでした")
        log(f"別ウィンドウの状態: {describe_page(target)}")
    else:
        log("別ウィンドウは開きませんでした")

    # --- 手段2：送信先を「このページ」に書き換えて送り直す ---
    log(f"元のページの状態: {describe_page(page)}")
    log("フォームの表示先を現在のページに変更して送信します")
    try:
        ok = page.evaluate(
            """() => {
                const f = document.forwardForm || document.forms[0];
                if (!f) return false;
                f.target = '_self';   // 別ウィンドウではなく、このページに表示させる
                f.submit();
                return true;
            }"""
        )
    except Exception as e:  # noqa: BLE001
        log(f"フォームの送信に失敗しました: {e}")
        ok = False

    if ok:
        if wait_until_arrived(page, long_wait):
            log("同じページに結果が表示されました")
            return page
        log("送信しましたが結果まで進みませんでした")
    else:
        log("送信できるフォームが見つかりませんでした")

    # --- 手段3：この間に別ウィンドウが開いていないか、最後にもう一度確認 ---
    if popups:
        target = popups[-1]
        if wait_until_arrived(target, 30):
            log("遅れて開いた別ウィンドウに結果が表示されました")
            return target

    log("結果ページにたどり着けませんでした")
    return page


def read_page(page):
    """ページから、本文・あき家の行・住宅名の有無を取り出す。"""
    body_text = collect_text(page)
    rows = collect_rows(page)
    name_hit = (BUILDING in body_text) or (BUILDING_KANA in body_text)
    return body_text, rows, name_hit


# 画面の種類を見分けるための目印
ERROR_MARKERS = ("エラーが発生しました", "URLを直接入力", "ただいま大変混雑")
EMPTY_MARKERS = (
    "該当する住宅がありません", "該当する住宅はありません", "該当するお部屋がありません",
    "条件に一致する", "検索結果は0件", "0件でした", "見つかりませんでした",
    "あき家がありません", "現在募集中の住宅はありません",
)


def classify(body_text: str, rows: list, name_hit: bool) -> str:
    """
    ページの種類を判定する。
      "results" … あき家が載っている
      "empty"   … 正常に表示されたが、あき家は無い
      "error"   … エラー画面 or 見慣れない画面（＝故障の可能性）

    "empty" と "error" をきちんと分けるのが肝。ここを混同すると、
    スクリプトが壊れていても「あき家なし」に見えてしまい永久に気づけない。
    """
    if rows:
        return "results"
    if any(marker in body_text for marker in WAITING_HINTS):
        return "error"
    if any(marker in body_text for marker in ERROR_MARKERS):
        return "error"
    if any(marker in body_text for marker in EMPTY_MARKERS):
        return "empty"
    if name_hit:
        return "empty"
    if "あき家" in body_text and ("検索" in body_text or "募集" in body_text):
        return "empty"
    return "error"


def save_debug(page, body_text: str) -> None:
    """あとから中身を確認できるように、画面と本文を残す。"""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / "page.txt").write_text(body_text, encoding="utf-8")
        (DEBUG_DIR / "page.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(DEBUG_DIR / "page.png"), full_page=True)
    except Exception as e:  # noqa: BLE001
        log(f"デバッグ出力の保存に失敗（無視して続行）: {e}")


def fetch_page():
    """ブラウザを立ち上げて、あき家の結果ページを取得する。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-popup-blocking",  # JKKは結果を別ウィンドウに出すので必須
            ]
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1280, "height": 900},
            extra_http_headers={"Accept-Language": "ja-JP,ja;q=0.9"},
        )

        # 新しく開いたウィンドウを取りこぼさないよう、先に見張りを立てておく
        popups = []
        context.on("page", lambda p_: popups.append(p_))

        page = context.new_page()
        page.set_default_timeout(60_000)

        result = ("", [], False, "error", page.url)
        shown = page

        try:
            # --- 順路A：公式の物件ページを開いて、そこのリンクを辿る ---
            log(f"公式ページを開きます: {PROPERTY_PAGE}")
            page.goto(PROPERTY_PAGE, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(1_500)

            href = page.evaluate(
                """() => {
                    const links = Array.from(document.querySelectorAll("a[href*='akiyaJyokenDirect']"));
                    const pc = links.find(a => !a.href.includes('Mobile'));
                    return pc ? pc.href : null;
                }"""
            )
            target_url = href or TARGET_URL
            log(f"空室確認リンクへ移動します: {target_url}")
            page.goto(
                target_url, referer=PROPERTY_PAGE,
                wait_until="domcontentloaded", timeout=90_000,
            )

            shown = reach_results(page, popups, patient=True)
            body_text, rows, name_hit = read_page(shown)
            state = classify(body_text, rows, name_hit)
            log(f"順路A の結果: {state}")
            result = (body_text, rows, name_hit, state, shown.url)

            # --- 順路B：うまくいかなかったときの予備（直リンクを開く） ---
            if state == "error":
                log(f"順路B を試します: {TARGET_URL}")
                popups.clear()
                page2 = context.new_page()
                page2.set_default_timeout(60_000)
                page2.goto(
                    TARGET_URL, referer=PROPERTY_PAGE,
                    wait_until="domcontentloaded", timeout=90_000,
                )
                shown = reach_results(page2, popups, patient=False)
                body_text, rows, name_hit = read_page(shown)
                state = classify(body_text, rows, name_hit)
                log(f"順路B の結果: {state}")
                result = (body_text, rows, name_hit, state, shown.url)

        except Exception as e:  # noqa: BLE001
            log(f"ページの取得中にエラーが起きました: {e}")

        save_debug(shown, result[0])
        context.close()
        browser.close()

    return result


# ---------------------------------------------------------------------------
# 5. 拾った行を「部屋の情報」に整える
# ---------------------------------------------------------------------------

def parse_room(text: str) -> dict:
    rent = re.search(r"([0-9]{1,3}(?:,[0-9]{3})+)\s*円", text)
    area = re.search(r"([0-9]+\.[0-9]+)\s*(?:㎡|m2|平方)", text)
    madori = re.search(r"([1-9])\s?([SLDKsldk]{1,4})", text)
    floor = re.search(r"([0-9]{1,2})\s*階", text)
    tou = re.search(r"([A-Za-z0-9])\s*号?棟", text)

    room = {
        # 行の文字列そのものを指紋（ID）にする。1文字でも違えば別の部屋として扱う
        "id": hashlib.sha1(text.encode("utf-8")).hexdigest()[:12],
        "raw": text[:400],
        "rent": rent.group(1) + "円" if rent else "—",
        "area": area.group(1) + "㎡" if area else "—",
        "madori": (madori.group(1) + madori.group(2).upper()) if madori else "—",
        "floor": floor.group(1) + "階" if floor else "—",
        "tou": tou.group(1).upper() + "棟" if tou else "—",
    }
    room["is_target"] = is_highlight(room)
    return room


def is_highlight(room: dict) -> bool:
    """
    本命条件（既定：D棟 / 4階 / 3LDK）に当てはまるか。
    ・はっきり違う項目が1つでもあれば False
    ・ページ上で読み取れなかった項目（—）は "違うとは言い切れない" として見逃す
    ・1つも照合できなかった場合は False（🎯は付かないが通知自体はされる）
    """
    matched = False
    pairs = [
        (HIGHLIGHT_TOU, room["tou"], lambda want, got: got.startswith(want)),
        (HIGHLIGHT_FLOOR, room["floor"], lambda want, got: got == f"{want}階"),
        (HIGHLIGHT_MADORI, room["madori"], lambda want, got: got == want),
    ]
    for want, got, ok in pairs:
        if not want or got == "—":
            continue
        if ok(want, got):
            matched = True
        else:
            return False
    return matched


def format_room(room: dict) -> str:
    mark = "🎯 " if room["is_target"] else ""
    return (
        f"{mark}{room['tou']} / {room['floor']} / {room['madori']} / "
        f"{room['area']} / **{room['rent']}**"
    )


# ---------------------------------------------------------------------------
# 6. メイン処理
# ---------------------------------------------------------------------------

def main() -> int:
    state = load_state()
    today = f"{now_jst():%Y-%m-%d}"

    def record_failure(reason: str) -> int:
        """取得できなかったときの共通処理。前回の掲載記録は消さずに残す。"""
        state["fail_streak"] = int(state.get("fail_streak", 0)) + 1
        log(f"取得に失敗しました（連続 {state['fail_streak']} 回目）: {reason}")
        # 10分間隔で6回＝約1時間続けて失敗したときだけ、1度お知らせする
        if state["fail_streak"] == 6:
            post_discord(
                "⚠️ JKKねっとの監視が1時間ほど連続で失敗しています。\n"
                "サイトのメンテナンス中か、ページ構成が変わった可能性があります。\n"
                "Actionsタブから手動実行して debug/page.png を確認してください。\n"
                f"詳細: `{reason[:300]}`"
            )
        state["date"] = today
        save_state(state)
        return 0

    # アクセス時刻を毎回わずかにずらす（機械的な等間隔アクセスを避けるため）
    jitter = random.randint(0, 45)
    log(f"{jitter} 秒待ってからアクセスします")
    time.sleep(jitter)

    try:
        body_text, rows, name_hit, page_state, final_url = fetch_page()
    except Exception as e:  # noqa: BLE001
        return record_failure(str(e))

    log(
        f"画面の種類: {page_state} / 抽出した行数: {len(rows)} / "
        f"住宅名ヒット: {name_hit} / 最終URL: {final_url}"
    )

    # 見慣れない画面＝故障の可能性。"あき家ゼロ" と混同しないよう失敗として扱う
    if page_state == "error":
        head = normalize(body_text)[:200] or "（本文が空でした）"
        return record_failure(f"想定外の画面が返りました: {head}")

    state["fail_streak"] = 0

    if len(rows) > 30:
        # 想定より多い＝住宅名で絞り込めていない可能性。名前を含む行だけに限定する
        rows = [r for r in rows if (BUILDING in r) or (BUILDING_KANA in r)]
        log(f"住宅名で絞り込み: {len(rows)} 行")

    rooms = [parse_room(r) for r in rows]

    if STRICT_MODE:
        rooms = [r for r in rooms if r["is_target"]]

    current = {r["id"]: r for r in rooms}
    previous = state.get("active", {})

    new_ids = [rid for rid in current if rid not in previous]
    gone_ids = [rid for rid in previous if rid not in current]

    # --- 新着があれば通知 ---
    if new_ids:
        new_rooms = [current[rid] for rid in new_ids]
        has_target = any(r["is_target"] for r in new_rooms)
        headline = (
            f"🎯🎉 **本命の部屋が出ました！**「{BUILDING}」"
            if has_target
            else f"🏠 **「{BUILDING}」にあき家が出ました**"
        )
        lines = "\n".join(f"・{format_room(r)}" for r in new_rooms)
        content = (
            f"{headline}\n{lines}\n\n"
            f"▼ すぐ確認する（JKK公式ページ）\n{PROPERTY_PAGE}\n"
            "※先着順です。ページ下部の「最新の空室状況を確認する」から申込へお進みください。"
        )
        embeds = [{
            "title": f"{BUILDING} あき家情報（{len(new_rooms)}件）",
            "url": PROPERTY_PAGE,
            "color": 0xE8453C if has_target else 0x3BA55D,
            "description": lines[:3900],
            "footer": {"text": f"検知時刻 {now_jst():%Y-%m-%d %H:%M} JST"},
        }]
        post_discord(content, embeds)
    else:
        log("新着はありません")

    if gone_ids:
        log(f"掲載が終了した部屋: {len(gone_ids)} 件（記録から削除しました）")

    # --- 1日1回の生存確認（任意） ---
    if SEND_DAILY_HEARTBEAT and state.get("heartbeat_date") != today:
        state["heartbeat_date"] = today
        post_discord(
            f"✅ 監視は正常に動いています（{now_jst():%m/%d %H:%M} 時点）。"
            f"現在の掲載数: {len(current)} 件"
        )

    # --- 状態を保存 ---
    # date は1日1回だけ変わる。これによりリポジトリが「60日間更新なし」で
    # 自動停止されるのを防ぎつつ、コミットは1日1回程度に抑えられる。
    state["active"] = current
    state["date"] = today
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
