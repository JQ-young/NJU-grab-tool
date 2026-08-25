# -*- coding: utf-8 -*-
"""
通识课部分 · 思政包课程：先检查，再按 5 秒一轮蹲守
=================================================
Phase 1（检查）：列出公共菜单下 GG01/GG02 里匹配思政包名单的课
  （课程名/课程号来自 grab_rush.TARGET_COURSES_RAW 思政课程包），
  打印每门的菜单、校区、已选/容量、是否已满、是否已选、kclx，以及两个菜单的已选门数。
Phase 2（蹲守）：5 秒一轮
  拉 GG01+GG02 列表（服务端只看未满+不冲突）→ 过滤：思政包名单 + 仙林 + 未选 + 黑名单/冷却外
  → 随机挑一门每轮只提交一次；抢到响铃、记黑名单、继续蹲；被"门数限制"拒绝的课自动冷却。

用法: python grab_sizheng.py [--dry]
"""
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
import grab_rush as g
import grab_tongshi as gt

MENUS = ["GG02", "GG01"]          # 通识课 / 公选（公共菜单的两个子菜单）
EXTRA_KEYWORDS = ("口语", "新媒体技术应用", "大学生生涯胜任力提升的理论和实践", "生涯胜任力")
# 额外目标关键词：课程名含任一即纳入目标（2026-08-25 用户依次要求：口语 / 新媒体技术应用 / 大学生生涯胜任力提升的理论和实践）
CYCLE_SECONDS = 5
MAX_GRAB = 1               # 抢到一门就收手（用户 2026-08-25 要求）
FAIL_COOLDOWN = 120


def build_sizheng():
    """从思政课程包名单解析 (课程名集合, 课程号集合)。类别词（油画等）不算。"""
    keys, numbers = set(), set()
    for line in g.TARGET_COURSES_RAW.splitlines():
        line = line.strip()
        if not line:
            continue
        numbers.update(re.findall(r"\d{8}[A-Z]?", line))
        text = re.sub(r"[\d;A-Za-z" "“”‘’《》""\s:：,，.．\-—()（）]+", "", line)
        if text and text not in g.CATEGORY_KEYS:
            keys.add(text)
    return keys, numbers


KEYS, NUMBERS = build_sizheng()


def is_sizheng(c):
    """目标判定：思政包（课程号/课程名）+ 额外关键词（如口语）。"""
    cn = c.get("courseNumber") or ""
    name = c.get("courseName") or ""
    if cn in NUMBERS:
        return True
    if any(k in name for k in KEYS):
        return True
    return any(k in name for k in EXTRA_KEYWORDS)


def fetch_menu(s, menu, filter_full=True):
    data = {"studentCode": g.STUDENT_NUMBER,
            "electiveBatchCode": g.BATCH_CODE,
            "teachingClassType": menu,
            "queryContent": ""}
    if filter_full:
        data["checkCapacity"] = "0"
        data["checkConflict"] = "0"
    r = s.post(g.BASE + "/elective/publicCourse.do", data={
        "querySetting": json.dumps({
            "data": data, "pageSize": "20", "pageNumber": "0", "order": "isChoose -"}),
    }, timeout=20)
    j = r.json()
    if str(j.get("code")) in ("2", "302") or "登录" in (j.get("msg") or ""):
        return None, j.get("msg")
    return j.get("dataList") or [], None


def sleep_to(t0):
    time.sleep(max(0.5, CYCLE_SECONDS - (time.time() - t0)))


