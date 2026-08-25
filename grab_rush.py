# -*- coding: utf-8 -*-
"""
南京大学选课平台 自动抢课脚本 v2（多课程 + 全自动登录）
====================================================
流程：
  1. 启动时弹出密码框（输入你的统一身份认证密码，仅存内存、不上传不打印）
  2. 弹出验证码窗口，按提示点 4 个字 → 完成登录（与浏览器同款加密：DES "this/password/is"）
  3. 按 TARGET_COURSES_RAW 里的课程名单逐一搜索解析教学班
  4. 校准服务器时间，保活；开放前 3 分钟强制重新登录保证会话新鲜
  5. 13:30 准点开始逐门轮询抢课：抢到响铃；时间冲突/已满的由服务器拒绝，脚本继续下一门
  6. 会话失效自动重新登录（再点一次验证码即可）

⚠️ 风险自担：使用脚本选课可能违反学校规定；频率默认温和（每门课间隔 0.05~0.15 秒）。
"""

import base64
import datetime
import json
import random
import re
import sys
import time

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

import nju_des

# ============================== 配置区域 ==================================

STUDENT_NUMBER = ""                  # 学号：登录前填你自己的                 # 学号
OPEN_TIME = "2026-08-24 13:30:00"            # 选课开放时刻
BATCH_CODE = "e4f378480a6440b1a332dd672c823ee2"   # 选课批次代码（【老生】2026秋课程补选）
AES_KEY = "wHm1xj3afURghi0c"                 # 抢课请求 AES 密钥（用户浏览器控制台 avy 实测值，2026-08-24）
COURSE_KIND_OVERRIDE = ""                    # 现行前端对本次批次取空串；报"课程种类"错误时可改 "1"
TARGET_TYPE_DEFAULT = "GG"                   # 实测诊断：这些课属于公选课菜单（GG），非可选时自动换 GG01/GG02/ZY 重试

# 目标课程名单（课程号+名称，来自思政选择性必修课课程包；逐门都抢，能抢几门是几门）
TARGET_COURSES_RAW = """
43000030中国共产党史
06010500;02111060;43000230中华人民共和国史
43000250改革开放史
00302440理解马克思
00371720走近中华优秀传统文化
00310820认识中国
78006400
新青年·习党史——纽扣课堂
00330030当代社会科学视域中的马克思主义哲学
00371760马克思主义与中国
00372500看见马克思
00532850马克思与古典政治经济学
04010030A马克思主义哲学史（上）
04010030B马克思主义哲学史（下）
00372380马克思主义与当代中国女性发展
04010120马克思主义哲学原理
04010740马克思主义哲学原著选读
04011220马克思主义哲学基本问题
05010360实践中的马克思主义新闻观
04011350马克思主义哲学与当代中国问题研究
43000070马克思主义与中国传统文化
43000010马克思主义理论导论
43000020A马克思主义发展史（上）
43000020B马克思主义发展史（下）
43000080中国化马克思主义经典著作导读
43000090马克思主义经典著作导读
43000100国外马克思主义原著导读
03001820习近平法治思想概论
43000060习近平新时代中国特色社会主义思想专题研究
00523090中国共产党革命理论的阅读与接受史
00522820当代中国史研究概论
02111350中国共产党革命史前沿问题
00532670当代中国史研究：史料与著作研读
43000110中国共产党党建专题研究
02000050看一台中国历史的大戏
02000100D中国通史（四）
02100110中国史专题研究
03000210中国法律思想史
03000020中国法律史
03000030宪法学
04010050中国伦理思想史
04011400中国传统伦理思想与当代道德建设
04011460文明交流视域下的中国传统思想
05000800中国传统文化与传播
05010530理解当代中国
08010070中国社会思想史
09000010B;43000170中国特色社会主义政治经济学
02110230;09010040中国经济史
10010090中国思想经典
23000220中国特色社会主义理论与实践研究
37100330 周易与中国文化
37100460中国古代社会与古代思想文化
43000200科技革命与马克思主义
77003080
78005470
43000280
78003740
04011150
04011300
77003340
00372230
00360090
00202310
78004540
04011400
00330850
00310100
32010160
00310060
02110990
周易与中国古典智慧
中华文化经典研读
毛泽东思想导论
禅与中国文化
20世纪中国马克思主义研究专题
马克思主义哲学：我们时代的真理和良心
儒家伦理与现代社会
中国传统哲学的智慧
中国书画鉴赏
现代中国的形成
庄子与中国思想
中国传统伦理思想与当代道德建设
中国古代人生哲学
胜解孙子兵法
国家安全学
二十世纪的中国民族与边疆
一带一路上的文明传承与生态变迁
红楼梦
油画
素描
摄影
剪辑
"""

