"""在 MaiBot 容器内验证 Host 协议与工具调用。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
from pathlib import Path


def load_plugin():
    plugin_dir = Path("/MaiMBot/plugins/short-term-memory")
    package_name = "stm_host_verify"
    package_spec = importlib.util.spec_from_file_location(
        package_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    assert package_spec is not None and package_spec.loader is not None
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[package_name] = package
    package_spec.loader.exec_module(package)

    plugin_spec = importlib.util.spec_from_file_location(
        f"{package_name}.plugin",
        plugin_dir / "plugin.py",
    )
    assert plugin_spec is not None and plugin_spec.loader is not None
    module = importlib.util.module_from_spec(plugin_spec)
    sys.modules[plugin_spec.name] = module
    plugin_spec.loader.exec_module(module)
    return module


class Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        print("ERROR", args, kwargs)


class Paths:
    def __init__(self, path: Path) -> None:
        self.data_dir = path


class Person:
    async def get_id(self, platform: str, user_id: str):
        return {"success": True, "person_id": f"pid-{user_id}"}

    async def get_value(self, person_id: str, field_name: str):
        return {"success": True, "value": "验证昵称"}


class Context:
    def __init__(self, path: Path) -> None:
        self.paths = Paths(path)
        self.logger = Logger()
        self.person = Person()


async def main() -> None:
    module = load_plugin()
    with tempfile.TemporaryDirectory() as temp_dir:
        plugin = module.ShortTermMemoryPlugin()
        plugin.set_plugin_config({})
        plugin._ctx = Context(Path(temp_dir))
        await plugin.on_load()

        created = await plugin.add_short_term_memory(
            qq_number="123456789",
            event="真实 Host 验证事项",
            delete_condition="验证完成后删除",
        )
        print("CREATE", json.dumps(created, ensure_ascii=False))
        record_id = created["id"]

        planner_result = await plugin.inject_planner(items=[], item_schema_version=1)
        assert planner_result is not None
        raw_items = planner_result["modified_kwargs"]["items"]
        from src.plugin_runtime.hook_payloads import deserialize_prompt_items

        parsed = deserialize_prompt_items(raw_items, item_schema_version=1, original_items=[])
        planner_line = parsed[0].parts[0].text.splitlines()[1]
        assert "（删除条件：验证完成后删除）" in planner_line
        print(
            "PLANNER_ITEM",
            raw_items[0]["item_type"],
            parsed[0].role.value,
            planner_line,
        )

        replyer_result = await plugin.inject_replyer(extra_prompt="")
        assert replyer_result is not None
        replyer_prompt = replyer_result["modified_kwargs"]["extra_prompt"]
        assert "（删除条件：验证完成后删除）" in replyer_prompt
        print("REPLYER_PROMPT", replyer_prompt.splitlines()[1])

        modified = await plugin.modify_short_term_memory(
            id=record_id,
            qq_number="987654321",
            event="修改后事项",
            qq_name="新验证昵称",
            delete_condition="验证结束后删除",
        )
        print("MODIFY", modified["success"], json.dumps(modified["memory"], ensure_ascii=False))

        cleared = await plugin.modify_short_term_memory(
            id=record_id,
            qq_number="987654321",
            event="修改后事项",
            qq_name="新验证昵称",
            delete_condition="",
        )
        assert cleared["success"] is True
        assert cleared["memory"]["delete_condition"] == ""
        print("MODIFY_CLEAR", json.dumps(cleared["memory"], ensure_ascii=False))

        deleted = await plugin.delete_short_term_memory(id=record_id)
        print("DELETE", deleted["success"], len(plugin._memories))
        await plugin.on_unload()


if __name__ == "__main__":
    asyncio.run(main())
