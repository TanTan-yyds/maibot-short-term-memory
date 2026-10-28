"""短期记忆插件配置模型。"""

from __future__ import annotations

from maibot_sdk import PluginConfigBase
from pydantic import Field

PLUGIN_VERSION = "1.0.4"
PLUGIN_CONFIG_VERSION = "1.0.0"


class ShortTermMemorySectionConfig(PluginConfigBase):
    """短期记忆设置。"""

    __ui_label__ = "短期记忆设置"
    __ui_icon__ = "memory"
    __ui_order__ = 1

    enabled: bool = Field(
        default=True,
        description="插件总开关",
        json_schema_extra={"label": "启用插件"},
    )
    inject_planner: bool = Field(
        default=True,
        description="在规划器（Planner）请求模型前注入全局短期记忆",
        json_schema_extra={"label": "注入规划器"},
    )
    inject_replyer: bool = Field(
        default=True,
        description="在回复器（Replyer）请求模型前注入全局短期记忆",
        json_schema_extra={"label": "注入回复器"},
    )
    enable_tools: bool = Field(
        default=True,
        description="启用创建、修改、删除短期记忆工具",
        json_schema_extra={"label": "启用记忆工具"},
    )
    max_entries: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="最多保留的记忆条数；达到上限后拒绝继续创建",
        json_schema_extra={"label": "最大记忆条数", "min": 1, "max": 1000},
    )
    retention_hours: int = Field(
        default=72,
        ge=0,
        le=8760,
        description="短期记忆保留小时数；0 表示不自动过期",
        json_schema_extra={"label": "保留小时数", "min": 0, "max": 8760},
    )
    cleanup_interval_minutes: int = Field(
        default=30,
        ge=1,
        le=1440,
        description="后台清理过期记忆的间隔（分钟）",
        json_schema_extra={"label": "清理间隔(分钟)", "min": 1, "max": 1440},
    )
    config_version: str = Field(
        default=PLUGIN_CONFIG_VERSION,
        description="配置版本号（请勿手动修改）",
        json_schema_extra={"label": "配置版本", "disabled": True},
    )


class ShortTermMemoryConfig(PluginConfigBase):
    """插件顶层配置。"""

    __ui_label__ = "短期记忆"
    __ui_icon__ = "memory"

    plugin: ShortTermMemorySectionConfig = Field(default_factory=ShortTermMemorySectionConfig)