# ---- 节奏 ----
LEAD_TIME = 0.5                # 提前多少秒开始发请求
RUSH_MIN_SLEEP = 0.8           # 每门课之间的最小间隔（秒）。实测太快会被服务器踢会话（"请求过快"）
RUSH_MAX_SLEEP = 1.5           # 每门课之间的最大间隔（秒）
MAX_RUSH_SECONDS = 1800        # 最多持续抢多久（秒）
RELOGIN_AT_T_MINUS = 180       # 开放前多少秒强制重新登录（保证会话新鲜）

# ---- 通知 ----
FANTANG = False                # Server酱 微信通知（https://sct.ftqq.com）
FANTANG_API = ""

TEST_MODE = ""                 # ""=正常 | "dry"=只解析名单+离线自检 | "now"=立即实弹开抢

# ============================== 配置区域结束 ===============================

BASE = "https://xk.nju.edu.cn/xsxkapp/sys/xsxkapp"
URL_LOGIN = BASE + "/student/check/login.do"
URL_VCODE = BASE + "/student/4/vcode.do"
URL_XKXF = BASE + "/student/xkxf.do"
URL_FAV = BASE + "/elective/queryfavorite.do"
URL_COURSE = BASE + "/elective/queryCourse.do"
URL_VOLUNTEER = BASE + "/elective/volunteer.do"

BEIJING = datetime.timezone(datetime.timedelta(hours=8))
PASSWORD = None   # 运行期从密码框读取，只存内存
DEFAULT_PASSWORD = ""   # 留空=每次弹密码框；想自动填就写这里（明文，注意安全）   # ⚠️ 明文存储！用户确认未改密码（2026-08-25）并要求自动输入。选课结束后请删除此行并修改密码

def log(*args):
    print(datetime.datetime.now().strftime("%H:%M:%S"), *args, flush=True)


def beep():
    try:
        import winsound
        for _ in range(5):
            winsound.Beep(1200, 300)
            time.sleep(0.15)
    except Exception:
        print("\a" * 5)


def notify(title, msg):
    if FANTANG and FANTANG_API:
        try:
            requests.post("https://sctapi.ftqq.com/%s.send" % FANTANG_API,
                          data={"title": title, "desp": msg}, timeout=10)
        except Exception:
            pass


def aes_encrypt(data, key):
    cipher = AES.new(key.encode("utf-8"), AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(data.encode("utf-8"), AES.block_size))).decode()


def login_pwd(password):
    """与前端 index.min.js 一致：base64( strEnc(pwd, "this","password","is") )"""
    return base64.b64encode(nju_des.str_enc(password, "this", "password", "is").encode()).decode()


def build_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Referer": "https://xk.nju.edu.cn/",
        "language": "zh_CN",
    })
    return s


# ------------------------- 登录（密码框 + 点选验证码） -------------------------

def ask_password():
    """返回登录密码：优先用 DEFAULT_PASSWORD，否则弹密码框（只存内存）。"""
    if DEFAULT_PASSWORD:
        return DEFAULT_PASSWORD
    try:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        p = simpledialog.askstring("南大选课助手", "请输入你的统一身份认证密码：", show="*")
        root.destroy()
        return p
    except Exception:
        return input("请输入统一身份认证密码: ")


