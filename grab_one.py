# -*- coding: utf-8 -*-
"""单课程一次抢课：按课程号搜索教学班 → 挑一个未满未选的 → 提交一次 volunteer.do
用法: python grab_one.py 00410080
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
import grab_rush as g

COURSE_NUMBER = sys.argv[1] if len(sys.argv) > 1 else "00410080"
MENUS = ["", "GG", "GG01", "GG02", "TX", "TX01", "TX02", "TX03", "TX04",
         "KZY", "TY", "YD", "QB", "SC", "ZY"]


def search_menu(s, menu):
    try:
        r = s.post(g.URL_COURSE, data={
            "querySetting": json.dumps({
                "data": {"studentCode": g.STUDENT_NUMBER, "electiveBatchCode": g.BATCH_CODE,
                         "teachingClassType": menu, "queryContent": COURSE_NUMBER},
                "pageSize": "20", "pageNumber": "0"}),
        }, timeout=20)
        j = r.json()
        if str(j.get("code")) in ("2", "302") or "登录" in (j.get("msg") or ""):
            return None, j.get("msg")
        return j.get("dataList") or [], None
    except Exception as e:
        return None, str(e)


def main():
    s = g.build_session()
    g.log(f"目标课程号: {COURSE_NUMBER}")
    g.log("正在登录（密码自动填写，请点验证码）...")
    while not g.login(s):
        time.sleep(1)
    s.headers.update({
        "Referer": g.BASE + "/*default/grablessons.do?token=" + s.headers.get("token", ""),
    })

    found = {}
    for menu in MENUS:
        lst, err = search_menu(s, menu)
        if err is not None:
            g.log(f"菜单「{menu or '(全局)'}」会话失效，重新登录...")
            if g.login(s):
                lst, err = search_menu(s, menu)
            else:
                g.log("登录失败，退出")
                sys.exit(1)
        for c in lst:
            tid = c.get("teachingClassID")
            if tid not in found:
                found[tid] = (menu, c)
        if lst:
            g.log(f"菜单「{menu or '(全局)'}」命中 {len(lst)} 条")
        time.sleep(0.4)

    if not found:
        g.log("❌ 所有菜单都没搜到这门课（可能不在当前批次开放范围）")
        sys.exit(1)

    g.log(f"共找到 {len(found)} 个教学班:")
    pick = None
    for tid, (menu, c) in found.items():
        full = g._is_full(c)
        chosen = str(c.get("isChoose")) in ("1", "true", "True")
        g.log(f"  菜单={menu or '-'} | {c.get('courseName')} | 校区={c.get('campusName') or c.get('teachingPlace')} | "
              f"{c.get('numberOfSelected')}/{c.get('classCapacity')} | 已满={full} | 已选={chosen} | ID={tid}")
        if pick is None and not full and not chosen:
            pick = (menu, c)
        time.sleep(0.05)

    if pick is None:
        g.log("⚠️ 所有教学班都已是满员/已选状态，未提交")
        sys.exit(0)

    menu, c = pick
    c["teachingClassType"] = menu or "GG"
    g.log(f"选中「{c.get('courseName')}」（菜单={menu or 'GG'}，校区={c.get('campusName')}），提交一次...")
    try:
        _plain, post = g.build_add_param(c)
        r = s.post(g.URL_VOLUNTEER, data=post, timeout=10)
        j = r.json()
    except Exception as e:
        g.log(f"提交异常: {e}")
        sys.exit(1)
    code = str(j.get("code"))
    msg = j.get("msg") or ""
    g.log(f"服务器回复: code={code} msg={msg} | {r.text[:200]}")
    if code == "1":
        g.log(f"🎉🎉 抢到: {c.get('courseName')} ({c.get('teachingClassID')})")
        g.beep()
    else:
        g.log("未成功（见上方回复）")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        g.log("已手动停止（Ctrl+C）")
