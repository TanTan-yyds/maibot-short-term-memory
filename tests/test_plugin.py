from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "short_term_memory_under_test"


def load_plugin_module() -> ModuleType:
    package_spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert package_spec is not None and package_spec.loader is not None
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[PACKAGE_NAME] = package
    package_spec.loader.exec_module(package)

    plugin_spec = importlib.util.spec_from_file_location(
        f"{PACKAGE_NAME}.plugin",
        PLUGIN_DIR / "plugin.py",
    )
    assert plugin_spec is not None and plugin_spec.loader is not None
    module = importlib.util.module_from_spec(plugin_spec)
    sys.modules[plugin_spec.name] = module
    plugin_spec.loader.exec_module(module)
    return module


plugin_module = load_plugin_module()
ShortTermMemoryPlugin = plugin_module.ShortTermMemoryPlugin


class FakeLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def _record(self, level: str, message: str, *args, **kwargs) -> None:
        self.messages.append(f"{level}:{message % args if args else message}")

    def debug(self, message, *args, **kwargs):
        self._record("debug", message, *args, **kwargs)

    def info(self, message, *args, **kwargs):
        self._record("info", message, *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        self._record("warning", message, *args, **kwargs)

    def error(self, message, *args, **kwargs):
        self._record("error", message, *args, **kwargs)


class FakePaths:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class FakePerson:
    def __init__(self) -> None:
        self.names = {"123456789": "测试昵称"}

    async def get_id(self, platform: str, user_id: str):
        if user_id in self.names:
            return {"success": True, "person_id": f"pid-{user_id}"}
        return {"success": False}

    async def get_value(self, person_id: str, field_name: str):
        qq = person_id.removeprefix("pid-")
        if field_name == "person_name" and qq in self.names:
            return {"success": True, "value": self.names[qq]}
        return {"success": False}


class FakeContext:
    def __init__(self, data_dir: Path) -> None:
        self.paths = FakePaths(data_dir)
        self.logger = FakeLogger()
        self.person = FakePerson()


async def make_plugin(tmp_path: Path) -> ShortTermMemoryPlugin:
    plugin = ShortTermMemoryPlugin()
    plugin.set_plugin_config({})
    plugin._ctx = FakeContext(tmp_path)
    await plugin.on_load()
    return plugin


@pytest.mark.asyncio
async def test_create_modify_delete_and_persist(tmp_path: Path) -> None:
    plugin = await make_plugin(tmp_path)

    created = await plugin.add_short_term_memory(
        qq_number="123456789",
        event="在群里约大家周六打游戏",
    )
    assert created["success"] is True
    record_id = created["id"]
    assert re.fullmatch(r"[0-9a-z]{4}", record_id)
    assert created["memory"]["qq_name"] == "测试昵称"

    prompt = plugin._build_injection_text()
    assert prompt.startswith("短期记忆(短期记忆为全局显示")
    assert f"{record_id}测试昵称（123456789）:在群里约大家周六打游戏" in prompt

    modified = await plugin.modify_short_term_memory(
        id=record_id,
        qq_number="987654321",
        event="改约周日打游戏",
        qq_name="新昵称",
    )
    assert modified["success"] is True
    assert modified["memory"]["qq_number"] == "987654321"
    assert modified["memory"]["qq_name"] == "新昵称"
    assert modified["memory"]["event"] == "改约周日打游戏"

    await plugin.on_unload()

    reloaded = await make_plugin(tmp_path)
    assert record_id in reloaded._memories
    assert reloaded._memories[record_id].qq_name == "新昵称"

    deleted = await reloaded.delete_short_term_memory(id=record_id)
    assert deleted["success"] is True
    assert reloaded._build_injection_text() == ""
    await reloaded.on_unload()


@pytest.mark.asyncio
async def test_injection_shapes_are_valid(tmp_path: Path) -> None:
    plugin = await make_plugin(tmp_path)
    await plugin.add_short_term_memory(
        qq_number="123456789",
        qq_name="小明",
        event="忘记带钥匙",
    )

    kwargs = {"items": [], "item_schema_version": 1}
    planner_result = await plugin.inject_planner(**kwargs)
    assert planner_result is not None
    item = planner_result["modified_kwargs"]["items"][0]
    assert item["item_type"] == "SystemMessageItem"
    assert item["meta"]["logical_turn_id"] is None
    assert item["parts"][0]["type"] == "text"
    assert "小明（123456789）:忘记带钥匙" in item["parts"][0]["text"]

    replyer_kwargs = {"extra_prompt": "已有要求"}
    replyer_result = await plugin.inject_replyer(**replyer_kwargs)
    assert replyer_result is not None
    prompt = replyer_result["modified_kwargs"]["extra_prompt"]
    assert prompt.startswith("已有要求\n\n短期记忆(")
    assert "小明（123456789）:忘记带钥匙" in prompt

    await plugin.on_unload()


@pytest.mark.asyncio
async def test_injection_switches_are_respected(tmp_path: Path) -> None:
    plugin = await make_plugin(tmp_path)
    await plugin.add_short_term_memory(
        qq_number="123456789",
        qq_name="小明",
        event="忘记带钥匙",
    )

    plugin.set_plugin_config(
        {
            "plugin": {
                "inject_planner": False,
                "inject_replyer": False,
                "config_version": "1.0.0",
            }
        }
    )
    assert await plugin.inject_planner(items=[]) is None
    assert await plugin.inject_replyer(extra_prompt="") is None

    await plugin.on_unload()


@pytest.mark.asyncio
async def test_id_collision_check_and_tool_validation(tmp_path: Path) -> None:
    plugin = await make_plugin(tmp_path)

    first = await plugin.add_short_term_memory(
        qq_number="123456789",
        qq_name="甲",
        event="第一件事",
    )
    second = await plugin.add_short_term_memory(
        qq_number="123456789",
        qq_name="乙",
        event="第二件事",
    )
    assert first["id"] != second["id"]
    assert plugin._normalize_id(first["id"].upper()) == first["id"]

    invalid_qq = await plugin.add_short_term_memory(qq_number="abc", event="x")
    assert invalid_qq["success"] is False
    too_long = await plugin.add_short_term_memory(
        qq_number="123456789",
        event="x" * 501,
    )
    assert too_long["success"] is False

    missing = await plugin.modify_short_term_memory(
        id="zzzz",
        qq_number="123456789",
        event="不存在",
    )
    assert missing["success"] is False

    # 强制下一次随机生成与已有编号冲突，验证查重后会继续生成新编号。
    choices = iter(["a", "a", "a", "a", "b", "b", "b", "b"])
    original_choice = plugin_module.secrets.choice
    plugin_module.secrets.choice = lambda alphabet: next(choices)
    try:
        plugin._memories["aaaa"] = plugin_module.MemoryRecord(
            id="aaaa",
            qq_number="123456789",
            qq_name="冲突",
            event="占位",
            created_at=plugin._now_iso(),
            updated_at=plugin._now_iso(),
        )
        assert plugin._new_id_locked() == "bbbb"
    finally:
        plugin_module.secrets.choice = original_choice

    await plugin.on_unload()
