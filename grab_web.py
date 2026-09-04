# -*- coding: utf-8 -*-
"""
南大选课蹲课助手 · 网页版
========================
运行（python grab_web.py，或打包后的 exe 双击）后自动打开浏览器页面：
  1. 页面上填学号、密码、课程号（可带校区/节奏等高级参数）
  2. 点「开始蹲课」→ 登录（验证码直接在网页里点选 4 个字，不再弹 cv2 窗口）
  3. 实时日志、会话失效自动重登、抢到响铃
  4. 可打包成 exe 分享给没有 Python 环境的同学

依赖：flask、requests、pycryptodome（同目录 grab_rush.py / nju_des.py）
"""

import base64
import json
import os
import random
import sys
import threading
import time
import webbrowser


# Windows 控制台默认 GBK，打印 emoji 会崩；exe 的 --noconsole 模式 stdout 为 None
class _NullWriter:
    def write(self, *a, **k):
        pass

    def flush(self, *a, **k):
        pass


if sys.stdout is None:
    sys.stdout = _NullWriter()
if sys.stderr is None:
    sys.stderr = _NullWriter()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from flask import Flask, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grab_rush as g

DEFAULT_MENUS = ["GG02", "GG01", "KZY", "TX", "TX01", "TX02", "TX03", "TX04",
                 "ZY", "TY", "YD", "QB", "SC"]

CAMPUS_FIELDS = ("campusName", "campus", "schoolCampus", "teachingPlace")
OTHER_CAMPUS_MARKERS = ("鼓楼", "苏州", "浦口")
MAX_LOG_LINES = 2000

app = Flask(__name__)


