# topic-titler

让 ZCode 的话题标题跟上你正在做的事情。每轮对话结束后，后台参考最近几轮内容自动更新标题，话题再多也更容易找到。

> 本插件是 [oil-oil/oil-codex-title](https://github.com/oil-oil/oil-codex-title)（MIT）的 **ZCode 移植版**，保留其命名策略与稳定性设计，按 ZCode 的钩子、数据库与凭据机制重写了实现。

## 命名效果

| 原来的标题 | 更容易找回的标题 |
| --- | --- |
| 回应中文问候 | 🧩 邮箱验证码｜过期排查 |
| 确认注册功能 | 🧩 支付回调｜重复发货修复 |
| 讨论视频标题 | 🎬 图像模型评测｜视频策划 |
| 继续修改 | 🎨 登录表单｜布局优化 |

统一采用 **「类别 emoji + 对象｜目标」**：

- 🎬 内容制作 · 🧩 工具开发 · 🔎 对比调研 · 🎨 页面设计 · 📝 方法整理 · 📅 日程安排 · ⚙️ 环境配置 · 💬 一般讨论
- 对象在前，目标在后；跟随你最近几轮的主要语言（如 `🧩 Email verification｜Fix expiry`）。
- 名称稳定：只有工作目标实质变化时才更新，"继续""推送"不会取代主线。

## 工作原理

- **触发**：插件注册两个钩子——`UserPromptSubmit` 在首条消息提交后立即生成初版标题（会话一开始就有合适名称）；`Stop` 在每轮回复结束后静默精炼与稳定。均后台运行，不往对话里添加消息。
- **命名**：独立调用 GLM Flash 模型（默认 `glm-4.5-flash`，免费档）生成候选标题，经严格校验（emoji 类别、全角「｜」结构、长度、无私人信息）后写入。
- **写入**：直接更新 ZCode 会话数据库的标题元数据（`title_source=generated`），与 ZCode 内置标题生成同通道。
- **稳定机制**（继承自原版）：内容指纹去重、纯确认轮跳过、冲突标题重试、手动改名检测与锁定保护。
- **凭据**：默认复用 ZCode 配置中已有的 bigmodel key（MCP 服务配置），也可在插件配置或环境变量 `TOPIC_TITLER_API_KEY` 中显式指定。密钥只在本机使用，绝不写入日志。

## 安装

1. **添加市场源**：ZCode 桌面端 → 设置 → 插件管理 → 发现 → `+` → 添加插件市场，填入 `stevenwangking/steven-zcode-plugins`（本地开发可改填目录 `/Users/steven/ZCodeProject/plugins`）。
2. **安装插件**：在该市场下找到 `topic-titler`，点安装。
3. 重启一个新话题正常聊天即可，无需其他配置。

> 需要本机装有 `python3`（macOS 自带）。

## 日常使用（对话中说）

- "预览这个话题的新标题。"
- "把标题改成 🧩 登录表单｜布局优化。"（写入即固定；恢复自动命名说"解锁"即可）
- "固定这个话题的标题。" / "恢复这个话题的自动命名。"
- "暂停自动命名。" / "恢复自动命名。"
- "命名状态怎么样？" / "检查命名是否正常。"

（由插件自带 skill 驱动 agent 执行对应 CLI。）

## 数据与隐私

- 数据目录：`~/.zcode/topic-titler/`（配置、每会话状态、审计日志、用量统计）。
- 审计日志只记录决策结论与 token 用量，不含对话原文与提示词。
- 发送给命名模型的上下文仅为最近几轮对话摘要（默认 5 轮、12000 字符内），用于生成标题。

## 与原版的差异

| | oil-codex-title（Codex） | topic-titler（ZCode） |
| --- | --- | --- |
| 触发 | Codex Stop Hook（async） | UserPromptSubmit + Stop 双钩子 + 脚本自行后台化（ZCode 暂无 async 钩子） |
| 读取对话 | Codex app-server `thread/read` | 直接读 ZCode 会话数据库 message/part 表 |
| 写入标题 | Codex app-server `thread/name/set` | 更新 session 表（title/title_source/title_message_id/time_title_updated） |
| 命名模型 | GPT-6 Luna Fast（`codex exec --ephemeral`） | GLM Flash 直连 API（凭据复用本机配置） |
| 归档 | 闲置话题自动归档 | 未移植（ZCode 无对应归档 API） |

## 许可证

MIT。本插件衍生自 oil-codex-title，原版版权声明保留于 LICENSE。