def click_captcha(image_path):
    """cv2 窗口：按提示顺序点 4 个字，返回 "x-y,x-y,x-y,x-y"。"""
    import cv2
    clicks_required, coords = 4, []

    def click_event(event, x, y, flags, params):
        nonlocal clicks_required
        if event == cv2.EVENT_LBUTTONDOWN and clicks_required > 0:
            coords.append((x, y))
            clicks_required -= 1
            cv2.circle(img, (x, y), 5, (0, 0, 255), -1)
            cv2.imshow(window_name, img)
            print(f"已记录点击 {len(coords)}/4 -> ({x}, {y})")

    print("--- 点选验证码：按提示【顺序】点击图片中的 4 个字（R重置，Esc退出）---")
    img = cv2.imread(image_path, cv2.IMREAD_COLOR)
    original_img = img.copy()
    window_name = "点击验证码"
    cv2.namedWindow(window_name)
    cv2.setMouseCallback(window_name, click_event)
    deadline_cv = time.time() + 120   # 120秒未点完自动放弃，避免卡死错过开抢
    while True:
        cv2.imshow(window_name, img)
        key = cv2.waitKey(1) & 0xFF
        if time.time() > deadline_cv:
            cv2.destroyAllWindows()
            log("验证码点击超时（120秒），放弃本次登录")
            return None
        if key == 13 or clicks_required == 0:
            break
        elif key in (ord("r"), ord("R")):
            coords, clicks_required = [], 4
            img = original_img.copy()
            print("已重置，请重新点击。")
        elif key == 27:
            cv2.destroyAllWindows()
            sys.exit(0)
    cv2.destroyAllWindows()
    if not coords:
        print("❌ 没有检测到点击")
        return None
    result = ",".join("%d-%d" % (x, y) for x, y in coords)
    print("✅ 验证码坐标:", result)
    return result


def login(s):
    """完整登录流程：获取点选验证码 → 用户点击 → DES加密密码提交。"""
    global PASSWORD
    if not PASSWORD:
        PASSWORD = ask_password()
    if not PASSWORD:
        log("❌ 未输入密码，无法登录")
        return False
    log("正在获取验证码...")
    try:
        vode = s.post(URL_VCODE, timeout=15).json()["data"]
        pic_base64 = vode["vode"].split(",")[1]
        uuid = vode["uuid"]
    except Exception as e:
        log(f"获取验证码失败: {e}")
        return False
    captcha_file = "captcha.png"
    with open(captcha_file, "wb") as f:
        f.write(base64.b64decode(pic_base64))
    coords = click_captcha(captcha_file)
    if coords is None:
        return False
    try:
        r = s.post(URL_LOGIN, data={
            "loginName": STUDENT_NUMBER,
            "loginPwd": login_pwd(PASSWORD),
            "verifyCode": coords,
            "vtoken": "null",
            "uuid": uuid,
        }, timeout=15).json()
    except Exception as e:
        log(f"登录请求异常: {e}")
        return False
    if r.get("code") != "1":
        log(f"❌ 登录失败: {r.get('msg')}（密码错或验证码点错，将重试）")
        return False
    log("✅ 登录成功")
    s.headers.update({"token": r["data"]["token"]})
    # 与前端一致：登录后调用一次 xkxf.do 初始化
    try:
        init = s.post(URL_XKXF, data={"xh": STUDENT_NUMBER, "xklcdm": BATCH_CODE}, timeout=15).json()
        log("初始化:", init.get("msg"))
    except Exception as e:
        log("初始化调用异常:", e)
    return True


# ------------------------- 课程名单解析与解析教学班 -------------------------

def parse_targets(raw):
    """把课程包文本解析成 [(搜索关键词, 课程号备选, 显示名)] 列表。"""
    entries = []
    pending_numbers = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("（注") or line.startswith("思政"):
            continue
        numbers = re.findall(r"\d{8}[A-Z]?", line)
        text = re.sub(r"[\d;A-Za-z" "“”‘’《》""\s:：,，.．\-—()（）]+", "", line)
        if numbers and text:
            entries.append((text, numbers[-1], text))
        elif numbers:
            pending_numbers.append(numbers[-1])
        elif text:
            if pending_numbers:
                num = pending_numbers.pop(0)
                entries.append((text, num, f"{num} {text}"))
            else:
                entries.append((text, None, text))
    for num in pending_numbers:
        entries.append((num, None, num))
    seen, out = set(), []
    for key, num, label in entries:
        if key in seen:
            continue
        seen.add(key)
        out.append((key, num, label))
    return out


CATEGORY_KEYS = {"油画", "素描", "摄影", "剪辑"}   # 类别词：取全部匹配课程（每类上限6门）


