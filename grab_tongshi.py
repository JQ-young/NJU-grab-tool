# -*- coding: utf-8 -*-
"""
南京大学选课平台 · 仙林通识课蹲课脚本 v2
====================================
目标：顶部菜单“公共”→ 通识课（菜单码 GG02，实测 kclx=导学/研讨/通识）
每 CYCLE_SECONDS 秒一轮：
  1. 调 publicCourse.do 拉取通识课列表（服务端过滤：只看未满 + 只看不冲突）
  2. 客户端再过滤：仙林校区 + 未满 + 未选 + 失败冷却期外
  3. 随机挑一门候选 → 每轮只提交一次 volunteer.do（“只点击一次”）
  4. 无候选 / 提交失败 → 刷新列表，下一轮再来
抢到 MAX_GRAB 门后响铃退出。

用法：
  python grab_tongshi.py           # 正常蹲课
  python grab_tongshi.py --dry     # 只看不抢：打印每轮候选，绝不提交（建议先跑这个核对）

登录复用 grab_rush.py：密码已配置自动填写，只需点一次验证码。
"""

import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
import grab_rush as g

# ============================== 配置区域 ==================================

MENU_CODE = "GG02"        # 通识课菜单码（顶部“公共”→通识课子菜单；GG02 实测 kclx=导学/研讨/通识）
CAMPUS_KEYWORD = "仙林"    # 校区关键词（客户端过滤 campusName）
OTHER_CAMPUS_MARKERS = ("鼓楼", "苏州", "浦口")   # 其他校区标志：任一出现即排除
FILTER_NOTFULL = "0"      # 服务端过滤：0=只看未满 | 2=不过滤（客户端仍会兜底过滤）
FILTER_CONFLICT = "0"     # 服务端过滤：0=只看不冲突 | 2=不过滤
CYCLE_SECONDS = 5         # 每轮间隔（秒）：“每 5 秒刷新一次”
MAX_GRAB = 99             # 最多抢几门（设大 = 持续蹲，出来一门抢一门；每抢到一门响铃但不退出）
FAIL_COOLDOWN = 120       # 提交失败/被拒的教学班冷却时间（秒），冷却期内不再选它

# ============================== 配置区域结束 ===============================

CAMPUS_FIELDS = ("campusName", "campus", "schoolCampus", "teachingPlace")
COURSE_TYPE_FIELDS = ("courseTypeName", "courseNatureName", "kclx")
TYPE_REJECT_MARKERS = ("思政", "专业", "体育")   # 类型字段明确含这些词且不含“通识”的课 → 排除

GRABBED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grabbed_ids.txt")


def load_grabbed():
    """读取已抢教学班ID黑名单（跨重启有效，防止重复抢同一门课）。"""
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


def campus_evidence(c):
    """收集课程所有校区/地点字段的值。"""
    return [str(c[f]) for f in CAMPUS_FIELDS if c.get(f)]


def is_in_campus(c):
    """严格校区判断（只要仙林）：
    1. 必须至少有一个校区/地点字段明确出现“仙林”；
    2. 任何校区/地点字段出现鼓楼/苏州/浦口 → 排除；
    3. 没有任何校区/地点信息 → 排除（宁可少抢，不抢错校区）。"""
    vals = campus_evidence(c)
    if not vals:
        return False
    joined = "".join(vals)
    if CAMPUS_KEYWORD not in joined:
        return False
    if any(m in joined for m in OTHER_CAMPUS_MARKERS):
        return False
    return True


def is_clearly_non_tongshi(c):
    """类型二次核对：类型字段明确是思政/专业/体育等且不含“通识”才排除；字段缺失不拦。"""
    for f in COURSE_TYPE_FIELDS:
        v = c.get(f)
        if v and "通识" not in str(v) and any(k in str(v) for k in TYPE_REJECT_MARKERS):
            return True
    return False


def search_public(s):
    """拉取通识课列表（publicCourse.do，一次返回全部）。
    会话失效返回 (None, msg)。"""
    data = {"studentCode": g.STUDENT_NUMBER,
            "electiveBatchCode": g.BATCH_CODE,
            "teachingClassType": MENU_CODE,
            "queryContent": ""}
    if FILTER_NOTFULL:
        data["checkCapacity"] = FILTER_NOTFULL
    if FILTER_CONFLICT:
        data["checkConflict"] = FILTER_CONFLICT
    r = s.post(g.BASE + "/elective/publicCourse.do", data={
        "querySetting": json.dumps({
            "data": data, "pageSize": "20", "pageNumber": "0", "order": "isChoose -"}),
    }, timeout=20)
    j = r.json()
    if str(j.get("code")) in ("2", "302") or "登录" in (j.get("msg") or ""):
        return None, j.get("msg")
    return j.get("dataList") or [], None


