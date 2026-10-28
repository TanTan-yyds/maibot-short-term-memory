# 短期记忆插件

[![MaiBot](https://img.shields.io/badge/MaiBot-1.3.x-blue)](https://github.com/Mai-with-u/MaiBot)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

为 MaiBot 提供全局短期记忆。每条记忆包含：

- 4 位编号（`0-9`、`a-z`，由插件随机生成并查重）
- QQ 号
- QQ 名/群昵称
- 事情描述
- 删除条件（可选）

记忆会同时注入 Maisaka 的 Planner（规划组件）和 Replyer（回复组件）。注入提示词中的括号说明是给 AI 的使用约束：没有会话、准备使用定时任务等场景时，不要把这些短期记忆当作当前会话事实。

## 安装

从 MaiBot 插件市场搜索「短期记忆」安装，或直接使用 GitHub 仓库安装：

```text
https://github.com/TanTan-yyds/maibot-short-term-memory
```

也可以手动把整个目录放到 MaiBot 的 `plugins/` 下：

```text
plugins/short-term-memory/
├── __init__.py
├── _manifest.json
├── plugin.py
├── config.py
├── LICENSE
└── README.md
```

在 WebUI 插件管理中启用，或重启 MaiBot。首次加载会生成 `config.toml`。

## 配置

配置文件：`plugins/short-term-memory/config.toml`

```toml
[plugin]
enabled = true
inject_planner = true
inject_replyer = true
enable_tools = true
max_entries = 100
retention_hours = 72
cleanup_interval_minutes = 30
config_version = "1.0.0"
```

- `retention_hours`：短期记忆保留小时数，`0` 表示不自动过期。
- `cleanup_interval_minutes`：后台过期清理间隔。

## 注入格式

```text
短期记忆(短期记忆为全局显示，必须要有消息进入才会触发，在一些情况下建议使用定时任务之类的):
abcd昵称（123456789）:事情描述
efgh另一位（987654321）:另一件事情（删除条件：当事人表示事情已结束时删除）
```

删除条件是可选的自然语言描述，用于提示 LLM 何时应调用删除工具。插件不会自动解释或执行删除条件。

Planner 通过追加一条系统 Context Item 注入；Replyer 通过 `extra_prompt` 注入。若其他插件也使用 `extra_prompt`，本插件会追加而不是覆盖。

## Tools

- `add_short_term_memory`：`qq_number`、`event` 必填；`qq_name`、`delete_condition` 可选。编号自动生成并返回。
- `modify_short_term_memory`：`id`、`qq_number`、`event` 必填；`qq_name`、`delete_condition` 可选。不传 `delete_condition` 时保留原值，传空字符串可清除。
- `delete_short_term_memory`：`id` 必填。

三个工具都设置为 `core_tool=True`，LLM 每轮可直接看到。

## 数据

记忆保存在插件专属持久化目录：

```text
data/plugins/<plugin_id>/short_term_memories.json
```

写入采用临时文件加原子替换；卸载时停止后台清理任务并落盘。

## 验证

1. 重载插件，日志出现 `[短期记忆] 插件已加载`。
2. 在聊天中让模型创建一条记忆，工具返回 4 位编号。
3. 让 Planner/Replyer 发起请求，提示词中应出现短期记忆内容。
4. 使用修改和删除工具，再次检查提示词内容。

## 许可证

[MIT](LICENSE)
