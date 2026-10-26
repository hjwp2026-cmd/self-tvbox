#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自维护 TVBox 接口体检脚本
1. 拉取上游直播列表(nigx 镜像,电视可直连)
2. 逐线路测活:HTTP 200 + HLS/TS/FLV 内容校验 + HLS 分片级复核
3. 生成 live.txt:每频道保留最快 3 条,连续失败 2 次才剔除(防误杀)
4. 点播源存活探测,生成 tvbox.json
5. 写 status.md 体检报告
上游挂了时保留现有 live.txt 不动,只更新报告 —— 这正是自己维护的意义。
"""
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.request

UA = "okhttp/4.9.0"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 上游(别人维护的素材库,挂了不影响我们已固化的列表)
UPSTREAM_URL = "https://down.nigx.cn/raw.githubusercontent.com/doubletree6/tvbox-iptv-subscription/main/cn_all_tvbox_multi.txt"
# 自家产物地址(供 tvbox.json 自引用)
SELF_LIVE = "https://down.nigx.cn/raw.githubusercontent.com/hjwp2026-cmd/self-tvbox/main/live.txt"
# 点播聚合源(精选,后续发现好源往这里加)
VOD_SOURCES = [
    ("aowu", "https://9763.kstore.vip/aowu.json"),
    ("王小二", "https://9280.kstore.vip/newwex.json"),
]

MAX_PER_CHANNEL = 3      # 每频道最多保留线路数
MAX_FAIL = 2             # 连续失败 N 次才剔除
TIMEOUT = 8              # 每线路探测超时(秒)
THREADS = 24             # 并发数
UPSTREAM_FAIL_RETRY = 2  # 上游连续失败几次后本次跳过更新


def http_get(url, timeout=TIMEOUT, limit=None):
    """GET 前 limit 字节,返回 (status, bytes, headers)。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read(limit) if limit else r.read(), r.headers


def looks_like_stream(blob):
    """判断响应体是否为可播的流(HLS 列表 / TS / FLV)。"""
    if not blob:
        return False
    head = blob[:2048]
    if head[:3] == b"FLV":
        return True
    if head.startswith(b"\x47"):  # TS sync byte
        return True
    text = head.decode("utf-8", "ignore")
    if "EXTM3U" in text:
        return True
    if ".m3u8" in text or ".m3u" in text or ".ts" in text:
        return True
    return False


def probe(url):
    """测活单条线路。返回 (ok, 耗时ms)。HLS 做分片级复核。"""
    t0 = time.time()
    try:
        status, blob, _ = http_get(url, limit=4096)
        if status != 200 or not looks_like_stream(blob):
            return False, 0
        ms = int((time.time() - t0) * 1000)
        # HLS 分片复核:拉第一个分片,能取到字节才算稳
        if b"EXTM3U" in blob[:2048]:
            text = blob[:4096].decode("utf-8", "ignore")
            m = re.search(r"(https?://[^\s#]+?\.ts[^\s#]*)", text)
            if not m:
                m = re.search(r"(https?://[^\s#]+(?:\.m3u8|\.ts)[^\s#]*)", text)
            if m:
                try:
                    fstatus, fblob, _ = http_get(m.group(1), limit=2048)
                    if fstatus != 200 or not fblob:
                        return False, 0
                except Exception:
                    return False, 0
        return True, ms
    except Exception:
        return False, 0


def parse_live(text):
    """解析上游 TXT -> [(分类, 频道名, url)]。"""
    items = []
    cat = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.endswith("#genre#"):
            cat = line[: -len("#genre#")].strip().rstrip(",")
            continue
        if "," in line:
            name, url = line.split(",", 1)
            name, url = name.strip(), url.strip()
            if url and name:
                items.append((cat, name, url))
    return items