def main():
    dry = "--dry" in sys.argv
    if dry:
        g.log("🧪 演练模式：只检查+过滤，不提交（Ctrl+C 退出）")

    s = g.build_session()
    g.log("正在登录（密码自动填写，请点验证码）...")
    while not g.login(s):
        time.sleep(1)
    s.headers.update({
        "Referer": g.BASE + "/*default/grablessons.do?token=" + s.headers.get("token", ""),
    })

    # ---------- Phase 1: 检查 ----------
    g.log("=" * 20 + " Phase 1 检查：通识课部分（GG01/GG02）的思政包+口语课程 " + "=" * 20)
    for menu in MENUS:
        lst, err = fetch_menu(s, menu, filter_full=False)
        if err is not None:
            g.log(f"菜单 {menu} 查询会话失效，重新登录...")
            if not g.login(s):
                g.log("登录失败，退出")
                sys.exit(1)
            lst, err = fetch_menu(s, menu, filter_full=False)
        chosen = [c for c in lst if str(c.get("isChoose")) in ("1", "true", "True")]
        hits = [c for c in lst if is_sizheng(c)]
        g.log(f"--- 菜单 {menu}：共 {len(lst)} 门，已选 {len(chosen)} 门，其中目标（思政包+口语）匹配 {len(hits)} 门 ---")
        for c in hits:
            g.log(f"   {c.get('courseName')} | 课程号={c.get('courseNumber')} | "
                  f"校区={c.get('campusName')} | 已选={c.get('numberOfSelected')}/{c.get('classCapacity')} | "
                  f"isFull={c.get('isFull')} | 已选课={c.get('isChoose')} | kclx={c.get('kclx')} | "
                  f"ID={c.get('teachingClassID')}")
        if menu == "GG02":
            kclx = {}
            for c in lst:
                v = c.get("kclx")
                if v:
                    kclx[str(v)] = kclx.get(str(v), 0) + 1
            g.log(f"   GG02 全菜单 kclx 分布: {kclx}")
        time.sleep(0.5)

    # ---------- Phase 2: 5 秒一轮蹲守 ----------
    g.log("=" * 20 + " Phase 2 蹲守开始（5 秒一轮，只抢：思政包/口语 + 仙林 + 未满 + 未选）" + "=" * 20)
    grabbed_ids = gt.load_grabbed()
    cooldown = {}
    grabbed = []
    round_no = 0
    while len(grabbed) < MAX_GRAB:
        round_no += 1
        t0 = time.time()
        items, seen_ids, relogged = [], set(), False
        for menu in MENUS:
            try:
                lst, err = fetch_menu(s, menu, filter_full=True)
            except Exception as e:
                g.log(f"[第{round_no}轮] 菜单 {menu} 查询异常: {e}")
                continue
            if err is not None:
                g.log("会话失效，自动重新登录（再点一次验证码）...")
                if g.login(s):
                    relogged = True
                else:
                    g.log("重新登录失败，30 秒后重试")
                    time.sleep(30)
                    relogged = True
                break
            for c in lst:
                cid = c.get("teachingClassID")
                if cid and cid not in seen_ids:
                    seen_ids.add(cid)
                    items.append((menu, c))
            time.sleep(random.uniform(0.3, 0.5))
        if relogged:
            continue

        cands = []
        stat = {"total": len(items), "sizheng": 0, "campus": 0, "full": 0, "chosen": 0,
                "cool": 0, "grabbed": 0}
        for menu, c in items:
            cid = c.get("teachingClassID")
            if cid and cid in grabbed_ids:
                stat["grabbed"] += 1
                continue
            if cid and time.time() < cooldown.get(cid, 0):
                stat["cool"] += 1
                continue
            if str(c.get("isChoose")) in ("1", "true", "True"):
                stat["chosen"] += 1
                continue
            if g._is_full(c):
                stat["full"] += 1
                continue
            if not is_sizheng(c):
                stat["sizheng"] += 1
                continue
            if not gt.is_in_campus(c):
                stat["campus"] += 1
                continue
            cands.append((menu, c))

        if dry:
            preview = ", ".join(f"{c.get('courseName')}({c.get('campusName')}, {c.get('numberOfSelected')}/{c.get('classCapacity')})"
                                for _m, c in cands[:10])
            g.log(f"[第{round_no}轮] 共{len(items)}门，目标+仙林+未满候选 {len(cands)} 门: {preview}")
            sleep_to(t0)
            continue

        if not cands:
            g.log(f"[第{round_no}轮] 无候选（共{stat['total']}门：非目标{stat['sizheng']} / "
                  f"非仙林{stat['campus']} / 已满{stat['full']} / 已选{stat['chosen']} / "
                  f"冷却{stat['cool']} / 已抢{stat['grabbed']}），刷新重试")
            sleep_to(t0)
            continue

        menu, c = random.choice(cands)
        c["teachingClassType"] = menu
        cid = c.get("teachingClassID")
        g.log(f"[第{round_no}轮] 候选 {len(cands)} 门，随机选「{c.get('courseName')}」"
              f"（菜单{menu}，{c.get('campusName')}）提交（每轮一次）")
        try:
            _plain, post = g.build_add_param(c)
            r = s.post(g.URL_VOLUNTEER, data=post, timeout=10)
            j = r.json()
        except Exception as e:
            g.log(f"[第{round_no}轮] 提交网络异常: {e}")
            sleep_to(t0)
            continue

        code = str(j.get("code"))
        msg = j.get("msg") or ""
        if code == "1":
            g.log(f"🎉🎉 抢到: {c.get('courseName')} ({cid})")
            g.notify("🎉 抢课成功", c.get("courseName"))
            g.beep()
            grabbed.append(c)
            if cid:
                grabbed_ids.add(cid)
                gt.save_grabbed(grabbed_ids)
            continue
        if code in ("2", "302") or any(k in msg for k in ("登录", "会话")):
            g.log("会话失效，自动重新登录...")
            if not g.login(s):
                g.log("重新登录失败，30 秒后重试")
                time.sleep(30)
            continue
        if any(k in msg for k in ("请求过快", "频率")):
            g.log(f"[第{round_no}轮] 请求过快，冷却 30 秒")
            if cid:
                cooldown[cid] = time.time() + FAIL_COOLDOWN
            time.sleep(30)
            continue
        if any(k in msg for k in ("已满", "人满", "满员", "名额", "课容量")):
            g.log(f"[第{round_no}轮] {c.get('courseName')} 已满，刷新重试")
        elif any(k in msg for k in ("冲突", "已选", "重复", "修读", "通过", "读过", "门数")):
            g.log(f"[第{round_no}轮] {c.get('courseName')} 被服务器拒绝（{msg[:50]}），冷却该课")
        else:
            g.log(f"[第{round_no}轮] 未预期响应: {msg[:80]} | {r.text[:120]}")
        if cid:
            cooldown[cid] = time.time() + FAIL_COOLDOWN
        sleep_to(t0)

    g.log(f"🏁 结束：成功 {len(grabbed)} 门")
    for c in grabbed:
        g.log("   ✅", c.get("courseName"))
    if grabbed:
        g.beep()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        g.log("已手动停止（Ctrl+C）")