def base_dir():
    """打包成 exe 后，数据文件写到 exe 所在目录，而不是 PyInstaller 解压临时目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# ============================== 全局状态 ==============================

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"          # idle | running | login_wait | done | error
        self.logs = []                # [{"t": "14:02:33", "text": "..."}]
        self.grabbed = []             # 已抢到的课程名
        self.stats = {}               # 最近一轮统计
        self.captcha = None           # {"uuid": ..., "image_b64": ..., "coords": ...}
        self.captcha_evt = threading.Event()
        self.stop_evt = threading.Event()
        self.thread = None


st = State()


def add_log(*args):
    ts = time.strftime("%H:%M:%S")
    text = " ".join(str(a) for a in args)
    with st.lock:
        st.logs.append({"t": ts, "text": text})
        if len(st.logs) > MAX_LOG_LINES:
            del st.logs[: len(st.logs) // 2]
    print(ts, text, flush=True)


# ============================== 登录（网页点选验证码） ==============================

def web_login(s):
    """完整登录流程：拉验证码 → 等网页回传 4 个字的坐标 → DES 加密密码提交。"""
    while not st.stop_evt.is_set():
        st.status = "login_wait"
        add_log("正在获取验证码（请在网页弹窗里点选 4 个字）...")
        try:
            vode = s.post(g.URL_VCODE, timeout=15).json()["data"]
            pic_b64 = vode["vode"].split(",", 1)[1]
            uuid = vode["uuid"]
        except Exception as e:
            add_log(f"获取验证码失败: {e}，2 秒后重试")
            time.sleep(2)
            continue

        with st.lock:
            st.captcha = {"uuid": uuid, "image_b64": pic_b64, "coords": None}
        st.captcha_evt.clear()
        deadline = time.time() + 120
        got = False
        while time.time() < deadline:
            if st.captcha_evt.wait(0.2):
                got = True
                break
            if st.stop_evt.is_set():
                break
        with st.lock:
            coords = (st.captcha or {}).get("coords")
            st.captcha = None
        if st.stop_evt.is_set():
            return False
        if not got or not coords:
            add_log("验证码等待超时（120 秒），重新获取")
            continue
        st.status = "running"

        try:
            r = s.post(g.URL_LOGIN, data={
                "loginName": g.STUDENT_NUMBER,
                "loginPwd": g.login_pwd(g.PASSWORD),
                "verifyCode": coords,
                "vtoken": "null",
                "uuid": uuid,
            }, timeout=15).json()
        except Exception as e:
            add_log(f"登录请求异常: {e}，重试")
            continue
        if r.get("code") != "1":
            msg = r.get("msg") or ""
            if any(k in msg for k in ("密码", "账号")):
                add_log(f"❌ 登录失败: {msg}（验证码已通过 → 是密码不对，请检查密码）")
            else:
                add_log(f"❌ 登录失败: {msg}（验证码点错，将重试）")
            time.sleep(1)
            continue
        add_log("✅ 登录成功")
        s.headers.update({"token": r["data"]["token"]})
        try:
            init = s.post(g.URL_XKXF, data={"xh": g.STUDENT_NUMBER, "xklcdm": g.BATCH_CODE},
                          timeout=15).json()
            add_log("初始化:", init.get("msg"))
        except Exception as e:
            add_log("初始化调用异常:", e)
        return True
    return False


# ============================== 选课查询与过滤 ==============================

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


def is_in_campus(c, campus_keyword):
    """严格校区判断：必须明确出现关键词，且不出现其他校区标志；无信息则排除。"""
    if not campus_keyword:
        return True
    vals = [str(c[f]) for f in CAMPUS_FIELDS if c.get(f)]
    if not vals:
        return False
    joined = "".join(vals)
    if campus_keyword not in joined:
        return False
    if any(m in joined for m in OTHER_CAMPUS_MARKERS):
        return False
    return True


def sleep_interruptible(seconds):
    """可被「停止」打断的 sleep。"""
    st.stop_evt.wait(seconds)


# ============================== 蹲课主循环 ==============================

def load_grabbed():
    try:
        with open(os.path.join(base_dir(), "grabbed_ids.txt"), "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    except OSError:
        return set()


def save_grabbed(ids):
    try:
        with open(os.path.join(base_dir(), "grabbed_ids.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(ids)))
    except OSError:
        pass


def worker(cfg):
    try:
        st.status = "running"
        g.STUDENT_NUMBER = cfg["studentNumber"]
        g.PASSWORD = cfg["password"]
        if cfg.get("batchCode"):
            g.BATCH_CODE = cfg["batchCode"]
        if cfg.get("aesKey"):
            g.AES_KEY = cfg["aesKey"]
        targets = [t for t in cfg["courseNumbers"] if t]
        campus = (cfg.get("campusKeyword") or "").strip()
        menus = cfg.get("menus") or DEFAULT_MENUS
        cycle = max(2, int(cfg.get("cycleSeconds") or 5))
        max_grab = max(1, int(cfg.get("maxGrab") or 1))
        cooldown_s = max(0, int(cfg.get("failCooldown") or 120))

        s = g.build_session()
        add_log(f"目标课程编号: {targets} | 校区: {campus or '不限'} | "
                f"{cycle} 秒一轮，抢到 {max_grab} 门收手")
        add_log("正在登录（请在网页弹窗点选验证码）...")
        if not web_login(s):
            add_log("已停止或登录未完成，结束")
            return
        s.headers.update({
            "Referer": g.BASE + "/*default/grablessons.do?token=" + s.headers.get("token", ""),
        })

        # ---------- 扫描：先看目标课的教学班分布 ----------
        add_log("=" * 18 + " 目标课程扫描 " + "=" * 18)
        tset = set(targets)
        for menu in menus:
            if st.stop_evt.is_set():
                return
            try:
                lst, err = fetch_menu_full(s, menu)
            except Exception as e:
                add_log(f"扫描菜单 {menu} 异常: {e}")
                continue
            if err is not None:
                add_log("会话失效，重新登录...")
                if not web_login(s):
                    return
                try:
                    lst, err = fetch_menu_full(s, menu)
                except Exception:
                    continue
            for c in lst:
                if (c.get("courseNumber") or "") in tset:
                    add_log(f"  菜单 {menu} | {c.get('courseName')} | 校区={c.get('campusName')} | "
                            f"已选={c.get('numberOfSelected')}/{c.get('classCapacity')} | "
                            f"isFull={c.get('isFull')} | 我已选={c.get('isChoose')} | ID={c.get('teachingClassID')}")
            sleep_interruptible(0.4)

        # ---------- 蹲守 ----------
        add_log("=" * 18 + f" 蹲守开始（{cycle} 秒一轮，抢到 {max_grab} 门收手）" + "=" * 18)
        grabbed_ids = load_grabbed()
        cooldown = {}
        grabbed = []
        round_no = 0
        while len(grabbed) < max_grab and not st.stop_evt.is_set():
            round_no += 1
            t0 = time.time()
            items, seen_ids, relogged = [], set(), False
            for menu in menus:
                if st.stop_evt.is_set():
                    return
                try:
                    lst, err = fetch_menu(s, menu)
                except Exception as e:
                    add_log(f"[第{round_no}轮] 菜单 {menu} 查询异常: {e}")
                    continue
                if err is not None:
                    add_log("会话失效，自动重新登录（请在网页点验证码）...")
                    relogged = True
                    if not web_login(s):
                        return
                    break
                for c in lst:
                    cid = c.get("teachingClassID")
                    if cid and cid not in seen_ids:
                        seen_ids.add(cid)
                        items.append((menu, c))
                sleep_interruptible(random.uniform(0.3, 0.5))
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
                if not is_in_campus(c, campus):
                    stat["campus"] += 1
                    continue
                cands.append((menu, c))
            with st.lock:
                st.stats = {"round": round_no, **stat, "cands": len(cands),
                            "grabbed_count": len(grabbed)}

            if not cands:
                add_log(f"[第{round_no}轮] 无候选（共{stat['total']}门：非目标{stat['not_target']} / "
                        f"非{campus or '目标校区'}{stat['campus']} / 已满{stat['full']} / "
                        f"已选{stat['chosen']} / 冷却{stat['cool']} / 已抢{stat['grabbed']}），刷新重试")
                sleep_interruptible(max(0.5, cycle - (time.time() - t0)))
                continue

            menu, c = random.choice(cands)
            c["teachingClassType"] = menu
            cid = c.get("teachingClassID")
            add_log(f"[第{round_no}轮] 候选 {len(cands)} 门，随机选「{c.get('courseName')}」"
                    f"（菜单{menu}，{c.get('campusName')}）提交（每轮一次）")
            try:
                _plain, post = g.build_add_param(c)
                r = s.post(g.URL_VOLUNTEER, data=post, timeout=10)
                j = r.json()
            except Exception as e:
                add_log(f"[第{round_no}轮] 提交网络异常: {e}")
                sleep_interruptible(max(0.5, cycle - (time.time() - t0)))
                continue

            code = str(j.get("code"))
            msg = j.get("msg") or ""
            if code == "1":
                add_log(f"🎉🎉 抢到: {c.get('courseName')} ({cid})")
                g.notify("🎉 抢课成功", c.get("courseName"))
                g.beep()
                grabbed.append(c)
                st.grabbed.append(c.get("courseName"))
                if cid:
                    grabbed_ids.add(cid)
                    save_grabbed(grabbed_ids)
                continue
            if code in ("2", "302") or any(k in msg for k in ("登录", "会话")):
                add_log("会话失效，自动重新登录...")
                if not web_login(s):
                    return
                continue
            if any(k in msg for k in ("请求过快", "频率")):
                add_log(f"[第{round_no}轮] 请求过快，冷却 30 秒")
                if cid:
                    cooldown[cid] = time.time() + cooldown_s
                sleep_interruptible(30)
                continue
            if any(k in msg for k in ("已满", "人满", "满员", "名额", "课容量")):
                add_log(f"[第{round_no}轮] {c.get('courseName')} 已满，刷新重试")
            elif any(k in msg for k in ("冲突", "已选", "重复", "修读", "通过", "读过", "门数")):
                add_log(f"[第{round_no}轮] {c.get('courseName')} 被服务器拒绝（{msg[:50]}），冷却该课")
            else:
                add_log(f"[第{round_no}轮] 未预期响应: {msg[:80]} | {r.text[:120]}")
            if cid:
                cooldown[cid] = time.time() + cooldown_s
            sleep_interruptible(max(0.5, cycle - (time.time() - t0)))

        if st.stop_evt.is_set():
            add_log("已手动停止")
        add_log(f"🏁 结束：成功 {len(grabbed)} 门")
        for c in grabbed:
            add_log("   ✅ " + c.get("courseName"))
        if grabbed:
            g.beep()
        st.status = "done"
    except Exception as e:
        import traceback
        add_log("❌ 程序异常: " + repr(e))
        add_log(traceback.format_exc())
        st.status = "error"
    finally:
        if st.status in ("running", "login_wait"):
            st.status = "idle"


# ============================== HTTP 接口 ==============================

@app.route("/")
def index():
    return HTML


@app.route("/api/start", methods=["POST"])
def api_start():
    if st.thread and st.thread.is_alive():
        return jsonify({"ok": False, "msg": "已经在蹲课中"}), 400
    try:
        d = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "msg": "请求格式错误"}), 400
    stu = str(d.get("studentNumber") or "").strip()
    pwd = str(d.get("password") or "")
    courses = [str(x).strip() for x in (d.get("courseNumbers") or [])]
    if not stu:
        return jsonify({"ok": False, "msg": "请填写学号"}), 400
    if not pwd:
        return jsonify({"ok": False, "msg": "请填写密码"}), 400
    if not courses:
        return jsonify({"ok": False, "msg": "请至少填写一个课程号"}), 400
    menus = [m.strip() for m in re_split(str(d.get("menus") or "")) if m.strip()]
    cfg = {
        "studentNumber": stu,
        "password": pwd,
        "courseNumbers": courses,
        "campusKeyword": str(d.get("campusKeyword") or "").strip(),
        "menus": menus,
        "cycleSeconds": d.get("cycleSeconds"),
        "maxGrab": d.get("maxGrab"),
        "failCooldown": d.get("failCooldown"),
        "batchCode": str(d.get("batchCode") or "").strip(),
        "aesKey": str(d.get("aesKey") or "").strip(),
    }
    st.stop_evt.clear()
    st.grabbed = []
    st.stats = {}
    st.thread = threading.Thread(target=worker, args=(cfg,), daemon=True)
    st.thread.start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    st.stop_evt.set()
    st.captcha_evt.set()   # 若正卡在等待验证码，立即唤醒
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    return jsonify({
        "status": st.status,
        "grabbed": list(st.grabbed),
        "stats": st.stats,
        "running": bool(st.thread and st.thread.is_alive()),
    })


@app.route("/api/logs")
def api_logs():
    since = int(request.args.get("since") or 0)
    with st.lock:
        logs = st.logs[since:]
        next_idx = len(st.logs)
    return jsonify({"logs": logs, "next": next_idx})


@app.route("/api/captcha")
def api_captcha():
    with st.lock:
        cap = st.captcha
    if not cap:
        return jsonify({"pending": False})
    return jsonify({"pending": True, "uuid": cap["uuid"], "image": cap["image_b64"]})


@app.route("/api/captcha", methods=["POST"])
def api_captcha_post():
    try:
        d = request.get_json(force=True) or {}
    except Exception:
        d = {}
    coords = str(d.get("coords") or "").strip()
    with st.lock:
        cap = st.captcha
    if not cap:
        return jsonify({"ok": False, "msg": "当前没有待点选的验证码"}), 400
    if not re_split_coords(coords):
        return jsonify({"ok": False, "msg": "坐标格式错误"}), 400
    add_log("验证码坐标已提交: " + coords)
    cap["coords"] = coords
    st.captcha_evt.set()
    return jsonify({"ok": True})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    st.stop_evt.set()
    st.captcha_evt.set()

    def _exit():
        time.sleep(0.3)
        os._exit(0)

    threading.Thread(target=_exit, daemon=True).start()
    return jsonify({"ok": True})


def re_split(text):
    import re
    return [x for x in re.split(r"[\s,，;；]+", text) if x]


def re_split_coords(coords):
    import re
    return bool(re.fullmatch(r"\d+-\d+(,\d+-\d+){3}", coords))


# ============================== 页面 ==============================

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>南大选课蹲课助手</title>
<link rel="icon" href="data:,">
<style>
:root{--bg:#f2f4f8;--card:#fff;--border:#e3e8ef;--text:#1a2333;--muted:#6b7686;
      --primary:#2563eb;--primary-h:#1d4ed8;--danger:#dc2626;--green:#16a34a;--orange:#d97706;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
     font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif}
header{background:var(--card);border-bottom:1px solid var(--border);
       padding:14px 24px;font-size:18px;font-weight:700}
header .sub{font-size:12px;color:var(--muted);font-weight:400;margin-left:10px}
main{display:grid;grid-template-columns:400px 1fr;gap:16px;padding:16px;max-width:1280px;margin:0 auto}
@media(max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px}
.card h2{font-size:15px;margin:0 0 12px}
label{display:block;margin-bottom:10px;font-size:13px;color:var(--muted)}
label b{color:var(--text);font-weight:600}
input[type=text],input[type=password],input[type=number],textarea{
  width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;
  font:inherit;font-size:14px;color:var(--text);margin-top:4px;background:#fafbfd}
input:focus,textarea:focus{outline:2px solid #bfdbfe;border-color:var(--primary)}
textarea{resize:vertical}
.save-pwd{display:flex;align-items:center;gap:6px;margin-top:-4px;font-size:12px}
.save-pwd input{width:auto}
details.adv{margin:10px 0;border:1px dashed var(--border);border-radius:8px;padding:8px 12px}
details.adv summary{cursor:pointer;font-size:13px;color:var(--muted);user-select:none}
.adv-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 14px;margin-top:10px}
.adv-grid label{font-size:12px}
.btn-row{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}
button{border:none;border-radius:8px;padding:10px 22px;font:inherit;font-size:14px;font-weight:600;cursor:pointer}
button:disabled{opacity:.45;cursor:not-allowed}
button.primary{background:var(--primary);color:#fff}
button.primary:hover:not(:disabled){background:var(--primary-h)}
button.danger{background:var(--danger);color:#fff}
button.ghost{background:#eef1f6;color:var(--text)}
.warn{margin:14px 0 0;font-size:12px;color:var(--orange)}
.right{display:flex;flex-direction:column;gap:16px;min-width:0}
.status-card .row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.badge{display:inline-block;padding:3px 14px;border-radius:999px;font-size:13px;font-weight:700;color:#fff}
.badge.idle{background:#94a3b8}
.badge.running{background:var(--primary)}
.badge.login_wait{background:var(--orange)}
.badge.done{background:var(--green)}
.badge.error{background:var(--danger)}
.stats{color:var(--muted);font-size:13px;margin-top:8px;min-height:18px}
#grabbed-list{margin:8px 0 0;padding-left:20px}
#grabbed-list li{color:var(--green);font-weight:600}
.log-card{flex:1;display:flex;flex-direction:column;min-height:300px}
#log{flex:1;background:#0f172a;color:#a7f3d0;border-radius:8px;padding:10px 12px;
     font:12px/1.7 Consolas,"Courier New",monospace;overflow-y:auto;max-height:70vh;min-height:200px;
     white-space:pre-wrap;word-break:break-all}
#log .ts{color:#64748b;margin-right:8px}
#log .success{color:#fde047;font-weight:700}
#log .err{color:#fca5a5}
#captcha-overlay{position:fixed;inset:0;background:rgba(15,23,42,.65);display:flex;
  align-items:center;justify-content:center;z-index:99}
#captcha-overlay.hidden{display:none}
.captcha-card{background:#fff;border-radius:12px;padding:20px;max-width:92vw;box-shadow:0 10px 40px rgba(0,0,0,.3)}
.captcha-card h3{margin:0 0 6px}
.captcha-card .hint{color:var(--muted);font-size:13px;margin:0 0 12px}
#captcha-box{position:relative;display:inline-block;line-height:0;background:#fafbfd;
  border:1px solid var(--border);border-radius:8px;overflow:hidden}
#captcha-box img{display:block;max-width:88vw;max-height:60vh}
.dot{position:absolute;width:18px;height:18px;border-radius:50%;background:rgba(220,38,38,.85);
  border:2px solid #fff;transform:translate(-50%,-50%);pointer-events:none;box-shadow:0 1px 4px rgba(0,0,0,.4)}
.dot::after{content:attr(data-i);position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;color:#fff;font-size:10px;font-weight:700}
</style>
</head>
<body>
<header>🎓 南大选课蹲课助手<span class="sub">本地运行 · 数据仅发给南大选课系统 · 风险自担</span></header>
<main>
  <section class="card">
    <h2>蹲课配置</h2>
    <label><b>学号</b><input id="f-stu" placeholder="如 231000000"></label>
    <label><b>统一身份认证密码</b><input id="f-pwd" type="password" placeholder="仅存内存，用于登录">
      <span class="save-pwd"><input type="checkbox" id="f-savepwd"> 记住密码（明文保存在本机浏览器）</span></label>
    <label><b>课程号</b>（8 位课程号，一行一个，可带备注）
      <textarea id="f-courses" rows="4" placeholder="00371690 口语表达入门&#10;78005020 大学生生涯胜任力"></textarea></label>
    <label><b>校区关键词</b><input id="f-campus" placeholder="仙林（留空 = 不限校区）"></label>
    <details class="adv">
      <summary>高级设置</summary>
      <div class="adv-grid">
        <label>刷新间隔（秒）<input id="f-cycle" type="number" value="5" min="2"></label>
        <label>抢到几门停<input id="f-maxgrab" type="number" value="1" min="1"></label>
        <label>被拒冷却（秒）<input id="f-cooldown" type="number" value="120" min="0"></label>
        <label>批次代码 electiveBatchCode<input id="f-batch" placeholder="默认用内置值"></label>
        <label>AES 密钥<input id="f-aes" placeholder="默认用内置值"></label>
        <label style="grid-column:1/-1">菜单列表（逗号分隔，默认全部）<input id="f-menus"
          placeholder="GG02,GG01,KZY,TX,TX01,TX02,TX03,TX04,ZY,TY,YD,QB,SC"></label>
      </div>
    </details>
    <div class="btn-row">
      <button id="btn-start" class="primary">▶ 开始蹲课</button>
      <button id="btn-stop" class="danger" disabled>⏹ 停止</button>
      <button id="btn-exit" class="ghost">退出程序</button>
    </div>
    <p class="warn">⚠️ 使用脚本选课可能违反学校规定；默认节奏温和（每 5 秒一轮，每轮最多提交一次）。</p>
  </section>

  <section class="right">
    <div class="card status-card">
      <div class="row"><span id="badge" class="badge idle">空闲</span></div>
      <div class="stats" id="stats-line"></div>
      <ul id="grabbed-list"></ul>
    </div>
    <div class="card log-card">
      <h2>运行日志</h2>
      <div id="log"></div>
    </div>
  </section>
</main>

<div id="captcha-overlay" class="hidden">
  <div class="captcha-card">
    <h3>点选验证码</h3>
    <p class="hint">请按提示【顺序】点击图片中的 4 个字（点错按 R 重置；120 秒未点完将重新获取）</p>
    <div id="captcha-box"><img id="captcha-img" alt="验证码" draggable="false"></div>
    <div class="btn-row">
      <button id="captcha-reset" class="ghost">重置（R）</button>
      <button id="captcha-cancel" class="danger">取消并停止</button>
    </div>
  </div>
</div>

<script>
"use strict";
const $ = id => document.getElementById(id);
const LS_KEY = "nju_grab_config";

function loadCfg(){
  try{
    const c = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
    if(c.stu) $("f-stu").value = c.stu;
    if(c.courses) $("f-courses").value = c.courses;
    if(c.campus) $("f-campus").value = c.campus;
    if(c.cycle) $("f-cycle").value = c.cycle;
    if(c.maxGrab) $("f-maxgrab").value = c.maxGrab;
    if(c.cooldown) $("f-cooldown").value = c.cooldown;
    if(c.menus) $("f-menus").value = c.menus;
    if(c.batch) $("f-batch").value = c.batch;
    if(c.aes) $("f-aes").value = c.aes;
    $("f-savepwd").checked = !!c.savePwd;
    if(c.savePwd && c.pwd){ $("f-pwd").value = c.pwd; }
  }catch(e){}
}
function saveCfg(){
  const savePwd = $("f-savepwd").checked;
  const c = {
    stu: $("f-stu").value.trim(),
    pwd: savePwd ? $("f-pwd").value : "",
    savePwd,
    courses: $("f-courses").value,
    campus: $("f-campus").value,
    cycle: $("f-cycle").value,
    maxGrab: $("f-maxgrab").value,
    cooldown: $("f-cooldown").value,
    menus: $("f-menus").value,
    batch: $("f-batch").value,
    aes: $("f-aes").value,
  };
  try{ localStorage.setItem(LS_KEY, JSON.stringify(c)); }catch(e){}
}
function parseCourses(text){
  const out = [];
  for(const line of text.split(/\n/)){
    const m = line.match(/\d{8}/);
    if(m) out.push(m[0]);
  }
  return [...new Set(out)];
}

function beep(times){
  try{
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    for(let i=0;i<(times||1);i++){
      const o = ctx.createOscillator(), gain = ctx.createGain();
      o.connect(gain); gain.connect(ctx.destination);
      o.frequency.value = 880; gain.gain.value = 0.12;
      const t = ctx.currentTime + i*0.35;
      o.start(t); o.stop(t+0.25);
    }
  }catch(e){}
}

let since = 0, shownUuid = null, dots = [];

async function startGrab(){
  const courses = parseCourses($("f-courses").value);
  if(!$("f-stu").value.trim()) return alert("请填写学号");
  if(!$("f-pwd").value) return alert("请填写统一身份认证密码");
  if(!courses.length) return alert("请至少填写一个 8 位课程号（一行一个）");
  saveCfg();
  $("btn-start").disabled = true;
  try{
    const r = await fetch("/api/start", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        studentNumber: $("f-stu").value.trim(),
        password: $("f-pwd").value,
        courseNumbers: courses,
        campusKeyword: $("f-campus").value.trim(),
        menus: $("f-menus").value,
        cycleSeconds: $("f-cycle").value,
        maxGrab: $("f-maxgrab").value,
        failCooldown: $("f-cooldown").value,
        batchCode: $("f-batch").value.trim(),
        aesKey: $("f-aes").value.trim(),
      })});
    const j = await r.json();
    if(!j.ok) alert(j.msg || "启动失败");
  }catch(e){ alert("无法连接本地服务: " + e); }
  $("btn-start").disabled = false;
}
async function stopGrab(){ await fetch("/api/stop", {method:"POST"}); }
async function exitApp(){
  if(!confirm("确认退出程序？浏览器页面将关闭，蹲课也会停止。")) return;
  await fetch("/api/shutdown", {method:"POST"});
}

function appendLogs(logs){
  const box = $("log");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  for(const l of logs){
    const div = document.createElement("div");
    const ts = document.createElement("span");
    ts.className = "ts"; ts.textContent = l.t;
    div.appendChild(ts);
    div.appendChild(document.createTextNode(l.text));
    if(l.text.includes("🎉")) div.className = "success";
    if(l.text.includes("❌") || l.text.includes("异常") || l.text.includes("失败")) div.className = "err";
    box.appendChild(div);
    if(l.text.includes("🎉")) beep(3);
  }
  while(box.childElementCount > 1500) box.removeChild(box.firstChild);
  if(nearBottom) box.scrollTop = box.scrollHeight;
}

function renderStatus(st){
  const b = $("badge");
  const map = {idle:["空闲","idle"],running:["蹲守中","running"],login_wait:["等待验证码","login_wait"],
               done:["已结束","done"],error:["出错","error"]};
  const [txt, cls] = map[st.status] || map.idle;
  b.textContent = txt; b.className = "badge " + cls;
  $("btn-stop").disabled = !st.running;
  const list = $("grabbed-list");
  list.innerHTML = st.grabbed.map(n => "<li>" + n + "</li>").join("");
  const s = st.stats || {};
  $("stats-line").textContent = s.round
    ? `第${s.round}轮：共${s.total}门 · 候选${s.cands}门 · 已抢到${s.grabbed_count}门`
    : "";
}

// ---- 验证码 ----
function resetDots(){
  dots = [];
  document.querySelectorAll("#captcha-box .dot").forEach(d => d.remove());
}
function renderCaptcha(cap){
  if(cap.pending && cap.uuid !== shownUuid){
    shownUuid = cap.uuid;
    resetDots();
    $("captcha-img").src = "data:image/png;base64," + cap.image;
    $("captcha-overlay").classList.remove("hidden");
  } else if(!cap.pending && !$("captcha-overlay").classList.contains("hidden")){
    $("captcha-overlay").classList.add("hidden");
    shownUuid = null;
  }
}
$("captcha-img").addEventListener("click", e => {
  if(dots.length >= 4) return;
  const img = $("captcha-img");
  const rect = img.getBoundingClientRect();
  const x = Math.round((e.clientX - rect.left) * img.naturalWidth / rect.width);
  const y = Math.round((e.clientY - rect.top) * img.naturalHeight / rect.height);
  dots.push([x, y]);
  const d = document.createElement("span");
  d.className = "dot";
  d.style.left = (x / img.naturalWidth * 100) + "%";
  d.style.top = (y / img.naturalHeight * 100) + "%";
  d.dataset.i = dots.length;
  $("captcha-box").appendChild(d);
  if(dots.length === 4) setTimeout(submitCaptcha, 250);
});
async function submitCaptcha(){
  const coords = dots.map(p => p[0] + "-" + p[1]).join(",");
  try{
    const r = await fetch("/api/captcha", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({coords})});
    const j = await r.json();
    if(!j.ok){ alert(j.msg || "提交失败"); resetDots(); }
  }catch(e){ alert("提交验证码失败: " + e); }
}
$("captcha-reset").addEventListener("click", resetDots);
$("captcha-cancel").addEventListener("click", async () => {
  await fetch("/api/stop", {method:"POST"});
  $("captcha-overlay").classList.add("hidden");
  shownUuid = null;
});
window.addEventListener("keydown", e => {
  if($("captcha-overlay").classList.contains("hidden")) return;
  if(e.key === "r" || e.key === "R") resetDots();
});

async function poll(){
  try{
    const [logsR, stR, capR] = await Promise.all([
      fetch("/api/logs?since=" + since).then(r => r.json()),
      fetch("/api/status").then(r => r.json()),
      fetch("/api/captcha").then(r => r.json()),
    ]);
    if(logsR.logs && logsR.logs.length){ appendLogs(logsR.logs); since = logsR.next; }
    renderStatus(stR);
    renderCaptcha(capR);
  }catch(e){}
}

$("btn-start").addEventListener("click", startGrab);
$("btn-stop").addEventListener("click", stopGrab);
$("btn-exit").addEventListener("click", exitApp);
loadCfg();
poll();
setInterval(poll, 800);
</script>
</body>
</html>
"""


# ============================== 启动 ==============================

def main(start_port):
    host, port = "127.0.0.1", start_port
    while port < start_port + 10:
        try:
            app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
            break
        except OSError:
            port += 1
    else:
        msg = f"无法启动本地服务：端口 {start_port}-{start_port + 9} 均被占用"
        if getattr(sys, "frozen", False):
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "南大选课蹲课助手", 0x10)
        else:
            print(msg)
        sys.exit(1)


if __name__ == "__main__":
    argv = sys.argv[1:]
    start_port = 8757
    if "--port" in argv:
        try:
            start_port = int(argv[argv.index("--port") + 1])
        except Exception:
            pass
    url = f"http://127.0.0.1:{start_port}"
    if "--no-browser" not in argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"🎓 南大选课蹲课助手已启动，浏览器打开 {url}（若没自动打开请手动访问）")
    try:
        main(start_port)
    except KeyboardInterrupt:
        pass