def _is_full(it):
    """根据 isFull / 已选人数>=容量 判断课程是否已满。"""
    if str(it.get("isFull")) in ("1", "true", "True"):
        return True
    cap, sel = it.get("classCapacity"), it.get("numberOfSelected")
    if cap is not None and sel is not None:
        try:
            if int(sel) >= int(cap):
                return True
        except (TypeError, ValueError):
            pass
    return False


def search_course(s, query):
    """按关键词搜索课程，返回教学班条目或 None。"""
    r = s.post(URL_COURSE, data={
        "querySetting": json.dumps({
            "data": {"studentCode": STUDENT_NUMBER, "electiveBatchCode": BATCH_CODE,
                     "teachingClassType": "", "queryContent": query},
            "pageSize": "20", "pageNumber": "0"}),
    }, timeout=20).json()
    code = str(r.get("code"))
    if code in ("2", "302") or "登录" in (r.get("msg") or ""):
        log("搜索时会话失效，重新登录...")
        login(s)
        return None
    items = r.get("dataList") or []
    return items[0] if items else None


def resolve_targets(s, targets):
    """搜索解析教学班：普通条目取首个匹配；类别词（油画/素描/摄影/剪辑）取全部匹配。
    满员课程直接剔除；按教学班ID去重。"""
    courses, seen_ids = [], set()
    for i, (key, num, label) in enumerate(targets):
        is_cat = key in CATEGORY_KEYS
        found = []
        queries = [key] if not num else [key, num]
        for query in queries:
            for _ in range(2):
                try:
                    r = s.post(URL_COURSE, data={
                        "querySetting": json.dumps({
                            "data": {"studentCode": STUDENT_NUMBER, "electiveBatchCode": BATCH_CODE,
                                     "teachingClassType": "", "queryContent": query},
                            "pageSize": "20", "pageNumber": "0"}),
                    }, timeout=20)
                    j = r.json()
                    if str(j.get("code")) in ("2", "302") or "登录" in (j.get("msg") or ""):
                        log("解析时会话失效，重新登录...")
                        login(s)
                        continue
                    items = j.get("dataList") or []
                except Exception as e:
                    log(f"搜索 {label} 异常: {e}")
                    time.sleep(1)
                    continue
                if is_cat:
                    found = [it for it in items if key in (it.get("courseName") or "")][:6]
                elif items:
                    found = [items[0]]
                if found:
                    break
            if found:
                break
        if not found:
            log(f"[{i + 1}/{len(targets)}] ⚠️ 未找到「{label}」，跳过")
            time.sleep(0.15)
            continue
        added = 0
        for c in found:
            tid = c.get("teachingClassID")
            if _is_full(c):
                log(f"[{i + 1}/{len(targets)}] {c.get('courseName')} 已满，剔除")
                continue
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            courses.append({
                "teachingClassID": tid,
                "teachingClassType": TARGET_TYPE_DEFAULT,
                "courseName": c.get("courseName") or label,
                "label": label,
            })
            log(f"[{i + 1}/{len(targets)}] {c.get('courseName')} -> ID={tid}")
            added += 1
        time.sleep(0.15)
    return courses


# ------------------------- 抢课请求 -------------------------

def build_add_param(course):
    course_type = course.get("teachingClassType") or ""
    course_kind = course.get("courseKind")
    if course_kind is None:
        course_kind = COURSE_KIND_OVERRIDE
    payload = json.dumps({
        "data": {
            "operationType": "1",
            "studentCode": STUDENT_NUMBER,
            "electiveBatchCode": BATCH_CODE,
            "teachingClassId": course["teachingClassID"],
            "courseKind": course_kind,
            "teachingClassType": course_type,
        },
    })
    plaintext = "%s?timestrap=%d" % (payload, int(time.time() * 1000))
    post_data = {
        "addParam": aes_encrypt(plaintext, AES_KEY),
        "studentCode": STUDENT_NUMBER,
    }
    return plaintext, post_data


# ------------------------- 时间同步与等待 -------------------------

