# NJU 选课蹲课工具

南京大学选课平台（xk.nju.edu.cn）的蹲课脚本集合：自动登录（点选验证码）、按课程编号/菜单蹲守、每 5 秒刷新一次、有未满名额即提交、抢到响铃。

> ⚠️ **风险声明**：使用脚本选课可能违反学校选课规定，可能导致选课资格被取消、账号受限等后果；请求频率过高还可能被服务器限流封号。请自行了解学校规定、谨慎使用、后果自负。本项目仅供学习交流。

## 脚本一览

| 脚本 | 用途 |
|------|------|
| `grab_web.py` | **网页版蹲课（推荐）**：本地起一个网页界面，填账号/密码/课程号即用，验证码在网页里点选，实时日志；可打包成 exe 分享给没有 Python 环境的同学 |
| `grab_custom.py` | **通用蹲课**：自定义学号/密码/课程编号（8 位课程号），扫全部开放菜单蹲守 |
| `grab_tongshi.py` | 蹲仙林通识课（GG02 菜单），服务端过滤未满+不冲突 |
| `grab_sizheng.py` | 蹲思政包课程 + 自定义关键词（口语等），扫描 GG01/GG02 |
| `grab_one.py` | 单课程号一次抢课：搜教学班并提交一次 |
| `grab_rush.py` | 准点抢课（按开放时刻开抢）+ 登录加密实现（其他脚本的依赖） |
| `nju_des.py` | 密码 DES 加密移植实现（依赖） |

## 环境准备

本机装好 Python 3.10+：

```powershell
pip install -r requirements.txt
```

## 快速开始（grab_custom.py）

1. 用记事本打开 `grab_rush.py`，在顶部填：
   - `STUDENT_NUMBER` 你的学号
   - `DEFAULT_PASSWORD` 你的统一身份认证密码（明文存储，用完请删除并改密码；留空则每次运行弹密码框）
2. 打开 `grab_custom.py`，改顶部配置区：
   - `TARGET_COURSE_NUMBERS` 要蹲的课程编号列表（也可命令行直接传）
   - `CAMPUS_KEYWORD` 只抢这个校区（留空 `""` = 不限校区）
3. 运行：

```powershell
python grab_custom.py                          # 按配置区蹲守
python grab_custom.py 00371690 78005020        # 命令行直接传课程编号
python grab_custom.py --dry                    # 只看不抢（先看候选名单）
```

运行后按提示点一次验证码（顺序点击图中 4 个字），之后每 5 秒自动刷新，目标课有未满名额就提交，抢到即响铃退出（默认抢 1 门就停，可在 `MAX_GRAB` 修改）。

## 网页版（grab_web.py）

有图形界面的版本：启动后自动打开浏览器页面，**网页里填账号、密码、课程号**（可带校区关键词、刷新间隔等高级参数），点「开始蹲课」→ 验证码**直接在网页里点选** → 实时日志滚动、会话失效自动重登、抢到响铃。密码默认只存内存，不落盘。

```powershell
pip install -r requirements.txt   # 需要 flask（网页版新增）
python grab_web.py                # 自动打开 http://127.0.0.1:8757
```

**没有 Python 环境的同学**：到本仓库 [Releases](https://github.com/JQ-young/NJU-grab-tool/releases) 下载 `NJU_Grab.exe`，双击即用（Windows 10/11，首次启动稍慢属正常）。若浏览器没有自动打开，手动访问 http://127.0.0.1:8757 ，用完在页面点「退出程序」。

打包命令（维护者用）：

```powershell
pip install pyinstaller
python -m PyInstaller --onefile --noconsole --name NJU_Grab grab_web.py
# 产物在 dist/NJU_Grab.exe，作为 Release 资产上传
```

## 技术说明

- 登录密码加密：`base64(DES strEnc(pwd, "this", "password", "is"))`（与前端 index.min.js 一致）
- 选课请求 `volunteer.do` 负载 AES-ECB 加密，密钥取自页面全局变量 `avy`（在 `grab_rush.py` 的 `AES_KEY` 处，若报参数错误请到浏览器 F12 控制台输入 `avy` 获取当前值）
- 选课批次代码 `BATCH_CODE` 来自登录后 `xkxf.do` 请求的 `xklcdm` 字段
- 通识课列表用 `/elective/publicCourse.do`（顶部菜单"公共"→通识课 GG02），支持服务端过滤参数 `checkCapacity=0`（只看未满）/`checkConflict=0`（只看不冲突）
- 默认请求频率温和（每轮 ≥5 秒），请勿调太快

## 致谢

登录加密与接口逆向参考 [TheFunny233/NJUClassGrabber](https://github.com/TheFunny233/NJUClassGrabber)（原作者 [lyc8503](https://github.com/lyc8503)，MIT License）。

## License

MIT
