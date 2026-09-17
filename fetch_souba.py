#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
相場の自動取得スクリプト。GitHub Actions が平日12:30と17:30(日本時間)に実行します。

1) 鐘栄商店: 「電気銅建値」「黄銅ダライ粉価格」の推移表(HTML) → souba-shouei.json
2) 東京製鐵: 国内鉄スクラップ購入価格表(PDF) → souba-tokyotetsu.json
   (宇都宮: 特A・特級・鋼ダライ粉・新断 / 田原: 新断)

- 東京製鐵のPDF読み取りには pdfplumber が必要(workflowでインストールします)
- どちらかの取得に失敗しても、もう片方は普通に更新されます
"""
import io
import json
import os
import re
import sys
import time
import unicodedata
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


def write_if_changed(path, data):
    """中身(fetched_at以外)が前回と同じなら書き換えない → 余計なコミットをしない"""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("items") == data["items"]:
                print(f"{path}: 変更なし(前回と同じ内容)")
                return
        except Exception:  # noqa: BLE001
            pass
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"{path} を更新しました")


def run_shouei():
    all_series = {}
    for key, cfg in PAGES.items():
        html_text = fetch_html(cfg["url"])
        series = extract_series(html_text)
        validate(key, cfg, series)
        all_series[key] = series
        latest = max(series)
        print(f"{cfg['name']}: {len(series)}件 最新 {latest} = {series[latest]:,}円/{cfg['unit']}")
    write_if_changed(OUT_FILE, build_json(all_series))


# ========== 東京製鐵(鉄スクラップ購入価格・PDF) ==========
TT_PAGE = "https://www.tokyosteel.co.jp/scrapprice/"
TT_OUT = "souba-tokyotetsu.json"
# 取り込む品目: (キー, 表示名, 工場ヘッダー名, PDF内の品名)
TT_ITEMS = [
    ("u_tokua",   "宇都宮 特A",       "宇都宮工場", "特A"),
    ("u_tokkyu",  "宇都宮 特級",      "宇都宮工場", "特級"),
    ("u_darai",   "宇都宮 鋼ダライ粉", "宇都宮工場", "鋼ダライ粉"),
    ("u_shindan", "宇都宮 新断",      "宇都宮工場", "新断"),
    ("t_shindan", "田原 新断",        "田原工場",   "新断"),
]
TT_MIN, TT_MAX = 5_000, 300_000  # 円/t の妥当範囲


def fetch_bytes(url, tries=3):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; sekine-tanka-bot/1.0)"})
    last_err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return res.read()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"取得に失敗: {url} ({last_err})")


def tt_norm(s):
    s = unicodedata.normalize("NFKC", s).replace(" ", "").replace("　", "")
    return s.replace("－", "ー").replace("‐", "ー").replace("-", "ー")


def parse_tokyotetsu_pdf(pdf_bytes, fallback_date):
    """PDFの1ページ目から {品目キー: (日付, 価格)} を返す。文字の位置座標で列を判定する"""
    import pdfplumber  # workflowでインストール

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page = pdf.pages[0]
        text = page.extract_text() or ""
        words = page.extract_words()

    # 適用開始日
    m = re.search(r"適用開始日[^0-9]*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text, re.S)
    date = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else fallback_date

    # 工場ヘッダーのx中心
    plants = {}
    for w in words:
        t = tt_norm(w["text"])
        if t.endswith("工場") and t not in plants:
            plants[t] = (w["x0"] + w["x1"]) / 2
    # 数字セルを列クラスタに分ける
    nums = [w for w in words if re.fullmatch(r"[\d,]+", w["text"]) and "," in w["text"]]
    if not plants or not nums:
        raise RuntimeError("東京製鐵PDF: 表を読み取れません(レイアウトが変わったかも)")
    centers = sorted((w["x0"] + w["x1"]) / 2 for w in nums)
    clusters = []
    for c in centers:
        if not clusters or c - clusters[-1][-1] > 15:
            clusters.append([c])
        else:
            clusters[-1].append(c)
    col_centers = [sum(g) / len(g) for g in clusters]

    def col_of(x):
        return min(range(len(col_centers)), key=lambda i: abs(col_centers[i] - x))

    plant_col = {name: col_of(x) for name, x in plants.items()}

    # 行ごとに 品名 と {列: 価格}
    rows = {}
    for w in words:
        rows.setdefault(round(w["top"] / 3) * 3, []).append(w)
    table = {}
    for key in sorted(rows):
        ws = sorted(rows[key], key=lambda w: w["x0"])
        name = tt_norm("".join(w["text"] for w in ws if not re.fullmatch(r"[\d,]+", w["text"])))
        vals = {}
        for w in ws:
            if re.fullmatch(r"[\d,]+", w["text"]) and "," in w["text"]:
                vals[col_of((w["x0"] + w["x1"]) / 2)] = int(w["text"].replace(",", ""))
        if name and vals and name not in table:
            table[name] = vals

    out = {}
    for ikey, _label, plant, item_name in TT_ITEMS:
        col = plant_col.get(plant)
        row = table.get(tt_norm(item_name))
        if col is None or row is None or col not in row:
            continue  # その改定でこの品目の記載がない場合はスキップ
        price = row[col]
        if not (TT_MIN <= price <= TT_MAX):
            raise RuntimeError(f"東京製鐵PDF: {item_name} の値 {price} が想定範囲外")
        out[ikey] = (date, price)
    if not out:
        raise RuntimeError("東京製鐵PDF: 対象品目が1つも取れません(レイアウトが変わったかも)")
    return out


def run_tokyotetsu():
    # 既存JSONを読む(あれば)
    old_items = {}
    known_dates = set()
    if os.path.exists(TT_OUT):
        try:
            with open(TT_OUT, encoding="utf-8") as f:
                old = json.load(f)
            old_items = old.get("items", {})
            known_dates = set(old.get("pdf_dates", []))
        except Exception:  # noqa: BLE001
            pass

    # ページからPDFリンク一覧(日付つき)を集める
    html_text = fetch_html(TT_PAGE)
    links = re.findall(r'href="([^"]*?/scrapprice/price/\d{4}/([\d.]+)\.pdf)"', html_text)
    pdfs = []
    for href, dstr in links:
        m = re.fullmatch(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", dstr)
        if not m:
            continue
        d = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        url = href if href.startswith("http") else "https://www.tokyosteel.co.jp" + href
        pdfs.append((d, url))
    if not pdfs:
        raise RuntimeError("東京製鐵: ページにPDFリンクが見つかりません")
    pdfs.sort()
    new_pdfs = [(d, u) for d, u in pdfs if d not in known_dates]
    print(f"東京製鐵: PDF {len(pdfs)}件中 新規 {len(new_pdfs)}件")

    # 品目ごとの {日付: 価格} を組み立て(既存分を引き継ぐ)
    series = {i[0]: dict(map(tuple, old_items.get(i[0], {}).get("series_raw", []))) for i in TT_ITEMS}
    done_dates = set(known_dates)
    for d, url in new_pdfs:
        try:
            data = parse_tokyotetsu_pdf(fetch_bytes(url), d)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ {d} のPDFを読めませんでした: {e}")
            continue
        for ikey, (ad, price) in data.items():
            series.setdefault(ikey, {})[ad] = price
        done_dates.add(d)
        time.sleep(0.3)

    items = {}
    for ikey, label, _plant, _iname in TT_ITEMS:
        raw = sorted(series.get(ikey, {}).items())
        items[ikey] = {"name": label, "unit": "t", "series_raw": [[d, p] for d, p in raw]}
    data = {
        "source": "東京製鐵",
        "page_url": TT_PAGE,
        "fetched_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "pdf_dates": sorted(done_dates),
        "items": items,
    }
    for ikey, v in items.items():
        s = v["series_raw"]
        print(f"  {v['name']}: {len(s)}件 最新 {s[-1][0]} = {s[-1][1]:,}円/t" if s else f"  {v['name']}: 0件")
    write_if_changed(TT_OUT, data)


def main():
    fails = []
    for name, fn in (("鐘栄商店", run_shouei), ("東京製鐵", run_tokyotetsu)):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"❌ {name} の取得に失敗: {e}")
            fails.append(name)
    if len(fails) == 2:
        return 1  # 両方失敗したときだけエラー(片方は生かす)
    return 0


if __name__ == "__main__":
    sys.exit(main())
