# 宿舍狼人杀 · 4 真人 + AI 补位

一个可以在宿舍局域网里玩的网页狼人杀：**4 个真人用手机进同一个房间，空座位自动由 LLM AI 补上**。不用下载 App，不用凑人，不用法官。

> 这是在开源项目 [AgentWereWolf](https://github.com/yingyingxia666/AgentWereWolf)（MIT）基础上做了大量改造的版本：重写移动端 UI、补规则判定、加结构化信念表、加困难模式战术、加赛后复盘等。

## 特性

- **4 真人 + AI 同房**：建房、分享链接、手机入座，空位自动补 AI；
- **8～12 人局**，固定板子，标准屠边胜负；
- **规则完整**：同守同救必死、猎人夜里被刀可开枪、投票过半才出局、可弃票；
- **AI 会盘逻辑**：结构化信念表（按夜记录查验/票型/宣称）、关键节点反思、困难模式会悍跳/刀神/分票；
- **手机竖屏 UI**：座位网格、点头像投票、快捷发言、玩家标记、逐字显示带停顿；
- **赛后复盘**：每晚刀口、用药、查验、每轮票型一目了然。

## 快速开始

需要 Python 3.9+。

```bash
pip install -r requirements.txt
cp .env.example .env        # 填入你的模型 key
python main.py
```

打开 `http://localhost:8777`。同一 WiFi 下的手机访问 `http://<电脑局域网IP>:8777`。

Windows 用户也可以直接双击 `启动.bat`（会自动打印手机访问地址；如果仓库里没有这个文件，可自行创建）。

## 配置

`.env`：

```env
DEEPSEEK_API_KEY=sk-你的key
DEEPSEEK_API_BASE=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

支持任何 OpenAI 兼容接口（DeepSeek、通义千问等）。一局 8 人几十次调用，成本几分钱。

> `.env` 已被 `.gitignore` 忽略，**不要提交真实 key**。若 key 曾经被提交或公开，请立刻去模型后台重置。

## 怎么玩

1. 第一个人选人数、创建房间，把房间链接发群里；
2. 其他人点链接，先填名字，再点座位；
3. 留几个空位，房主点「开始游戏」，空位自动变 AI；
4. 4 真人 + 4 AI 就是 8 人局。

## 目录结构

```
main.py              FastAPI 服务 + 房间/WebSocket
game_controller.py   游戏流程与规则判定
agent/               AI 决策、记忆、信息过滤、策略
game/phases/         各阶段 prompt
roles/               角色策略库
static/              前端（手机竖屏）
llms_config/         模型配置（key 从 .env 读）
```

## 致谢

- 原始项目：[AgentWereWolf](https://github.com/yingyingxia666/AgentWereWolf)（MIT）
- 角色图标：[game-icons.net](https://game-icons.net)（CC BY 3.0）

## License

MIT
