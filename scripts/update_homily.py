#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日讲道自动更新 v2：只读抓思高原站最新讲道，验过 MP3 能打开后，
把新条目插到 index.html 顶部的 ENTRIES 数组，并同步更新置顶区；
同时按置顶日期重建“往年今日”区块（过去三年同一天在原站的讲道）。
- 缺失的中间日期会一并补回（不只补最新一天）。
- 往年讲道只存原站直链 MP3；文字页逐个核实，存在的才给链接，不存在的行只有音频。
用法（在仓库根目录）：python3 scripts/update_homily.py
无新讲道时不改文件，退出码 0；MP3 验不过或原站打不开时不改文件，退出码 1。
只用 Python 标准库，不需安装任何包。
"""
import datetime
import json
import re
import sys
import urllib.error
import urllib.request

BASE_INFO = "https://ccreadbible.info"
BASE_MP3 = "https://www.ccreadbible.net"
HEADERS = {"User-Agent": "Mozilla/5.0 (daily-homily-updater; +https://ccreadbible78.github.io)"}
INDEX_HTML = "index.html"
PAST_YEARS = 3  # 往年今日往前取几年

CN_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9}
BOOK_MAP = {"瑪": "玛窦福音", "玛": "玛窦福音", "谷": "马尔谷福音",
            "路": "路加福音", "若": "若望福音"}
WEEKDAY = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"]


def cn_to_int(s):
    """中文数字转阿拉伯数字，只处理 1-99 的章节号，如 十六->16，二五->25，三十一->31。"""
    if not s:
        return None
    if "十" not in s:
        n = 0
        for ch in s:
            n = n * 10 + CN_DIGIT.get(ch, 0)
        return n
    parts = s.split("十")
    tens = CN_DIGIT.get(parts[0], 1) if parts[0] else 1
    ones = CN_DIGIT.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
    return tens * 10 + ones


def expand_gospel(gospel):
    """路十六9-15 -> 路加福音 16:9-15；瑪六1-6, 16-18 -> 玛窦福音 6:1-6, 16-18。"""
    if not gospel or gospel in ("無", "无"):
        return "专题讲道"
    m = re.match(r"^([瑪玛谷路若])([一二三四五六七八九十]+)(.*)$", gospel.strip())
    if not m:
        return gospel
    book, chapter_cn, rest = m.groups()
    chapter = cn_to_int(chapter_cn)
    verses = rest.strip().replace("，", ", ").replace("、", ", ")
    # 章节后的 9-15 这类用冒号分隔：16:9-15
    return "%s %s:%s" % (BOOK_MAP.get(book, book), chapter, verses)


def fetch(url, timeout=30, retries=3):
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="ignore")
        except Exception as e:  # noqa: BLE001 - 网络重试，报错留到最后统一报
            last = e
    raise RuntimeError("打不开 %s：%s" % (url, last))


def head_mp3_info(url, timeout=30):
    """返回 (是不是可播的音频, 大小文案如 '约3.7M' 或 None)。"""
    req = urllib.request.Request(url, headers=HEADERS, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        length = r.headers.get("Content-Length", "")
        ok = r.status == 200 and ("audio" in ctype or (length and int(length) > 10000))
        size = None
        if length:
            try:
                size = "约%.1fM" % (int(length) / 1048576)
            except ValueError:
                size = None
        return ok, size


def text_page_exists(url, timeout=20):
    """文字页是否存在：先 HEAD，打不通再 GET 只读 1 字节。"""
    try:
        req = urllib.request.Request(url, headers=HEADERS, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status == 200:
                return True
    except Exception:  # noqa: BLE001 - HEAD 不通就换 GET 再试
        pass
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(1)
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False
    return False


def bimonthly_url(year, month):
    """双月页地址，如 2026 年 11 月 -> homily-2026-1112.html。"""
    block_start = month - 1 if month % 2 == 0 else month
    return "%s/homily/homily-%d-%02d%02d.html" % (BASE_INFO, year, block_start, block_start + 1)


def parse_monthly_entries(html, want_date):
    """从月度页 HTML 里找出指定日期的全部条目（正日 + 可能的 -1 专题）。"""
    entries = []
    seen = set()
    for m in re.finditer(r"https://www\.ccreadbible\.net/bona/\d+homily/(\d{8}(-1)?)\.mp3", html):
        mp3_url = m.group(0)
        if mp3_url in seen:
            continue
        seen.add(mp3_url)
        ymd = m.group(1)
        if ymd[:8] != want_date:
            continue
        start = max(0, m.start() - 900)
        chunk = html[start:m.start()]
        # 找离 MP3 最近的一次文字页链接与标题、福音
        # 标题在 <a><span>标题</span></a> 里，中间隔着 <span> 标签，不能用 [^<]+ 直接取
        links = re.findall(r'homily-(\d{8}(-1)?)\.html.*?(?:<span[^>]*>)?([^<]+)</span>', chunk, flags=re.S)
        if not links:
            continue
        title = links[-1][2].strip()
        g = re.findall(r"福音[:：]\s*([^<\s]+)", chunk)
        gospel = g[-1].strip() if g else "無"
        y, mo, d = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])
        entry = {
            "date": "%04d-%02d-%02d" % (y, mo, d),
            "title": title,
            "gospel": gospel,
            "text_url": "%s/homily-%d/homily-%s.html" % (BASE_INFO, y, ymd),
            "mp3_url": mp3_url,
        }
        if ymd.endswith("-1"):
            entry["extra"] = True
        entries.append(entry)
    return entries


def parse_monthly_row(html, want_ymd):
    """从月度页里按行找指定日期的主条目（不含 -1 专题）。
    旧年份月度页标题不是链接、只有裸 <span>，所以按 <tr> 行解析，
    不依赖文字页链接。返回 dict 或 None。"""
    for tr in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
        block = tr.group(1)
        m = re.search(r"https://www\.ccreadbible\.net/bona/\d+homily/(%s)\.mp3" % re.escape(want_ymd), block)
        if not m:
            continue
        mp3_url = m.group(0)
        spans = re.findall(r"<span[^>]*>([^<]+)</span>", block)
        title = spans[0].strip() if spans else ""
        g = re.search(r"福音[:：]\s*([^<\s]+)", block)
        gospel = g.group(1).strip() if g else "無"
        y, mo, d = int(want_ymd[:4]), int(want_ymd[4:6]), int(want_ymd[6:8])
        return {
            "date": "%04d-%02d-%02d" % (y, mo, d),
            "title": title,
            "gospel": gospel,
            "mp3_url": mp3_url,
        }
    return None


def rebuild_past(html, top_date, monthly_cache):
    """按置顶日期重建往年今日：过去 PAST_YEARS 年同一天的主条目。
    只改 var PAST 数组和标题行里的日期，行由页面 JS 按 PAST 数组渲染。
    返回 (html, 往年条数)。"""
    y, mo, d = int(top_date[:4]), int(top_date[5:7]), int(top_date[8:10])
    mmdd = "%02d%02d" % (mo, d)
    past = []
    for py in range(y - 1, y - 1 - PAST_YEARS, -1):
        want = "%04d%s" % (py, mmdd)
        key = (py, mo)
        if key not in monthly_cache:
            try:
                monthly_cache[key] = fetch(bimonthly_url(py, mo))
            except Exception as e:  # noqa: BLE001
                print("往年 %d 年月度页打不开，跳过：%s" % (py, e))
                continue
        row = parse_monthly_row(monthly_cache[key], want)
        if not row:
            continue
        try:
            ok, size = head_mp3_info(row["mp3_url"])
        except Exception as exc:  # noqa: BLE001
            print("往年 MP3 验不过，跳过 %s：%s" % (row["mp3_url"], exc))
            continue
        if not ok:
            print("往年 MP3 验不过（不是音频或太小），跳过：%s" % row["mp3_url"])
            continue
        row["size"] = size or ""
        text_url = "%s/homily-%d/homily-%s.html" % (BASE_INFO, py, want)
        if text_page_exists(text_url):
            row["text_url"] = text_url
        past.append(row)
    past_json = json.dumps(past, ensure_ascii=False)
    html, n = re.subn(r"var PAST = \[.*?\];", "var PAST = %s;" % past_json, html, count=1, flags=re.S)
    if not n:
        print("index.html 里找不到 var PAST，往年数组没写进去。")
    # 标题里的日期：只改“往年今日”同一文本行内的日期（[^<] 跨不过标签，不会碰到各行内容）
    m = re.search(r"<!-- PAST-YEARS-START -->(.*?)<!-- PAST-YEARS-END -->", html, flags=re.S)
    if m:
        sec = m.group(1)
        sec2, n2 = re.subn(r"(往年今日[^<]{0,80}?)(\d{1,2}月\d{1,2}日)",
                           r"\g<1>%d月%d日" % (mo, d), sec, count=1)
        if n2:
            html = html[:m.start(1)] + sec2 + html[m.end(1):]
    else:
        print("index.html 里找不到往年今日标记，标题日期没改。")
    return html, len(past)


def main():
    try:
        html = open(INDEX_HTML, encoding="utf-8").read()
    except FileNotFoundError:
        print("找不到 index.html，请在仓库根目录运行。")
        return 1
    m = re.search(r"var ENTRIES = \[", html)
    if not m:
        print("index.html 里找不到 var ENTRIES = [，不敢改，已退出。")
        return 1
    top = re.search(r'"date":\s*"(\d{4})-(\d{2})-(\d{2})"', html)
    if not top:
        print("读不到现有置顶日期，已退出。")
        return 1
    top_date = "%s-%s-%s" % top.groups()
    top_ymd = top_date.replace("-", "")

    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date()
    year = today.year
    # 先看今年的索引页，第一条就是最新讲道
    try:
        index_html = fetch("%s/homily-%d.html" % (BASE_INFO, year))
    except Exception as e:  # noqa: BLE001
        print("原站索引页打不开：%s" % e)
        return 1
    found = re.findall(r"homily-(\d{8})\.html", index_html)
    if not found:
        print("索引页里没找到讲道链接，已退出。")
        return 1
    latest_ymd = found[0]
    latest_date = "%s-%s-%s" % (latest_ymd[:4], latest_ymd[4:6], latest_ymd[6:8])
    if latest_ymd <= top_ymd:
        print("已是最新（置顶 %s，最新 %s），不用更新。" % (top_date, latest_date))
        return 0

    # 把置顶之后到最新之间的每一天都补回（不只补最新一天），从新到旧收集
    monthly_cache = {}

    def get_monthly(yy, mm):
        key = (yy, mm)
        if key not in monthly_cache:
            monthly_cache[key] = fetch(bimonthly_url(yy, mm))
        return monthly_cache[key]

    top_d = datetime.date(int(top_date[:4]), int(top_date[5:7]), int(top_date[8:10]))
    latest_d = datetime.date(int(latest_ymd[:4]), int(latest_ymd[4:6]), int(latest_ymd[6:8]))
    entries_part = html[m.start():]
    todo = []
    cur = latest_d
    while cur > top_d:
        ymd = "%04d%02d%02d" % (cur.year, cur.month, cur.day)
        try:
            page = get_monthly(cur.year, cur.month)
        except Exception as e:  # noqa: BLE001
            print("月度页打不开：%s" % e)
            return 1
        day_entries = parse_monthly_entries(page, ymd)
        if not day_entries:
            print("月度页里没找到 %s 的条目，跳过这一天（不编造）。" % ymd)
            cur -= datetime.timedelta(days=1)
            continue
        for e in day_entries:
            if e["mp3_url"] not in entries_part and e not in todo:
                todo.append(e)
        cur -= datetime.timedelta(days=1)
    if not todo:
        print("最新 %s 已在页面里，不用更新。" % latest_date)
        return 0
    for e in todo:
        try:
            ok, _size = head_mp3_info(e["mp3_url"])
        except Exception as exc:  # noqa: BLE001
            print("MP3 验不过 %s：%s，已退出（不改文件）。" % (e["mp3_url"], exc))
            return 1
        if not ok:
            print("MP3 验不过 %s（不是音频或太小），已退出（不改文件）。" % e["mp3_url"])
            return 1

    # ---- 写回 index.html：只改置顶区 + ENTRIES 数组 + 计数/日期范围 ----
    first = todo[0]  # 最新一天的正日条目（-1 专题排在后面）
    new_json = ", ".join(json.dumps(e, ensure_ascii=False) for e in todo)
    html = html.replace("var ENTRIES = [", "var ENTRIES = [%s, " % new_json, 1)

    d = datetime.date(int(first["date"][:4]), int(first["date"][5:7]), int(first["date"][8:10]))
    wd = WEEKDAY[(d.weekday() + 1) % 7]
    date_cn = "%d年%d月%d日 · %s" % (d.year, d.month, d.day, wd)
    hero_h2 = "%d月%d日 %s" % (d.month, d.day, wd)
    gospel_full = expand_gospel(first["gospel"])
    # 置顶区
    html = re.sub(r'<span class="today-date" id="todayText">[^<]*</span>',
                  '<span class="today-date" id="todayText">%s</span>' % date_cn, html, count=1)
    html = re.sub(r'(<section class="hero"[^>]*>.*?<h2>)[^<]*(</h2>)',
                  r'\g<1>%s\g<2>' % hero_h2, html, count=1, flags=re.S)
    html = re.sub(r'(<div class="feast">)[^<]*(</div>)',
                  r'\g<1>%s\g<2>' % first["title"], html, count=1)
    html = re.sub(r'(<div class="gospel">\s*<b>)[^<]*(</b>)',
                  r'\g<1>福音：%s\g<2>' % gospel_full, html, count=1)
    html = re.sub(r'(<p class="verse">).*?(</p>)',
                  r'\g<1>今日福音：%s<small>—— 思高读经推广中心 · 每日弥撒讲道</small>\g<2>' % gospel_full,
                  html, count=1, flags=re.S)
    html = re.sub(r'(<strong id="nowTitle">)[^<]*(</strong>)',
                  r'\g<1>%d月%d日 · %s讲道\g<2>' % (d.month, d.day, wd), html, count=1)
    html = re.sub(r'(<span id="nowSub">)[^<]*(</span>)',
                  r'\g<1>%s\g<2>' % gospel_full, html, count=1)
    html = re.sub(r'(<audio id="audio"[^>]*src=")[^"]*(")',
                  r'\g<1>%s\g<2>' % first["mp3_url"], html, count=1)
    html = re.sub(r'(<a class="text-link" id="textLink" href=")[^"]*(")',
                  r'\g<1>%s\g<2>' % first["text_url"], html, count=1)
    # 计数与日期范围
    total_m = re.search(r"共 (\d+) 条", html)
    total = int(total_m.group(1)) + len(todo) if total_m else None
    if total:
        html = html.replace("共 %s 条" % total_m.group(1), "共 %d 条" % total, 1)
        html = re.sub(r"全部 \d+ 条", "全部 %d 条" % total, html, count=1)
    html = re.sub(r"2026年1月1日 — \d+月\d+日",
                  "2026年1月1日 — %d月%d日" % (d.month, d.day), html)
    html = re.sub(r'max="2026-\d{2}-\d{2}"', 'max="%s"' % first["date"], html)
    html = re.sub(r"按日期查找（2026年1月1日 — \d+月\d+日）",
                  "按日期查找（2026年1月1日 — %d月%d日）" % (d.month, d.day), html)
    # 12 月按钮：原来只有 1–11 月，新月份是 12 月时补一个
    if d.month == 12 and 'data-month="12"' not in html:
        html = html.replace(
            '<button class="month-btn" data-month="11">11月</button>',
            '<button class="month-btn" data-month="11">11月</button>\n      <button class="month-btn" data-month="12">12月</button>', 1)
    # 若新日期曾在缺失清单里，把它从 MISSING 数组里拿掉。
    # 注意：只能改 MISSING 数组那一段，不能在整页替换——新条目本身就含有
    # "2026-11-07" 这个日期，整页替换会把刚插进去的条目也删坏。
    mm = re.search(r"var MISSING = \[[^\]]*\];", html)
    if mm:
        fixed = mm.group(0)
        for e in todo:
            fixed = fixed.replace('"%s", ' % e["date"], "")
            fixed = fixed.replace('"%s"' % e["date"], "")
        fixed = fixed.replace(", ]", "]")
        html = html[:mm.start()] + fixed + html[mm.end():]

    # 往年今日：按新的置顶日期重建
    html, past_count = rebuild_past(html, first["date"], monthly_cache)

    open(INDEX_HTML, "w", encoding="utf-8").write(html)
    print("已更新：新增 %d 条，置顶改为 %s %s（%s），总数 %s 条；往年今日 %d 条。"
          % (len(todo), first["date"], first["title"], gospel_full, total, past_count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
