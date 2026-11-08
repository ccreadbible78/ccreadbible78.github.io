#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日讲道自动更新 v3：只读抓思高原站最新讲道，验过 MP3 能打开后，
把新条目插到 index.html 顶部的 ENTRIES 数组，并同步更新置顶区；
同时按置顶日期重建“同一礼仪日”区块——同一天主教礼仪日在不同年份的讲道。
- 缺失的中间日期会一并补回（不只补最新一天）。
- 同一礼仪日按礼仪身份匹配，不是同一公历日期：
  庆节（如拉特朗 11 月 9 日）找往年同一庆节；
  主日按“同一主日＋同一甲/乙/丙年”找（每三年一次，日期会差几天）；
  平日按“同一周＋同一星期＋同一单/双数年”找（每两年一次）；
  庆节当天被取代的平日（如 11 月 9 日的常年期三十二週星期一）另成一组。
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


# ---------------- 同一礼仪日（v3 新增） ----------------

def norm_title(t):
    """标题归一化，供比对用：去全部空白；旧页的 卅/廿 换成 三十/二十；
    去掉开头的甲年/乙年/丙年（比对时由候选年份本身保证年循环一致）。"""
    if not t:
        return ""
    s = re.sub(r"\s+", "", t)
    s = s.replace("卅", "三十").replace("廿", "二十")
    s = re.sub(r"^(甲年|乙年|丙年)", "", s)
    return s


def classify(top_title, top_date):
    """把置顶条目的标题分成一类，返回 (kind, key, label)。
    kind: feast（ dated 庆节）/ sunday / weekday / other。
    key：比对用的归一化键；label：组标题。"""
    y, mo, d = int(top_date[:4]), int(top_date[5:7]), int(top_date[8:10])
    raw = top_title.strip()
    m = re.match(r"^\s*(\d{1,2})月(\d{1,2})日\s*(.+?)\s*$", raw)
    if m:
        return ("feast", norm_title(m.group(3)), m.group(3).strip())
    nt = norm_title(raw)
    # 组标题去掉开头的甲/乙/丙年，避免“甲年…… · 甲年”重复
    base = re.sub(r"^(甲年|乙年|丙年)\s*", "", raw)
    if "主日" in nt:
        if "將臨期" in nt or mo == 12:
            idx = (y - 2022) % 3
        else:
            idx = (y - 2023) % 3
        cyc = ["甲", "乙", "丙"][idx]
        return ("sunday", nt, "%s · %s年" % (base, cyc))
    if "星期" in nt:
        par = "雙數年" if y % 2 == 0 else "單數年"
        return ("weekday", nt, "%s · %s" % (base, par))
    return ("other", nt, base)


def get_block(cache, year, block_start):
    """取一页双月页（带缓存）。block_start 是奇数月：1、3、5、7、9、11。"""
    key = (year, block_start)
    if key not in cache:
        cache[key] = fetch(bimonthly_url(year, block_start))
    return cache[key]


def parse_monthly_all(html):
    """解析一页双月页的全部主条目（不含 -1 专题）。
    旧年份月度页标题不是链接、只有裸 <span>，所以按 <tr> 行解析，
    标题取第二个 <td> 去标签后、“福音”之前的文字（不要取第一个 <span>，
    旧页第一个 <span> 可能是日期或福音）。返回 list[dict]。"""
    entries = []
    for tr in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
        block = tr.group(1)
        m = re.search(r"https://www\.ccreadbible\.net/bona/\d+homily/(\d{8})(-1)?\.mp3", block)
        if not m:
            continue
        ymd = m.group(1)
        if m.group(2):
            continue  # -1 专题不参与礼仪日匹配
        cells = re.findall(r"<td[^>]*>(.*?)</td>", block, flags=re.S | re.I)
        title = ""
        if len(cells) >= 2:
            txt = re.sub(r"<[^>]+>", "", cells[1])
            title = txt.split("福音")[0].strip()
        g = re.search(r"福音[:：]\s*([^<\s]+)", block)
        gospel = g.group(1).strip() if g else "無"
        y, mo, d = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])
        entry = {
            "date": "%04d-%02d-%02d" % (y, mo, d),
            "title": title,
            "gospel": gospel,
            "mp3_url": m.group(0),
        }
        fm = re.match(r"^\s*\d{1,2}月\d{1,2}日\s*(.+?)\s*$", title)
        entry["feast_name"] = fm.group(1).strip() if fm else None
        entries.append(entry)
    return entries


