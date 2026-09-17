#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
鐘栄商店のサイトから「電気銅建値」「黄銅ダライ粉価格」の推移表を取得して
souba-shouei.json を作るスクリプト。GitHub Actions が平日12:30(日本時間)に実行します。

- 追加ライブラリ不要(Python標準ライブラリだけで動きます)
- サイトの表が読めなかったときは、エラーで止まって前回のJSONをそのまま残します
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

JST = timezone(timedelta(hours=9))

PAGES = {
    "copper": {
        "url": "http://www.shouei-shouten.com/14536823058148",
        "name": "電気銅建値",
        "unit": "t",
        "min_price": 200_000,   # 値の妥当性チェック(円/t)
        "max_price": 20_000_000,
    },
    "brass": {
        "url": "http://www.shouei-shouten.com/14537920252370",
        "name": "黄銅ダライ粉",
        "unit": "kg",
        "min_price": 100,       # 値の妥当性チェック(円/kg)
        "max_price": 20_000,
    },
}

OUT_FILE = "souba-shouei.json"


# ---------- HTMLの表をそのまま読み取る部品 ----------
class TableParser(HTMLParser):
    """ページ内の全ての<table>を [[セル文字列,...], ...] の形で集める"""

    def __init__(self):
        super().__init__()
        self.tables = []
        self._table_stack = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table_stack.append([])
        elif tag == "tr" and self._table_stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "table" and self._table_stack:
            t = self._table_stack.pop()
            if t:
                self.tables.append(t)
        elif tag == "tr" and self._row is not None:
            if self._table_stack:
                self._table_stack[-1].append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None:
            text = "".join(self._cell)
            text = re.sub(r"\s+", " ", text).strip()
            if self._row is not None:
                self._row.append(text)
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_price(text):
    """'1,140,000' '856' '1.224'(カンマ打ち間違い) などを数値にする。数値でなければ None"""
    s = text.strip().replace(",", "").replace("，", "").replace(" ", "")
    if not s:
        return None
    # サイト側の入力ミス対策: 「1.224」のようにカンマの代わりにピリオドが使われた場合
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    if not re.fullmatch(r"\d+(\.\d+)?", s):
        return None
    v = float(s)
    return int(v) if v == int(v) else v


def parse_date_md(text):
    """'01月04日' → (1, 4)。違う形式なら None"""
    m = re.fullmatch(r"\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def extract_series(html_text):
    """推移表(年ごとの「改定日/価格」列が並ぶ表)を探して {日付: 価格} を返す"""
    tp = TableParser()
    tp.feed(html_text)
    series = {}
    for table in tp.tables:
        # 「改定日」を含む行がある表だけが対象(月間平均の表などは対象外)
        if not any(any("改定日" in c for c in row) for row in table):
            continue
        # 年の並びを見つける(例: 2022年, 2023年, ...)
        years = []
        for row in table:
            found = [int(m.group(1)) for c in row for m in [re.search(r"(20\d{2})\s*年", c)] if m]
            if len(found) >= 2:
                years = found
                break
        if not years:
            continue
        for row in table:
            # 「改定日」「年」の行はスキップして、日付+価格のペアを拾う
            if any("改定日" in c for c in row) or any(re.search(r"20\d{2}\s*年", c) for c in row):
                continue
            # 空セルを保ったまま2つずつ組にする
            pairs = []
            i = 0
            while i + 1 < len(row):
                pairs.append((row[i], row[i + 1]))
                i += 2
            for idx, (dtext, ptext) in enumerate(pairs):
                if idx >= len(years):
                    break
                md = parse_date_md(dtext)
                price = parse_price(ptext)
                if md is None or price is None:
                    continue
                d = f"{years[idx]:04d}-{md[0]:02d}-{md[1]:02d}"
                series[d] = price
    return series


# ---------- 取得 ----------
def fetch_html(url, tries=3):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; sekine-tanka-bot/1.0)"},
    )
    last_err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                raw = res.read()
                ctype = res.headers.get("Content-Type", "")
            m = re.search(r"charset=([\w\-]+)", ctype, re.I)
            encodings = [m.group(1)] if m else []
            m2 = re.search(rb'charset=["\']?([\w\-]+)', raw[:2000], re.I)
            if m2:
                encodings.append(m2.group(1).decode("ascii", "ignore"))
            encodings += ["utf-8", "cp932", "euc-jp"]
            for enc in encodings:
                try:
                    return raw.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    continue
            return raw.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"取得に失敗: {url} ({last_err})")


def validate(key, cfg, series):
    if len(series) < 30:
        raise RuntimeError(f"{cfg['name']}: 取れた件数が少なすぎます({len(series)}件)。ページ構成が変わったかもしれません")
    today = datetime.now(JST).date()
    for d, p in series.items():
        if not (cfg["min_price"] <= p <= cfg["max_price"]):
            raise RuntimeError(f"{cfg['name']}: {d} の値 {p} が想定範囲外です")
        if datetime.strptime(d, "%Y-%m-%d").date() > today + timedelta(days=3):
            raise RuntimeError(f"{cfg['name']}: 未来の日付 {d} が入っています")


def build_json(all_series):
    items = {}
    for key, cfg in PAGES.items():
        s = all_series[key]
        ordered = sorted(s.items())  # 日付順
        items[key] = {
            "name": cfg["name"],
            "unit": cfg["unit"],
            "series": [[d, p] for d, p in ordered],
        }
    return {
        "source": "株式会社鐘栄商店",
        "source_urls": {k: v["url"] for k, v in PAGES.items()},
        "fetched_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "items": items,
    }


def main():
    all_series = {}
    for key, cfg in PAGES.items():
        html_text = fetch_html(cfg["url"])
        series = extract_series(html_text)
        validate(key, cfg, series)
        all_series[key] = series
        latest = max(series)
        print(f"{cfg['name']}: {len(series)}件 最新 {latest} = {series[latest]:,}円/{cfg['unit']}")

    data = build_json(all_series)

    # 中身(fetched_at以外)が前回と同じなら書き換えない → 余計なコミットをしない
    if os.path.exists(OUT_FILE):
        try:
            with open(OUT_FILE, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("items") == data["items"]:
                print("変更なし(前回と同じ内容)")
                return
        except Exception:  # noqa: BLE001
            pass

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"{OUT_FILE} を更新しました")


if __name__ == "__main__":
    sys.exit(main())
