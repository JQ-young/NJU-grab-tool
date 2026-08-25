# -*- coding: utf-8 -*-
"""
自定义蹲课程序（独立版）
========================
在下方配置区填好账号、密码、要蹲的课程编号，运行后：
  1. 登录（密码自动填写，只需点一次验证码）
  2. 先扫描一遍：列出这些课程编号在当前选课批次里的教学班（菜单/校区/名额/是否已满）
  3. 进入蹲守：每 5 秒刷新一轮，目标课一旦有未满的（符合校区要求）就随机挑一个提交一次
  4. 抢到 MAX_GRAB 门后响铃退出（默认抢到 1 门就收手）

用法：
  python grab_custom.py                      # 用配置区里的账号和目标
  python grab_custom.py 00371690 78005020    # 命令行直接传课程编号（覆盖配置区列表）
  python grab_custom.py --dry                # 只看不抢：扫描并打印候选，绝不提交

依赖：同目录下的 grab_rush.py / nju_des.py（登录加密实现），pycryptodome、opencv-python。
"""

import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
import grab_rush as g

# ============================== 配置区（改这里） ==============================

STUDENT_NUMBER = ""                  # 你的学号          # 学号
PASSWORD = ""                  # 你的统一身份认证密码（明文存储，用完请删除并修改密码）               # 统一身份认证密码（明文存储，用完请删除并改密码）

# 要蹲的课程编号（8 位课程号，选课系统里的 courseNumber），可写多个
TARGET_COURSE_NUMBERS = [
    "00371690",      # 口语表达入门
    "78005020",      # 大学生生涯胜任力提升的理论与实践
]

CAMPUS_KEYWORD = "仙林"                # 只抢这个校区；留空 "" = 不限校区
OTHER_CAMPUS_MARKERS = ("鼓楼", "苏州", "浦口")   # 出现这些校区的课一律排除
MENUS = ["GG02", "GG01", "KZY", "TX", "TX01", "TX02", "TX03", "TX04",
         "ZY", "TY", "YD", "QB", "SC"]   # 搜索范围（教学班所属菜单），可按需删减
CYCLE_SECONDS = 5                     # 每轮刷新间隔（秒）
MAX_GRAB = 1                          # 抢到几门后停止（1 = 抢到一门就收手）
FAIL_COOLDOWN = 120                   # 提交被拒的教学班冷却时间（秒）

# ============================== 配置区结束 ==================================

GRABBED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grabbed_ids.txt")
CAMPUS_FIELDS = ("campusName", "campus", "schoolCampus", "teachingPlace")


def load_grabbed():
    try:
        with open(GRABBED_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    except OSError:
        return set()


def save_grabbed(ids):
    try:
        with open(GRABBED_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(ids)))
    except OSError:
        pass


def is_in_campus(c):
    """严格校区判断：必须明确出现 CAMPUS_KEYWORD，且不出现其他校区标志；无信息则排除。"""
    if not CAMPUS_KEYWORD:
        return True
    vals = [str(c[f]) for f in CAMPUS_FIELDS if c.get(f)]
    if not vals:
        return False
    joined = "".join(vals)
    if CAMPUS_KEYWORD not in joined:
        return False
    if any(m in joined for m in OTHER_CAMPUS_MARKERS):
        return False
    return True


def fetch_menu(s, menu):
    """按菜单拉课程列表（服务端只看未满+不冲突）。会话失效返回 (None, msg)。"""
    data = {"studentCode": g.STUDENT_NUMBER,
            "electiveBatchCode": g.BATCH_CODE,
            "teachingClassType": menu,
            "queryContent": ""}
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


def fetch_menu_full(s, menu):
    """同上但不做未满/冲突过滤（扫描用，能看到满员班）。"""
    data = {"studentCode": g.STUDENT_NUMBER,
            "electiveBatchCode": g.BATCH_CODE,
            "teachingClassType": menu,
            "queryContent": ""}
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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    targets = args if args else list(TARGET_COURSE_NUMBERS)
    targets = [t.strip() for t in targets if t.strip()]
    if not targets:
        g.log("❌ 没有目标课程编号：请在配置区 TARGET_COURSE_NUMBERS 填写，"
              "或命令行传入（python grab_custom.py 课程号...）")
        sys.exit(1)
    if dry:
        g.log("🧪 演练模式：只扫描和过滤，不提交任何选课请求（Ctrl+C 退出）")

    # 覆盖登录凭据（grab_rush 的登录流程读取这两个全局变量）
    g.STUDENT_NUMBER = STUDENT_NUMBER
    g.PASSWORD = PASSWORD

    s = g.build_session()
    g.log(f"目标课程编号: {targets}")
    g.log("正在登录（密码自动填写，请点验证码）...")
    while not g.login(s):
        time.sleep(1)
    s.headers.update({
        "Referer": g.BASE + "/*default/grablessons.do?token=" + s.headers.get("token", ""),
    })

    # ---------- 扫描：先看目标课的教学班分布 ----------
    g.log("=" * 20 + " 目标课程扫描 " + "=" * 20)
    tset = set(targets)
    for menu in MENUS:
        lst, err = fetch_menu_full(s, menu)
        if err is not None:
            g.log("会话失效，重新登录...")
            if not g.login(s):
                g.log("登录失败，退出")
                sys.exit(1)
            lst, err = fetch_menu_full(s, menu)
        for c in lst:
            if (c.get("courseNumber") or "") in tset:
                g.log(f"  菜单 {menu} | {c.get('courseName')} | 校区={c.get('campusName')} | "
                      f"已选={c.get('numberOfSelected')}/{c.get('classCapacity')} | "
                      f"isFull={c.get('isFull')} | 我已选={c.get('isChoose')} | ID={c.get('teachingClassID')}")
        time.sleep(0.4)

    # ---------- 蹲守 ----------
    g.log("=" * 20 + f" 蹲守开始（{CYCLE_SECONDS} 秒一轮，抢到 {MAX_GRAB} 门收手）" + "=" * 20)
    grabbed_ids = load_grabbed()
    cooldown = {}
    grabbed = []
    round_no = 0
    while len(grabbed) < MAX_GRAB:
        round_no += 1
        t0 = time.time()
        items, seen_ids, relogged = [], set(), False
        for menu in MENUS:
            try:
                lst, err = fetch_menu(s, menu)
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
        stat = {"total": len(items), "not_target": 0, "campus": 0, "full": 0,
                "chosen": 0, "cool": 0, "grabbed": 0}
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
            if (c.get("courseNumber") or "") not in tset:
                stat["not_target"] += 1
                continue
            if not is_in_campus(c):
                stat["campus"] += 1
                continue
            cands.append((menu, c))

        if dry:
            preview = ", ".join(f"{c.get('courseName')}({c.get('campusName')}, {c.get('numberOfSelected')}/{c.get('classCapacity')})"
                                for _m, c in cands[:10])
            g.log(f"[第{round_no}轮] 共{len(items)}门，目标未满候选 {len(cands)} 门: {preview}")
            sleep_to(t0)
            continue

        if not cands:
            g.log(f"[第{round_no}轮] 无候选（共{stat['total']}门：非目标{stat['not_target']} / "
                  f"非{CAMPUS_KEYWORD or '目标校区'}{stat['campus']} / 已满{stat['full']} / "
                  f"已选{stat['chosen']} / 冷却{stat['cool']} / 已抢{stat['grabbed']}），刷新重试")
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
                save_grabbed(grabbed_ids)
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