def load_state():
    p = os.path.join(BASE, "state.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state):
    json.dump(state, open(os.path.join(BASE, "state.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


def build_live(items, state):
    """分组 -> 优选 -> 防抖,生成 live.txt 行。"""
    grouped = {}
    for cat, name, url in items:
        grouped.setdefault((cat, name), []).append(url)

    chosen = []  # (cat, name, url)
    changed = {"added": 0, "kept": 0, "dropped": 0, "degraded": 0}
    for (cat, name), urls in grouped.items():
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=THREADS) as ex:
            for url, (ok, ms) in zip(urls, ex.map(probe, urls)):
                results.append((url, ok, ms))
        ok_items = [(u, ms) for u, ok, ms in results if ok]
        bad_items = [(u, ms) for u, ok, ms in results if not ok]
        picks = []
        for u, ms in ok_items:
            state.setdefault(u, {"fail": 0})["fail"] = 0
            picks.append((u, ms))
        for u, _ in bad_items:
            st = state.setdefault(u, {"fail": 0})
            st["fail"] += 1
            if st["fail"] < MAX_FAIL:
                picks.append((u, 10 ** 9))  # 降级保留,排最后
        picks.sort(key=lambda x: x[1])
        for u, _ in picks[:MAX_PER_CHANNEL]:
            chosen.append((cat, name, u))

    # 写文件,统计与旧版差异
    old = set()
    oldp = os.path.join(BASE, "live.txt")
    if os.path.exists(oldp):
        for line in open(oldp, encoding="utf-8"):
            line = line.strip()
            if line and "," in line and not line.endswith("#genre#"):
                old.add(line.split(",", 1)[1])
    new_lines = []
    counts = {}
    for cat, name, url in chosen:
        counts[cat] = counts.get(cat, 0) + 1
        if url not in old:
            changed["added"] += 1
    new_lines.append("")
    for cat, name, url in chosen:
        new_lines.append(f"{name},{url}")
    # 分组:按原顺序输出且保留分类行
    out = []
    cur_cat = None
    for cat, name, url in chosen:
        if cat != cur_cat:
            if cat:
                out.append(f"{cat},#genre#")
            cur_cat = cat
        out.append(f"{name},{url}")
    old_set_lines = set(old)
    for _, _, url in chosen:
        if url in old_set_lines:
            changed["kept"] += 1
    for u in old_set_lines:
        if not any(url == u for _, _, url in chosen):
            changed["dropped"] += 1
    with open(oldp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return counts, changed


def check_vod():
    """探测点播源,返回存活列表。"""
    alive = []
    for name, url in VOD_SOURCES:
        try:
            status, blob, _ = http_get(url, timeout=10, limit=2048)
            if status == 200 and (b"spider" in blob or b"urls" in blob or b"lives" in blob):
                alive.append({"name": name, "url": url, "type": 0})
        except Exception:
            pass
    return alive


def write_tvbox(alive):
    cfg = {
        "urls": alive,
        "lives": [{"name": "自维护直播·实测", "url": SELF_LIVE, "type": 0}],
        "spider": "",
    }
    json.dump(cfg, open(os.path.join(BASE, "tvbox.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def write_status(upstream_ok, counts, changed, alive, skipped=False):
    lines = [
        "# 体检报告",
        "",
        f"- 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"- 上游抓取: {'✅ 成功' if upstream_ok else '❌ 失败(继续使用现有列表)'}",
        f"- 本次{'跳过线路更新(上游连续失败)' if skipped else '执行线路体检'}",
        f"- 直播频道: {sum(counts.values())} 个(分类: " +
        ", ".join(f"{k} {v}" for k, v in counts.items()) + ")",
        f"- 变更: 新增 {changed['added']} · 保留 {changed['kept']} · 剔除 {changed['dropped']} · 降级保留 {changed['degraded']}",
        f"- 点播源: " + ("、".join(a["name"] for a in alive) if alive else "全部失效 ❌"),
        "",
    ]
    open(os.path.join(BASE, "status.md"), "w", encoding="utf-8").write("\n".join(lines))


def main():
    skipped = False
    upstream_ok = False
    counts, changed = {}, {"added": 0, "kept": 0, "dropped": 0, "degraded": 0}
    st = load_state()
    up_st = st.setdefault("_upstream", {"fail": 0})
    try:
        status, blob, _ = http_get(UPSTREAM_URL, timeout=15)
        if status == 200 and blob:
            up_st["fail"] = 0
            upstream_ok = True
        else:
            raise RuntimeError(f"upstream status {status}")
    except Exception:
        up_st["fail"] += 1
        upstream_ok = False

    if upstream_ok and up_st["fail"] == 0:
        items = parse_live(blob.decode("utf-8", "ignore"))
        if not items:
            raise RuntimeError("上游解析为空")
        counts, changed = build_live(items, st)
        save_state(st)
    elif up_st["fail"] >= UPSTREAM_FAIL_RETRY:
        skipped = True

    alive = check_vod()
    write_tvbox(alive)
    write_status(upstream_ok, counts, changed, alive, skipped)
    print(f"[ok] 上游={'过' if upstream_ok else '挂'} 频道={sum(counts.values())} "
          f"新增={changed['added']} 剔除={changed['dropped']} 点播={[a['name'] for a in alive]}")
    return 0 if (upstream_ok or skipped) and alive else 1


if __name__ == "__main__":
    sys.exit(main())