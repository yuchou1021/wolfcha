# Wolfcha · AI 狼人杀

> LLM 驱动的多人社交推理游戏：**真人先坐，空位交给 AI**。
> 不用凑人、不用法官，打开网页就能开局。

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![PRs](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#贡献)

![Wolfcha](docs/screenshots/hero.png)

---

## 这是什么

一个跑在浏览器里的狼人杀。几个真人用手机进同一个房间，剩下的座位由大模型 AI 补齐——

- AI 有**独立人格**：每局从 39 位王者荣耀英雄里发牌，名字、口头禅、被踩时的反应都不一样；
- AI 有**信息边界**：只知道自己的身份、技能结果和公开信息，看不到别人的底牌；
- AI 会**盘逻辑**：结构化信念表记录每一夜的查验、票型和身份宣称，投票跟着自己的发言走；
- AI 有**难度梯度**：困难模式会悍跳预言家、给队友发金水、分票、刀跳出来的神职。

一局 8～12 人，真人几个、AI 几个都行，空位自动补齐。几十次模型调用，成本几分钱。

## 特性

| | |
|---|---|
| **多人同房** | 建房 → 分享链接 → 手机入座，空位自动补 AI，无需注册 |
| **AI 玩家** | 39 位英雄人格 + 板子感知 + 信念表 + 关键节点反思 |
| **难度** | 简单 / 普通 / 困难，困难档有悍跳、倒钩、分票、刀神等战术 |
| **规则** | 8～12 人固定板子、屠边胜负、同守同救必死、猎人夜枪、多数票出局、弃票 |
| **移动端** | 竖屏布局、点头像投票、快捷发言、玩家标记、逐字显示带停顿 |
| **复盘** | 每晚刀口 / 用药 / 查验 / 每轮票型，结算页一键展开 |
| **成本** | 批量生成性格、选择性反思、上下文截断，单局成本压到最低 |
| **彩蛋** | 39 位王者荣耀英雄随机发牌，名字与性格一起上桌 |

## 彩蛋：王者荣耀人格

AI 座位不只是「玩家 2 号」——每局会从 **39 位王者荣耀英雄**里发牌，给每个 AI 一个英雄名和贴合人设的性格：

- **李白·青莲剑仙**：爱用比喻，句子潇洒，常说「十步之内，皆是剑意」
- **诸葛亮·绝代智谋**：语速平稳、条理清楚，爱说「我已有七分把握」
- **张飞·万夫莫敌**：嗓门大、句子短，「俺老张说话直」
- **貂蝉·绝世舞姬**：语气柔，节奏轻，像在跳舞

几条设计：

- **同一局不重复**：池子发完才洗牌，洗牌时会避开上一局刚用过的那批；
- **避开真人重名**：如果真人叫「李白」，这局就不会再发一个 AI 李白，否则宣称解析会对不上人；
- **只是人格皮肤**：不影响身份分配、胜负判定和任何规则。

> 英雄名称与设定版权归腾讯 / 天美所有，此处仅作为粉丝向彩蛋，与官方无任何关联。

## 架构

```
  手机 / 浏览器
       │  WebSocket + HTTP
       ▼
  FastAPI 服务（房间、座位、事件广播）
       │
       ▼
  GameController（流程调度 + 规则判定）
       │
       ├── Agent 决策层（人格 / 记忆 / 信息过滤 / 策略）
       │        │
       │        ▼
       │   LLM API（DeepSeek / 通义 / 任意 OpenAI 兼容端点）
       │
       └── 事件总线 → 公共事件广播 / 私密事件单播
```

关键设计：

- **信息隔离**：夜间私密消息（狼刀、查验、女巫用药）按 `visibility_type` 过滤，绝不会进入其他玩家的上下文；
- **信念表**：每轮把存活、死讯、身份宣称、票型、按夜查验压成结构化事实，代替让模型读长文本；
- **选择性反思**：只在出局、投票、查验等关键节点触发，每次最多 2 个 AI，兼顾连贯性与成本。

## 快速开始

```bash
git clone https://github.com/yuchou1021/wolfcha.git
cd wolfcha
pip install -r requirements.txt
cp .env.example .env       # 填入模型 key
python main.py
```

打开 `http://localhost:8777`。同一局域网的手机访问 `http://<你的内网IP>:8777`。

Windows 也可以直接双击 `启动.bat`，会自动打印手机访问地址。

## 配置

```env
DEEPSEEK_API_KEY=sk-your-key
DEEPSEEK_API_BASE=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

支持任意 OpenAI 兼容接口。`llms_config/` 下可以为不同模型预设参数。

> `.env` 已被 `.gitignore` 忽略。**不要提交真实 key**；若曾泄露，请立即在模型后台重置。

## 玩法

1. 第一个人选人数、创建房间，把链接发到群里；
2. 其他人点链接，填名字、选座位；
3. 留出空位，房主点「开始游戏」，空位自动变 AI；
4. 白天按序发言、投票，夜里各自行动，直到一方达成胜利条件。

## 项目结构

```
main.py              FastAPI 服务、房间与 WebSocket
game_controller.py   游戏流程与规则判定
agent/               决策、记忆、信息过滤、策略库
game/phases/         各阶段 prompt 构建
roles/               角色策略与人格素材
static/              移动端前端
llms_config/         模型预设
```

## Roadmap

- [ ] V2：基于 [Wolfcha](https://github.com/oil-oil/wolfcha) 引擎的多人联机版（房间中继 + 私密视图）
- [ ] 更多角色：白狼王、骑士、狼美人
- [ ] 语音发言与 TTS
- [ ] 对局回放与数据统计

## 贡献

欢迎 Issue 和 PR。提交前请确认：

- 没有把 `.env`、真实 key、日志带进提交；
- 规则改动附上对应场景说明（例如“同守同救”的判定）；
- 前端改动在手机宽度下自测过；
- 欢迎用手机截几张实际界面图放到 `docs/screenshots/`（大厅 / 身份牌 / 夜晚 / 投票 / 复盘），README 会展示。

## 致谢

- 原始项目：[AgentWereWolf](https://github.com/yingyingxia666/AgentWereWolf)（MIT）
- 角色图标：[game-icons.net](https://game-icons.net)（CC BY 3.0）
- 灵感与对标：[Wolfcha](https://github.com/oil-oil/wolfcha)

## License

[MIT](LICENSE)
