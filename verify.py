import json
import re
import sys
import urllib.parse

import requests

BASE = "http://127.0.0.1:8848"
ok_all = True


def check(name, path, expect=200, must_contain=None):
    global ok_all
    try:
        r = requests.get(BASE + path, timeout=20)
        status_ok = r.status_code == expect
        content_ok = True
        missing = ""
        if must_contain:
            for token in must_contain:
                if token not in r.text:
                    content_ok = False
                    missing = token
                    break
        flag = "OK " if (status_ok and content_ok) else "FAIL"
        if not (status_ok and content_ok):
            ok_all = False
        print(f"[{flag}] {name:<22} {path:<46} status={r.status_code} len={len(r.text)}"
              + (f" missing={missing!r}" if missing else ""))
        return r
    except Exception as e:
        ok_all = False
        print(f"[FAIL] {name:<22} {path:<46} ERR {e}")
        return None


print("=" * 96)
print("1) 访客页面（会员专属页返回门禁页）")
print("=" * 96)
check("首页/每日快讯(门禁)", "/", must_contain=["会员可见", "每日快讯", "登录后继续"])
check("股票统计(门禁)", "/stocks", must_contain=["会员可见", "股票统计"])
check("板块统计(门禁)", "/sectors", must_contain=["会员可见", "板块统计"])
check("全网热搜(开放)", "/news-wire", must_contain=["全网热搜", "热搜"])
check("全站搜索(开放)", "/search?q=" + urllib.parse.quote("算力"), must_contain=["全站搜索", "小时前"])
check("登录页", "/login", must_contain=["账号登录"])
check("注册页", "/register", must_contain=["注册账号"])
check("监控页(未登录跳转登录)", "/monitor-keywords", must_contain=["账号登录"])

print()
print("=" * 96)
print("2) API")
print("=" * 96)
r = check("API stats", "/api/stats")
r = check("API brief", "/api/brief?page_size=3")
if r:
    d = r.json()
    print(f"         brief total={d['total']} returned={len(d['rows'])}")
r = check("API stocks", "/api/stocks?hours=24")
if r:
    d = r.json()
    print(f"         stocks ranked={len(d)}")
    if d:
        print(f"         top1={d[0]['name']} {d[0]['code']} mentions={d[0]['mentions']}")
r = check("API sectors", "/api/sectors?hours=24")
if r:
    d = r.json()
    print(f"         sectors ranked={len(d)}")
    if d:
        print(f"         top1={d[0]['name']} mentions={d[0]['mentions']}")
r = check("API hot", "/api/hot?page=1")
if r:
    print(f"         hot total={r.json()['total']}")

print()
print("=" * 96)
print("3) 搜索功能（访客开放，受 3 小时延迟限制）")
print("=" * 96)
for term in ["算力", "芯片", "600519", "固态电池"]:
    r = check(f"搜索 {term}", "/search?q=" + urllib.parse.quote(term))
    if r:
        m = re.search(r"每日快讯 <strong>(\d+)</strong>", r.text)
        h = re.search(r"全网热搜 <strong>(\d+)</strong>", r.text)
        print(f"         访客可见 brief={m.group(1) if m else '?'} hot={h.group(1) if h else '?'}")

print()
print("=" * 96)
print("4) 注册 / 登录 / 会员分层")
print("=" * 96)
s = requests.Session()
uname = "demo_admin"
r = s.post(BASE + "/register", data={"username": uname, "password": "demo123456"},
           timeout=20, allow_redirects=False)
print(f"         注册 {uname}: status={r.status_code} -> {r.headers.get('location')}")

r = s.post(BASE + "/login", data={"username": uname, "password": "demo123456", "remember": "on"},
           timeout=20, allow_redirects=False)
print(f"         登录: status={r.status_code} cookie={'db_session' in s.cookies.get_dict()}")

r = s.get(BASE + "/", timeout=20)
has_gate = "member-only-page" in r.text
print(f"         登录后首页: 会员视图={'是' if not has_gate else '否'} len={len(r.text)} 条目={len(re.findall(r'<article class=.item', r.text))}")

r = s.get(BASE + "/stocks?hours=24", timeout=20)
print(f"         会员股票统计: 排名行数={len(re.findall(r'<td><span class=.rank', r.text))}")
r = s.get(BASE + "/sectors?hours=24", timeout=20)
print(f"         会员板块统计: 排名行数={len(re.findall(r'<td><span class=.rank', r.text))}")

r = s.get(BASE + "/search?q=" + urllib.parse.quote("算力"), timeout=20)
m = re.search(r"每日快讯 <strong>(\d+)</strong>", r.text)
print(f"         会员搜索『算力』: brief={m.group(1) if m else '?'}")

r = s.get(BASE + "/monitor-keywords", timeout=20)
print(f"         监控页: status={r.status_code} 含关键词表单={'添加监控' in r.text}")
r = s.post(BASE + "/monitor-keywords", data={"keyword": "算力"}, timeout=20, allow_redirects=False)
print(f"         添加监控关键词: status={r.status_code}")
r = s.get(BASE + "/monitor-keywords?kw=" + urllib.parse.quote("算力"), timeout=20)
print(f"         命中查询: 含命中={'命中' in r.text}")

print()
print("=" * 96)
print("5) 触发采集 API")
print("=" * 96)
r = s.post(BASE + "/api/refresh", timeout=180)
print(f"         /api/refresh: status={r.status_code}")
if r.status_code == 200:
    d = r.json()
    print(f"         stats={json.dumps(d['stats'], ensure_ascii=False)}")

print()
print("=" * 96)
print("总判定:", "全部通过 PASS" if ok_all else "存在失败项 FAIL")
print("=" * 96)
sys.exit(0 if ok_all else 1)