def sync_server_time(s):
    """通过 Date 头估算 服务器时间-本地时间 偏移（秒）。"""
    offsets = []
    for _ in range(5):
        t0 = time.time()
        try:
            r = s.get("https://xk.nju.edu.cn/", timeout=10)
            t1 = time.time()
            d = r.headers.get("Date")
            if d:
                import calendar
                server_unix = calendar.timegm(time.strptime(d, "%a, %d %b %Y %H:%M:%S GMT"))
                offsets.append((server_unix - t0) + (t1 - t0) / 2)
        except Exception:
            pass
        time.sleep(0.3)
    if not offsets:
        log("⚠️ 无法获取服务器时间，按本地时间计算")
        return 0.0
    offsets.sort()
    return offsets[len(offsets) // 2]


def keepalive(s):
    try:
        s.post(URL_FAV, data={
            "querySetting": json.dumps({
                "data": {"studentCode": STUDENT_NUMBER, "electiveBatchCode": BATCH_CODE,
                         "teachingClassType": "SC", "queryContent": ""},
                "pageSize": "20", "pageNumber": "0", "order": "isChoose -",
            }),
        }, timeout=15)
    except Exception:
        pass


def wait_until_open(s, deadline_epoch, offset):
    last_keepalive = 0.0
    relogged = False
    while True:
        remain = deadline_epoch - (time.time() + offset)
        if remain <= LEAD_TIME:
            print()
            return
        now = time.time()
        if remain > 600:
            interval, sleep_s = 60, 10
        elif remain > 3:
            interval, sleep_s = 15, 0.2
        else:
            interval, sleep_s = 10 ** 9, 0.02
        if not relogged and remain <= RELOGIN_AT_T_MINUS:
            log("🔄 距开放不足 3 分钟，强制重新登录保证会话新鲜...")
            if login(s):
                relogged = True
            else:
                log("⚠️ 重新登录失败，继续使用当前会话（可能仍有效），不再重试")
                relogged = True
            continue
        if now - last_keepalive > interval:
            last_keepalive = now
            keepalive(s)
        if remain > 600:
            print(f"\r距选课开放还有 {int(remain)} 秒（约 {int(remain // 60)} 分钟），保活中…", end="", flush=True)
        else:
            print(f"\r距选课开放还有 {remain:.1f} 秒，请保持脚本运行", end="", flush=True)
        time.sleep(sleep_s)


# ------------------------- 抢课主循环 -------------------------

def rush(s, courses, deadline_epoch, offset):
    log(f"🚀 共 {len(courses)} 门目标课程，等待服务器时间越过开放时刻...")
    while time.time() + offset < deadline_epoch:
        time.sleep(0.005)
    log("🔥 时间到，开抢！")
    start = time.time()
    pending = list(courses)
    done, failed = [], []
    round_no = 0
    while pending and time.time() - start < MAX_RUSH_SECONDS:
        round_no += 1
        for c in list(pending):
            if time.time() - start > MAX_RUSH_SECONDS:
                break
            time.sleep(random.uniform(RUSH_MIN_SLEEP, RUSH_MAX_SLEEP))   # 每个请求前都间隔，防止"请求过快"踢会话
            try:
                _, post = build_add_param(c)
                r = s.post(URL_VOLUNTEER, data=post, timeout=10)
                j = r.json()
            except Exception as e:
                log(f"[{c['label']}] 网络/解析异常: {e}")
                time.sleep(0.05)
                continue
            code = str(j.get("code"))
            msg = j.get("msg") or ""
            if code == "1":
                log(f"🎉🎉 抢到: {c['courseName']} ({c['teachingClassID']})")
                notify("🎉 抢课成功", c["courseName"])
                beep()
                done.append(c)
                pending.remove(c)
                continue
            if code in ("2", "302") or any(k in msg for k in ("登录", "会话", "非法请求", "请求过快")):
                log(f"[{c['label']}] 会话失效（{msg[:30]}），自动重新登录...")
                if login(s):
                    time.sleep(5)   # 重登后冷却，避免再次触发"请求过快"
                    continue
                time.sleep(2)
                continue
            if any(k in msg for k in ("已满", "人满", "满员", "名额已", "超过课容量", "课容量")):
                if "看见马克思" in (c.get("courseName") or ""):
                    log(f"[{c['label']}] 已满，按约定持续蹲守（系统有延迟放出名额机制）")
                    continue
                log(f"[{c['label']}] 课程已满（{msg[:30]}），不再尝试")
                failed.append(c)
                pending.remove(c)
                continue
            if "非可选课程" in msg or "无法选课" in msg:
                alts = ["GG01", "GG02", "ZY"]
                tried = c.get("alt_tried", 0)
                if tried < len(alts):
                    c["teachingClassType"] = alts[tried]
                    c["alt_tried"] = tried + 1
                    log(f"[{c['label']}] 非可选，换类型 {alts[tried]} 重试")
                    continue
                log(f"[{c['label']}] 各类型均非可选，剔除该课")
                failed.append(c)
                pending.remove(c)
                continue
            if any(k in msg for k in ("不在开放选课轮次", "轮次", "获取菜单失败", "菜单失败")):
                log(f"[{c['label']}] {msg[:40]}，剔除该课")
                failed.append(c)
                pending.remove(c)
                continue
            if any(k in msg for k in ("人数", "太多", "未开始", "未开放", "时间")):
                continue   # 尚未开放或服务器繁忙，下轮再试
            if any(k in msg for k in ("冲突", "已选", "重复", "修读", "通过", "读过")):
                log(f"[{c['label']}] 被服务器拒绝（{msg[:60]}），跳过该课")
                failed.append(c)
                pending.remove(c)
                continue
            if any(k in msg for k in ("类型", "种类", "菜单", "参数", "非法")):
                log(f"[{c['label']}] 参数/类型错误（{msg[:60]}），跳过该课")
                failed.append(c)
                pending.remove(c)
                continue
            log(f"[{c['label']}] 未预期响应: {msg[:80]} | {r.text[:120]}")
        log(f"第{round_no}轮: 已抢到{len(done)}门 / 仍在抢{len(pending)}门 / 已放弃{len(failed)}门")
        time.sleep(random.uniform(RUSH_MIN_SLEEP, RUSH_MAX_SLEEP))
    log(f"🏁 抢课结束：成功 {len(done)} 门，放弃 {len(failed)} 门")
    for c in done:
        log("   ✅", c["courseName"])
    if done:
        beep()


# ------------------------- 主流程 -------------------------

def main():
    log("===== 南京大学选课平台 自动抢课 v2 =====")
    targets = parse_targets(TARGET_COURSES_RAW)
    log(f"课程名单解析出 {len(targets)} 门（去重后）")

    if TEST_MODE == "dry":
        log("🧪 演练模式：不登录不发请求，仅自检。")
        log("   DES 自检:", login_pwd("test1234")[:20] + "...")
        sample = {"teachingClassID": "2026202710037250001", "teachingClassType": TARGET_TYPE_DEFAULT,
                  "courseName": "看见马克思"}
        plaintext, post = build_add_param(sample)
        log("   AES 明文:", plaintext)
        log("   AES 加密:", post["addParam"][:40] + "...")
        log("   名单前10门:", [t[1] for t in targets[:10]])
        sys.exit(0)

    s = build_session()
    log("正在登录（请留意弹窗：先输密码，再点验证码）...")
    while not login(s):
        time.sleep(1)
    s.headers.update({
        "Referer": BASE + "/*default/grablessons.do?token=" + s.headers.get("token", ""),
    })

    log("解析目标课程教学班...")
    courses = resolve_targets(s, targets)
    if not courses:
        log("❌ 一门课都没解析出来，退出")
        sys.exit(1)
    # 优先抢「看见马克思」（开放瞬间的第一发请求给它）
    courses.sort(key=lambda c: 0 if "看见马克思" in (c.get("courseName") or "") else 1)
    log("抢课顺序（第一优先）:", [c["courseName"] for c in courses[:3]])

    offset = sync_server_time(s)
    log(f"🕐 服务器时间偏移 ≈ {offset:+.2f} 秒")

    try:
        deadline_epoch = datetime.datetime.strptime(OPEN_TIME, "%Y-%m-%d %H:%M:%S") \
            .replace(tzinfo=BEIJING).timestamp()
    except ValueError:
        log("❌ OPEN_TIME 格式错误")
        sys.exit(1)

    if TEST_MODE == "now":
        log("🧪 实弹测试模式：3 秒后立即开抢！")
        deadline_epoch = time.time() + offset + 3
    else:
        if deadline_epoch - time.time() < 0:
            log("⚠️ OPEN_TIME 已是过去时间，将立即开抢")
        wait_until_open(s, deadline_epoch, offset)

    rush(s, courses, deadline_epoch, offset)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("已手动停止（Ctrl+C）")

