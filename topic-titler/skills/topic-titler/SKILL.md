---
name: topic-titler
description: 话题自动命名的会话内控制：预览候选标题、改名/固定、解锁、暂停/恢复全局自动命名、查看状态与体检。当用户想管理话题标题（"预览这个话题的新标题""把标题改成 XXX""暂停自动命名"等）时使用。
---

# topic-titler 会话内控制

话题自动命名由两个后台钩子完成：首条消息提交后立即起初版标题，之后每轮对话结束再精炼与稳定（全程静默，不打断对话）。本技能用于在对话中响应用户的控制请求，全部通过插件自带 CLI 执行（`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/topic_titler.py" <子命令>`，`$CLAUDE_PLUGIN_ROOT` 是插件安装根目录；本地源码开发时用插件目录绝对路径代替）。

## 请求 → 命令对照

| 用户请求 | 命令 |
| --- | --- |
| 预览这个话题的新标题 | `preview`（可加 `--session sess_xxx` 或 `--cwd <目录>`） |
| 把标题改成 XXX / 固定为 XXX | `rename --title "XXX" --apply`（先给用户看当前标题再改；写入即锁定） |
| 恢复这个话题的自动命名 | `unlock` |
| 固定当前标题（不指定新标题） | `lock` |
| 暂停自动命名（全局） | `pause` |
| 恢复自动命名（全局） | `resume` |
| 命名状态 / 用量 | `status` |
| 检查是否正常 | `doctor --ping` |

## 规则

- 所有命令输出 JSON；向用户转述 `status`、`action`、`title`、`reason` 字段，错误时转述 `message`。
- `preview` 默认取"最近更新的未删除会话"；用户在指定会话上下文提问时优先传 `--session`。
- `rename --apply` 属于修改操作：先向用户复述候选/目标标题，得到确认再执行；执行后告知"已固定，恢复自动命名说一声即可"。
- 标题规范由模型策略保证（类别 emoji + 对象｜目标）；不要手工拼标题绕过 `rename`，除非用户明确给出完整标题文本。
- 数据目录：`~/.zcode/topic-titler/`（config.json、threads/ 每会话状态、logs/ 审计日志）；API key 来源优先级：插件 config > 环境变量 `TOPIC_TITLER_API_KEY` > ZCode 配置里 bigmodel MCP key。key 一律脱敏展示。
