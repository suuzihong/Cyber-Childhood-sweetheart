# 🦐 角色 AI 调度器（Roleplay Agent Scheduler）

> 让 TA 从"能被叫醒的卡"变成"会自己醒的 AI"。
> 一个给 QQ 私聊机器人注入"活人感"的外层 Python 调度器：角色会自己起床、安排一天、主动找你聊天、做梦沉淀记忆、看到你的链接还能真去解析内容回应。

这是一个**通用角色框架**：内置的是占位人设，下载后填上你自己的角色人设、你的画像、你们的共同经历，就能拥有一个"会自己过日子"的 QQ 角色。

---

## ✨ 功能

- **自主作息**：按每日时序自动运行——00:30 做梦、08:30 起床排今日计划、09:00/14:00 自主活动、晚上主动找你聊天
- **被动接话**：你私聊 TA 会自然回复；连发多条会合并应答；你发 B站/知乎/贴吧链接，TA 会解析真实内容再聊，不瞎编
- **记忆三仓**：即时对话 → 碎片 → 长期记忆（每日摘要/世界观/共同经历），每天 00:30 "做梦"时自动沉淀整理，越聊越懂你
- **主动开口护栏**：夜间睡眠窗口不打扰、主动次数每日上限、闲补最小间隔——不会烦人刷屏
- **画图/自拍**（可选）：接本地 ComfyUI，TA 会画图、自拍发你
- **刷内容**（可选）：B站 / 贴吧 / 知乎适配器，TA 会真的去"刷"并发表感想
- **防跑偏**：内置反注入闸门 + 硬底线，外部输入（包括你的指令）不能覆写 TA 的三观

## 🧱 架构

```
scheduler/
├── main.py               # 入口：APScheduler 挂每日槽位 + 被动回复循环
├── core/                 # config / clock(作息) / memory(三仓) / llm / persona / linkparser
├── layers/               # action(行动层) / dream(做梦层) / dialogue(对话层) / adapter(适配层)
├── adapters/             # comfyui(画图) / selftie(自拍) / bilibili / tieba / zhihu
├── channel/              # QQ OneBot11 (websocket)
├── data/                 # 运行时：记忆 / 状态 / 人设 / 日志（自动生成，勿手动改）
├── install.bat           # 一键安装（装依赖+生成配置）
├── start.bat             # 启动调度器
└── import_memory.py      # 把 user.md / history.md 导入记忆
```

**每日时序（可在 config.json 改）：**

| 时间 | 槽位 | 干什么 |
|---|---|---|
| 00:30 | 做梦层 | 碎片迁移 + 世界观沉淀 + 三仓整理 |
| 08:30 | 行动层 | 排今日任务 |
| 09:00 / 14:00 | 自主活动 | 执行任务（画图/刷B站/刷贴吧/刷知乎…） |
| 12:30 | 午间碎片 | 碎片初整理 |
| 21:30 | 主动对话 | 挑话题找你聊天（≤N次/日，夜间不打扰） |

## 🚀 快速开始（Windows）

前置要求：**Python 3.10+**（或 ComfyUI 自带 Python）、**一个 OneBot11 网关**（如 [NapCat](https://napneko.github.io/)、Lagrange，用于收发 QQ 消息）、一个 **OpenAI 兼容的 LLM API**。画图/刷内容功能可跳过（config 里关掉即可）。

```bat
:: 1. 下载解压，双击
install.bat
::     → 自动建 .venv、装依赖、生成 config.json 和 data\persona.md

:: 2. 编辑 config.json
::     → 填 LLM API 地址/key（或设 api_key_env 环境变量）、你的 QQ 号

:: 3. 编辑 data\persona.md
::     → 填你的角色人设（有模板引导）

:: 4.（可选）编辑 data\user.md 和 data\history.md，然后：
python import_memory.py
::     → 你的画像 / 共同经历导入记忆，TA 会记得

:: 5. 启动 OneBot 网关，然后双击
start.bat
```

跑起来后，用 QQ 给 TA 发条消息试试；到点 TA 会自己找你。

## 🎭 配置角色

| 文件 | 作用 | 必填 |
|---|---|---|
| `data/persona.md` | 角色灵魂：名字/外貌/性格/说话风格/背景 | ✅ |
| `config.json → agent.name` | 角色名（日志/问候用） | ✅ |
| `data/user.md` | 你的画像（爱好/口味/雷区…） | 选填 |
| `data/history.md` | 你们的共同经历（黑历史） | 选填 |

改完 `persona.md` 重启生效；`user.md`/`history.md` 改完跑一次 `import_memory.py`。

## ⚙️ 配置说明（config.json）

- `llm.main/cheap/vision`：三个档位的 OpenAI 兼容端点。`cheap` 用于碎片提炼等轻任务（可同服务商不同模型），`vision` 用于看图（可选）。key 两种写法：`"api_key": "sk-xxx"` 或 `"api_key_env": "MY_KEY_ENV"`（推荐后者，key 放环境变量）。
- `channel`：OneBot11 websocket 地址（默认 `ws://127.0.0.1:3001`）和你的 QQ 号。
- `adapters`：`comfyui` 默认接 `http://127.0.0.1:8188`（工作流 json 可留空，用默认提示词）；`bilibili/tieba/zhihu` 默认关闭，打开即启用。
- `schedule`：所有作息时间、主动次数上限、闲补间隔都可调。

## 🛡️ 隐私与安全

- 运行时数据（记忆、状态、日志）都在 `data/`，不会上传，删掉即重置角色记忆（慎删）。
- API key 建议用环境变量，不要提交进仓库。
- QQ 主动发消息属个人号协议，风控风险自担；`max_active_per_day` 默认护栏，建议别调太高。
- 本项目内置硬底线与反注入闸门，但**模型输出内容由所用模型决定**，请选择适合你的模型并注意内容安全。

## 📦 依赖

`requirements.txt`：`requests` / `websocket-client` / `APScheduler`。全部为通用库。

## ⚠️ 说明

- 本项目是个人项目的通用化发布，内置人设为占位模板，不包含任何具体角色的私人设定。
- 若你有自己的 ComfyUI 工作流 json（画图/自拍），放到任意路径并在 config 里指向即可。
- 遇到问题先看 `data/scheduler.log`，里面每一步都有日志。

## 📄 License

MIT