def blocks_for_window(year, center_md, days=14):
    """置顶日期前后 ±days 天覆盖到的双月块，按时间顺序返回 [(年, 块首月)]。
    同一礼仪日（如主日/平日）在别年会落在相差几天的日期上，用窗口找。"""
    center = datetime.date(year, center_md[0], center_md[1])
    blocks = []
    seen = set()
    cur = center - datetime.timedelta(days=days)
    end = center + datetime.timedelta(days=days)
    while cur <= end:
        bs = cur.month if cur.month % 2 == 1 else cur.month - 1
        key = (cur.year, bs)
        if key not in seen:
            seen.add(key)
            blocks.append(key)
        cur += datetime.timedelta(days=1)
    return blocks


def find_by_title(year, target_norm, center_md, cache):
    """在候选年份、置顶日期前后两周内，找归一化标题相同的第一条主条目。"""
    for (yy, bs) in blocks_for_window(year, center_md):
        try:
            html = get_block(cache, yy, bs)
        except Exception as e:  # noqa: BLE001
            print("月度页打不开，跳过 %d 年 %02d 块：%s" % (yy, bs, e))
            continue
        for e in parse_monthly_all(html):
            if norm_title(e["title"]) == target_norm:
                return e
    return None


def find_feast(year, month, day, feast_norm, cache):
    """在候选年份的同一月日，找庆节名相同的条目（固定日期的庆节）。"""
    bs = month if month % 2 == 1 else month - 1
    try:
        html = get_block(cache, year, bs)
    except Exception as e:  # noqa: BLE001
        print("月度页打不开，跳过 %d 年庆节查找：%s" % (year, e))
        return None
    for e in parse_monthly_all(html):
        if e["feast_name"] is None:
            continue
        em, ed = int(e["date"][5:7]), int(e["date"][8:10])
        if (em, ed) == (month, day) and norm_title(e["feast_name"]) == feast_norm:
            return e
    return None


def verify_entry(entry):
    """验 MP3 能打开、算大小、核实文字页；验不过返回 None。"""
    try:
        ok, size = head_mp3_info(entry["mp3_url"])
    except Exception as exc:  # noqa: BLE001
        print("同一礼仪日 MP3 验不过，跳过 %s：%s" % (entry["mp3_url"], exc))
        return None
    if not ok:
        print("同一礼仪日 MP3 验不过（不是音频或太小），跳过：%s" % entry["mp3_url"])
        return None
    entry["size"] = size or ""
    ymd = entry["date"].replace("-", "")
    text_url = "%s/homily-%s/homily-%s.html" % (BASE_INFO, ymd[:4], ymd)
    if text_page_exists(text_url):
        entry["text_url"] = text_url
    entry.pop("feast_name", None)
    return entry


def parse_entries_index(html):
    """从 var ENTRIES 数组读出 (date, title) 列表（从新到旧），供找最近的主日锚点用。"""
    idx = []
    m = re.search(r"var ENTRIES = \[(.*?)\];", html, flags=re.S)
    if not m:
        return idx
    for em in re.finditer(r'"date":\s*"(\d{4}-\d{2}-\d{2})"[^}]*?"title":\s*"([^"]+)"', m.group(1)):
        idx.append((em.group(1), em.group(2)))
    return idx


def render_liturgical_block(groups):
    """渲染同一礼仪日区块的静态 HTML（标题＋各组＋各行）。
    行用 .past-item（页面 JS 按此 class 绑定点播），组标题用 .lit-group。"""
    parts = []
    parts.append('  <div class="section-head">')
    parts.append('    <h3>同一礼仪日</h3><p>同一天主教礼仪日 · 不同年份的讲道</p>')
    parts.append('  </div>')
    parts.append('  <div class="rule"></div>')
    for g in groups:
        parts.append('')
        parts.append('  <div class="lit-group"><h4>%s</h4></div>' % g["label"])
        parts.append('  <div class="list">')
        for e in g["entries"]:
            yy, mm, dd = e["date"][:4], int(e["date"][5:7]), int(e["date"][8:10])
            parts.append('    <article class="item past-item" data-src="%s">' % e["mp3_url"])
            parts.append('      <div class="datebox"><b>%s</b><span>%d月%d日</span></div>' % (yy, mm, dd))
            parts.append('      <div class="item-main">')
            parts.append('        <h4>%s</h4>' % e["title"])
            parts.append('        <p>福音：%s</p>' % e["gospel"])
            parts.append('        <div class="meta">%s年%d月%d日 · MP3 %s · 思高读经推广中心</div>'
                         % (yy, mm, dd, e.get("size", "").replace("约", "约 ", 1)))
            parts.append('      </div>')
            parts.append('      <button class="mini-play" aria-label="播放%s年%d月%d日讲道">'
                         '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></button>'
                         % (yy, mm, dd))
            parts.append('    </article>')
        parts.append('  </div>')
    return "\n".join(parts)