def sleep_to(t0):
    """补足本轮剩余时间，保证每轮间隔 ≥ CYCLE_SECONDS 且至少 0.5 秒。"""
    time.sleep(max(0.5, CYCLE_SECONDS - (time.time() - t0)))


def main():
    dry = "--dry" in sys.argv
    if dry:
        g.log("🧪 演练模式：只搜索和过滤，不提交任何选课请求（Ctrl+C 退出）")

    s = g.build_session()
    g.log("正在登录（密码已自动填写，请点验证码）...")
    while not g.login(s):
        time.sleep(1)
    s.headers.update({
        "Referer": g.BASE + "/*default/grablessons.do?token=" + s.headers.get("token", ""),
    })
    g.log(f"🎯 目标菜单 {MENU_CODE}（公共→通识课），批次 {g.BATCH_CODE}")

    cooldown = {}          # teachingClassID -> 冷却截止时间戳
    grabbed_ids = load_grabbed()
    if grabbed_ids:
        g.log(f"黑名单已加载 {len(grabbed_ids)} 个已抢教学班（不再重复抢）")
    grabbed = []
    round_no = 0
    while len(grabbed) < MAX_GRAB:
        round_no += 1
        t0 = time.time()
        try:
            items, err = search_public(s)
        except Exception as e:
            g.log(f"[第{round_no}轮] 查询异常: {e}")
            sleep_to(t0)
            continue
        if err is not None:
            g.log("会话失效，自动重新登录（再点一次验证码）...")
            if g.login(s):
                continue
            g.log("重新登录失败，30 秒后重试")
            time.sleep(30)
            continue

        if round_no == 1:
            seen = {}
            for c in items:
                for v in campus_evidence(c):
                    if "校区" in v or CAMPUS_KEYWORD in v:
                        seen[v[:50]] = seen.get(v[:50], 0) + 1
            g.log("校区取值分布（核对“只选仙林”过滤）：")
            for v, n in sorted(seen.items(), key=lambda kv: -kv[1]):
                g.log(f"   {n} 门: {v}")
            kclx = {}
            for c in items:
                v = c.get("kclx")
                if v:
                    kclx[str(v)[:40]] = kclx.get(str(v)[:40], 0) + 1
            g.log("kclx（课程性质）分布（核对是通识）：")
            for v, n in sorted(kclx.items(), key=lambda kv: -kv[1]):
                g.log(f"   {n} 门: {v}")

        cands = []
        stat = {"total": len(items), "campus": 0, "full": 0, "chosen": 0, "cool": 0, "type": 0, "grabbed": 0}
        for c in items:
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
            if not is_in_campus(c):
                stat["campus"] += 1
                continue
            if is_clearly_non_tongshi(c):
                stat["type"] += 1
                continue
            cands.append(c)

        if dry:
            def _cv(c):
                v = campus_evidence(c)
                return v[0][:14] if v else "校区未知"
            preview = ", ".join(
                f"{c.get('courseName')}({_cv(c)}, {c.get('numberOfSelected')}/{c.get('classCapacity')})"
                for c in cands[:10])
            g.log(f"[第{round_no}轮] 返回 {len(items)} 门，仙林未满候选 {len(cands)} 门: {preview}")
            sleep_to(t0)
            continue

        if not cands:
            g.log(f"[第{round_no}轮] 无候选（共{stat['total']}门：非仙林{stat['campus']} / "
                  f"非通识{stat['type']} / 已满{stat['full']} / 已选{stat['chosen']} / "
                  f"冷却{stat['cool']} / 已抢{stat['grabbed']}），刷新重试")
            sleep_to(t0)
            continue

        c = random.choice(cands)
        c["teachingClassType"] = MENU_CODE
        cid = c.get("teachingClassID")
        g.log(f"[第{round_no}轮] 候选 {len(cands)} 门，随机选「{c.get('courseName')}」提交（每轮一次）")
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
            continue   # 不退出：继续蹲下一门
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
        elif any(k in msg for k in ("冲突", "已选", "重复", "修读", "通过", "读过")):
            g.log(f"[第{round_no}轮] {c.get('courseName')} 被服务器拒绝（{msg[:40]}），冷却该课")
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