def build_liturgical(html, top_date, top_title, cache, entries_index):
    """按置顶日期与标题重建同一礼仪日：重写 var LITURGICAL 数组与标记间的静态区块。
    返回 (html, 条数, 组数)。"""
    y, mo, d = int(top_date[:4]), int(top_date[5:7]), int(top_date[8:10])
    kind, key, label = classify(top_title, top_date)
    groups = []
    # 主组：按礼仪身份往回找，直到凑够 3 条（某年缺失就跳过、继续往前找，
    # 如常年期三十二週星期一：2024、2022 有，2020 是拉特朗庆节、2018 当天原站无讲道，
    # 要一直找到 2016 才凑满 3 条）。最多往前试 6 个周期，避免原站太旧无数据时空转。
    step = {"feast": 1, "sunday": 3, "weekday": 2, "other": 1}[kind]
    main_entries = []
    py = y - step
    tries = 0
    while len(main_entries) < 3 and tries < 6:
        tries += 1
        if kind == "feast":
            found = find_feast(py, mo, d, key, cache)
        else:
            found = find_by_title(py, key, (mo, d), cache)
        if found:
            v = verify_entry(found)
            if v:
                main_entries.append(v)
        py -= step
    if main_entries:
        groups.append({"label": label, "entries": main_entries})
    # 第二组：庆节当天被取代的平日（仅当置顶是庆节且不是主日）
    top_wd = datetime.date(y, mo, d).weekday()  # Mon=0 .. Sun=6
    if kind == "feast" and top_wd != 6:
        anchor = None
        for (edate, etitle) in entries_index:
            if edate <= top_date and "主日" in norm_title(etitle):
                anchor = (edate, etitle)
                break
        if anchor:
            m2 = re.search(r"(常年期|將臨期|聖誕期|四旬期|復活期)第(.+?)主日",
                           norm_title(anchor[1]))
            if m2:
                season, week_cn = m2.group(1), m2.group(2)
                wd_char = ["一", "二", "三", "四", "五", "六"][top_wd]
                target = norm_title("%s第%s週星期%s" % (season, week_cn, wd_char))
                par = "雙數年" if y % 2 == 0 else "單數年"
                sub_entries = []
                py2 = y - 2
                tries2 = 0
                while len(sub_entries) < 3 and tries2 < 6:
                    tries2 += 1
                    found = find_by_title(py2, target, (mo, d), cache)
                    if found:
                        v = verify_entry(found)
                        if v:
                            sub_entries.append(v)
                    py2 -= 2
                if sub_entries:
                    sub_label = "%s · %s" % (sub_entries[0]["title"], par)
                    groups.append({"label": sub_label, "entries": sub_entries})
    if not groups:
        print("同一礼仪日没找到往年条目，区块会是空的。")
    lit_json = json.dumps(groups, ensure_ascii=False)
    html, n = re.subn(r"var LITURGICAL = \[.*?\];",
                      "var LITURGICAL = %s;" % lit_json, html, count=1, flags=re.S)
    if not n:
        print("index.html 里找不到 var LITURGICAL，同一礼仪日数组没写进去。")
    block = render_liturgical_block(groups)
    html, n2 = re.subn(r"<!-- LITURGICAL-START -->.*?<!-- LITURGICAL-END -->",
                       "<!-- LITURGICAL-START -->\n%s\n  <!-- LITURGICAL-END -->" % block,
                       html, count=1, flags=re.S)
    if not n2:
        print("index.html 里找不到同一礼仪日标记，区块没写进去。")
    return html, sum(len(g["entries"]) for g in groups), len(groups)


# ---------------- 主流程 ----------------

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

    # 同一礼仪日：按新的置顶日期与标题重建（组结构＋静态行一并重写）
    entries_index = parse_entries_index(html)
    html, lit_count, lit_groups = build_liturgical(
        html, first["date"], first["title"], monthly_cache, entries_index)

    open(INDEX_HTML, "w", encoding="utf-8").write(html)
    print("已更新：新增 %d 条，置顶改为 %s %s（%s），总数 %s 条；同一礼仪日 %d 组 %d 条。"
          % (len(todo), first["date"], first["title"], gospel_full, total, lit_groups, lit_count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
