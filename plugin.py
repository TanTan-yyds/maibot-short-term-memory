"""MaiBot 全局短期记忆插件。

记忆条目包含：四位编号、QQ 名、QQ 号、事情描述。
记忆会注入 Maisaka Planner 与 Replyer 的提示词，并通过三个 Tool 进行维护。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import string
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import (
    ErrorPolicy,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

from .config import PLUGIN_VERSION, ShortTermMemoryConfig

_PLANNER_HOOK = "maisaka.planner.before_request"
_REPLYER_HOOK = "maisaka.replyer.before_request"

_ID_ALPHABET = string.digits + string.ascii_lowercase
_ID_LENGTH = 4
_ID_RE = re.compile(r"^[0-9a-z]{4}$")
_QQ_RE = re.compile(r"^\d{5,15}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")

_MAX_QQ_NAME_CHARS = 64
_MAX_EVENT_CHARS = 500
_MAX_DELETE_CONDITION_CHARS = 200
_PROMPT_HEADER = (
    "短期记忆(短期记忆为全局显示，必须要有消息进入才会触发，"
    "在一些情况下建议使用定时任务之类的，在一般情况下删除条件一般都是发生一次就算结束，"
    "这个要你自行判断或者询问，也可以不写):"
)


def _clean_text(value: Any) -> str:
    normalized = _CONTROL_RE.sub(" ", str(value or ""))
    return " ".join(normalized.split()).strip()


@dataclass(slots=True)
class MemoryRecord:
    """一条短期记忆。"""

    id: str
    qq_number: str
    qq_name: str
    event: str
    created_at: str
    updated_at: str
    delete_condition: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> MemoryRecord | None:
        if not isinstance(data, dict):
            return None
        record_id = str(data.get("id") or "").strip().lower()
        qq_number = str(data.get("qq_number") or "").strip()
        qq_name = str(data.get("qq_name") or "").strip()
        event = str(data.get("event") or "").strip()
        created_at = str(data.get("created_at") or "").strip()
        updated_at = str(data.get("updated_at") or created_at).strip()
        delete_condition = _clean_text(data.get("delete_condition"))[
            :_MAX_DELETE_CONDITION_CHARS
        ]
        if not _ID_RE.fullmatch(record_id) or not _QQ_RE.fullmatch(qq_number):
            return None
        if not qq_name or not event or not updated_at:
            return None
        return cls(
            id=record_id,
            qq_number=qq_number,
            qq_name=qq_name,
            event=event,
            created_at=created_at or updated_at,
            updated_at=updated_at,
            delete_condition=delete_condition,
        )


class ShortTermMemoryPlugin(MaiBotPlugin):
    """短期记忆插件。"""

    config_model = ShortTermMemoryConfig

    def __init__(self) -> None:
        super().__init__()
        self._memories: dict[str, MemoryRecord] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None

    # ─── 生命周期 ─────────────────────────────────────────────

    async def on_load(self) -> None:
        self._load_from_disk()
        async with self._lock:
            removed = self._purge_expired_locked()
            if removed:
                self._save_locked()
        self._restart_cleanup_task()
        self.ctx.logger.info(
            "[短期记忆] 插件已加载，当前记忆 %d 条 (version=%s)",
            len(self._memories),
            PLUGIN_VERSION,
        )

    async def on_unload(self) -> None:
        await self._stop_cleanup_task()
        async with self._lock:
            self._save_locked()
        self.ctx.logger.info("[短期记忆] 插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope != "self":
            return
        async with self._lock:
            removed = self._purge_expired_locked()
            if removed:
                self._save_locked()
        self._restart_cleanup_task()
        self.ctx.logger.info(
            "[短期记忆] 配置已更新: version=%s, 清理过期记忆=%d",
            version,
            removed,
        )

    # ─── 规划器注入 ───────────────────────────────────────────

    @HookHandler(
        _PLANNER_HOOK,
        name="short_term_memory_planner_inject",
        description="在 Planner 请求模型前注入全局短期记忆",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_planner(self, **kwargs: Any) -> dict[str, Any] | None:
        try:
            if not self._cfg().inject_planner:
                return None
            prompt = self._build_injection_text()
            if not prompt:
                return None

            items = kwargs.get("items")
            if isinstance(items, list):
                items.append(self._build_system_item(prompt))
                kwargs["items"] = items
                return {"action": "continue", "modified_kwargs": kwargs}
            return None
        except Exception as exc:
            self.ctx.logger.error("[短期记忆] Planner 注入失败: %s", exc, exc_info=True)
            return None

    # ─── 回复器注入 ───────────────────────────────────────────

    @HookHandler(
        _REPLYER_HOOK,
        name="short_term_memory_replyer_inject",
        description="在 Replyer 请求模型前通过 extra_prompt 注入全局短期记忆",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_replyer(self, **kwargs: Any) -> dict[str, Any] | None:
        try:
            if not self._cfg().inject_replyer:
                return None
            prompt = self._build_injection_text()
            if not prompt:
                return None
            existing = str(kwargs.get("extra_prompt") or "").strip()
            kwargs["extra_prompt"] = f"{existing}\n\n{prompt}".strip() if existing else prompt
            return {"action": "continue", "modified_kwargs": kwargs}
        except Exception as exc:
            self.ctx.logger.error("[短期记忆] Replyer 注入失败: %s", exc, exc_info=True)
            return None

    # ─── Tool：创建 ───────────────────────────────────────────

    @Tool(
        "add_short_term_memory",
        brief_description="创建一条全局短期记忆，记录某位 QQ 用户做过或经历过的事情。",
        detailed_description=(
            "创建短期记忆。必须提供 qq_number（QQ号）和 event（对事情的描述）。"
            "qq_name 可选；未提供时会尝试从人物资料中自动解析，解析失败则记为“未知QQ用户”。"
            "delete_condition 可选，用于描述满足什么条件时应删除这条记忆；"
            "通常发生一次就算结束，需要你自行判断或询问用户，也可以不写。"
            "编号由插件随机生成 4 位小写字母/数字并自动查重，调用者不要自行传编号。"
            "返回 content 与生成的 id；后续修改、删除都使用该 id。"
        ),
        parameters=[
            ToolParameterInfo(
                name="qq_number",
                param_type=ToolParamType.STRING,
                description="记忆所属用户的 QQ 号（纯数字，必填）",
                required=True,
            ),
            ToolParameterInfo(
                name="event",
                param_type=ToolParamType.STRING,
                description="对事情的描述（必填，建议一句话说清人物、动作和结果）",
                required=True,
            ),
            ToolParameterInfo(
                name="qq_name",
                param_type=ToolParamType.STRING,
                description="QQ 名或群昵称（可选；不填时自动查询）",
                required=False,
            ),
            ToolParameterInfo(
                name="delete_condition",
                param_type=ToolParamType.STRING,
                description="可选的删除条件，例如“当事人明确表示事情已结束时删除”",
                required=False,
            ),
        ],
        core_tool=True,
    )
    async def add_short_term_memory(
        self,
        qq_number: str = "",
        event: str = "",
        qq_name: str = "",
        delete_condition: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            if not self._cfg().enabled or not self._cfg().enable_tools:
                return self._fail("短期记忆工具已关闭")
            normalized_qq = self._normalize_qq(qq_number)
            normalized_event = self._normalize_event(event)
            normalized_condition = self._normalize_delete_condition(delete_condition)
            if not normalized_qq:
                return self._fail("qq_number 必须是 5-15 位纯数字 QQ 号")
            if not normalized_event:
                return self._fail(f"event 不能为空且不能超过 {_MAX_EVENT_CHARS} 个字符")
            if len(normalized_condition) > _MAX_DELETE_CONDITION_CHARS:
                return self._fail(
                    f"delete_condition 不能超过 {_MAX_DELETE_CONDITION_CHARS} 个字符"
                )

            normalized_name = self._normalize_name(qq_name)
            if not normalized_name:
                normalized_name = await self._resolve_qq_name(normalized_qq)

            async with self._lock:
                max_entries = self._cfg().max_entries
                if len(self._memories) >= max_entries:
                    return self._fail(
                        f"短期记忆已达到上限 {max_entries} 条，请先删除旧记忆"
                    )
                record_id = self._new_id_locked()
                now = self._now_iso()
                record = MemoryRecord(
                    id=record_id,
                    qq_number=normalized_qq,
                    qq_name=normalized_name,
                    event=normalized_event,
                    created_at=now,
                    updated_at=now,
                    delete_condition=normalized_condition,
                )
                self._memories[record_id] = record
                self._save_locked()

            content = f"已创建短期记忆 {record.id}：{self._format_record_fields(record)}"
            return {
                "success": True,
                "content": content,
                "message": content,
                "id": record.id,
                "memory": record.to_dict(),
            }
        except Exception as exc:
            self.ctx.logger.error("[短期记忆] 创建失败: %s", exc, exc_info=True)
            return self._fail(f"创建短期记忆失败：{exc}")

    # ─── Tool：修改 ───────────────────────────────────────────

    @Tool(
        "modify_short_term_memory",
        brief_description="修改指定编号的短期记忆，可更新 QQ 号、QQ 名和事情描述。",
        detailed_description=(
            "修改短期记忆。必须提供 id（4 位编号）、qq_number（原 QQ 或修改后的 QQ 均可）"
            "以及 event（修改后的事情描述）。qq_name 可选；QQ 号未变化且不传 qq_name 时保留原名，"
            "QQ 号变化时自动尝试解析新名字。delete_condition 可选，通常发生一次就算结束，"
            "需要你自行判断或询问用户；不传时保留原值，传空字符串可清除删除条件。只修改传入的字段。"
        ),
        parameters=[
            ToolParameterInfo(
                name="id",
                param_type=ToolParamType.STRING,
                description="要修改的短期记忆编号（4 位小写字母/数字，必填）",
                required=True,
            ),
            ToolParameterInfo(
                name="qq_number",
                param_type=ToolParamType.STRING,
                description="原 QQ 号或修改后的 QQ 号（必填，纯数字）",
                required=True,
            ),
            ToolParameterInfo(
                name="event",
                param_type=ToolParamType.STRING,
                description="修改后的事情描述（必填）",
                required=True,
            ),
            ToolParameterInfo(
                name="qq_name",
                param_type=ToolParamType.STRING,
                description="修改后的 QQ 名或群昵称（可选）",
                required=False,
            ),
            ToolParameterInfo(
                name="delete_condition",
                param_type=ToolParamType.STRING,
                description="修改后的删除条件（可选；不传保留原值，传空字符串清除）",
                required=False,
            ),
        ],
        core_tool=False,
    )
    async def modify_short_term_memory(
        self,
        id: str = "",
        qq_number: str = "",
        event: str = "",
        qq_name: str = "",
        delete_condition: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            if not self._cfg().enabled or not self._cfg().enable_tools:
                return self._fail("短期记忆工具已关闭")
            record_id = self._normalize_id(id)
            normalized_qq = self._normalize_qq(qq_number)
            normalized_event = self._normalize_event(event)
            normalized_condition: str | None = None
            if delete_condition is not None:
                normalized_condition = self._normalize_delete_condition(delete_condition)
            if not record_id:
                return self._fail("id 必须是 4 位小写字母/数字")
            if not normalized_qq:
                return self._fail("qq_number 必须是 5-15 位纯数字 QQ 号")
            if not normalized_event:
                return self._fail(f"event 不能为空且不能超过 {_MAX_EVENT_CHARS} 个字符")
            if (
                normalized_condition is not None
                and len(normalized_condition) > _MAX_DELETE_CONDITION_CHARS
            ):
                return self._fail(
                    f"delete_condition 不能超过 {_MAX_DELETE_CONDITION_CHARS} 个字符"
                )

            async with self._lock:
                current = self._memories.get(record_id)
                if current is None:
                    return self._fail(f"未找到编号为 {record_id} 的短期记忆")
                current_qq = current.qq_number
                current_name = current.qq_name

            normalized_name = self._normalize_name(qq_name)
            if not normalized_name:
                if normalized_qq == current_qq:
                    normalized_name = current_name
                else:
                    normalized_name = await self._resolve_qq_name(normalized_qq)

            async with self._lock:
                record = self._memories.get(record_id)
                if record is None:
                    return self._fail(f"未找到编号为 {record_id} 的短期记忆")
                record.qq_number = normalized_qq
                record.qq_name = normalized_name
                record.event = normalized_event
                if normalized_condition is not None:
                    record.delete_condition = normalized_condition
                record.updated_at = self._now_iso()
                self._save_locked()
                updated = MemoryRecord.from_dict(record.to_dict())

            assert updated is not None
            content = f"已修改短期记忆 {updated.id}：{self._format_record_fields(updated)}"
            return {
                "success": True,
                "content": content,
                "message": content,
                "id": updated.id,
                "memory": updated.to_dict(),
            }
        except Exception as exc:
            self.ctx.logger.error("[短期记忆] 修改失败: %s", exc, exc_info=True)
            return self._fail(f"修改短期记忆失败：{exc}")

    # ─── Tool：删除 ───────────────────────────────────────────

    @Tool(
        "delete_short_term_memory",
        brief_description="按编号删除一条短期记忆。",
        detailed_description="删除短期记忆。必须提供 id（4 位小写字母/数字编号）。删除后不可恢复。",
        parameters=[
            ToolParameterInfo(
                name="id",
                param_type=ToolParamType.STRING,
                description="要删除的短期记忆编号（4 位小写字母/数字，必填）",
                required=True,
            ),
        ],
        core_tool=False,
    )
    async def delete_short_term_memory(self, id: str = "", **kwargs: Any) -> dict[str, Any]:
        try:
            if not self._cfg().enabled or not self._cfg().enable_tools:
                return self._fail("短期记忆工具已关闭")
            record_id = self._normalize_id(id)
            if not record_id:
                return self._fail("id 必须是 4 位小写字母/数字")
            async with self._lock:
                record = self._memories.pop(record_id, None)
                if record is None:
                    return self._fail(f"未找到编号为 {record_id} 的短期记忆")
                self._save_locked()
            content = f"已删除短期记忆 {record.id}：{self._format_record_fields(record)}"
            return {
                "success": True,
                "content": content,
                "message": content,
                "id": record.id,
            }
        except Exception as exc:
            self.ctx.logger.error("[短期记忆] 删除失败: %s", exc, exc_info=True)
            return self._fail(f"删除短期记忆失败：{exc}")

    # ─── 注入与数据辅助 ───────────────────────────────────────

    def _build_injection_text(self) -> str:
        cfg = self._cfg()
        if not cfg.enabled or not self._memories:
            return ""

        records = sorted(
            self._memories.values(),
            key=lambda record: record.updated_at,
            reverse=True,
        )
        lines = [_PROMPT_HEADER]
        for record in records:
            lines.append(f"{record.id}{self._format_record_fields(record)}")
        return "\n".join(lines)

    @staticmethod
    def _format_record_fields(record: MemoryRecord) -> str:
        text = f"{record.qq_name}（{record.qq_number}）:{record.event}"
        if record.delete_condition:
            text += f"；删除条件：{record.delete_condition}"
        return text

    @staticmethod
    def _build_system_item(prompt: str) -> dict[str, Any]:
        return {
            "item_type": "SystemMessageItem",
            "meta": {
                "item_id": uuid.uuid4().hex,
                "logical_turn_id": None,
                "timestamp": datetime.now().astimezone().isoformat(),
            },
            "parts": [{"type": "text", "text": prompt}],
        }

    @property
    def _data_file(self) -> Path:
        return self.ctx.paths.data_dir / "short_term_memories.json"

    def _load_from_disk(self) -> None:
        path = self._data_file
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            items = raw.get("memories", {}) if isinstance(raw, dict) else {}
            if isinstance(items, list):
                iterable = ((str(item.get("id") or ""), item) for item in items if isinstance(item, dict))
            elif isinstance(items, dict):
                iterable = items.items()
            else:
                iterable = ()
            for key, item in iterable:
                record = MemoryRecord.from_dict(item)
                if record is None:
                    self.ctx.logger.warning("[短期记忆] 跳过无效记忆记录: %s", key)
                    continue
                self._memories[record.id] = record
        except Exception as exc:
            self.ctx.logger.error("[短期记忆] 读取数据文件失败: %s", exc, exc_info=True)

    def _save_locked(self) -> None:
        path = self._data_file
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "memories": {
                record_id: record.to_dict()
                for record_id, record in sorted(self._memories.items())
            },
        }
        temp_path = path.with_name(path.name + ".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, path)

    def _purge_expired_locked(self) -> int:
        retention_hours = self._cfg().retention_hours
        if retention_hours <= 0 or not self._memories:
            return 0
        cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
        expired: list[str] = []
        for record_id, record in self._memories.items():
            try:
                updated_at = datetime.fromisoformat(record.updated_at)
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=UTC)
            except ValueError:
                continue
            if updated_at < cutoff:
                expired.append(record_id)
        for record_id in expired:
            self._memories.pop(record_id, None)
        return len(expired)

    def _restart_cleanup_task(self) -> None:
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _stop_cleanup_task(self) -> None:
        task = self._cleanup_task
        self._cleanup_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                interval = max(60, int(self._cfg().cleanup_interval_minutes) * 60)
                await asyncio.sleep(interval)
                async with self._lock:
                    removed = self._purge_expired_locked()
                    if removed:
                        self._save_locked()
                if removed:
                    self.ctx.logger.info("[短期记忆] 已清理 %d 条过期记忆", removed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.error("[短期记忆] 清理任务异常: %s", exc, exc_info=True)

    async def _resolve_qq_name(self, qq_number: str) -> str:
        try:
            person_result = await self.ctx.person.get_id("qq", qq_number)
            if not isinstance(person_result, dict) or not person_result.get("success"):
                return "未知QQ用户"
            person_id = str(person_result.get("person_id") or "").strip()
            if not person_id:
                return "未知QQ用户"
            for field_name in ("person_name", "nickname"):
                value_result = await self.ctx.person.get_value(person_id, field_name)
                if isinstance(value_result, dict) and value_result.get("success"):
                    name = self._normalize_name(value_result.get("value"))
                    if name:
                        return name
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("[短期记忆] 自动解析 QQ 名失败: %s", exc)
        return "未知QQ用户"

    def _new_id_locked(self) -> str:
        for _ in range(100_000):
            candidate = "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))
            if candidate not in self._memories:
                return candidate
        raise RuntimeError("短期记忆编号空间已耗尽，无法生成唯一编号")

    @staticmethod
    def _normalize_id(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        return normalized if _ID_RE.fullmatch(normalized) else ""

    @staticmethod
    def _normalize_qq(value: Any) -> str:
        normalized = str(value or "").strip()
        return normalized if _QQ_RE.fullmatch(normalized) else ""

    @staticmethod
    def _normalize_name(value: Any) -> str:
        normalized = _CONTROL_RE.sub(" ", str(value or ""))
        normalized = " ".join(normalized.split()).strip()
        return normalized[:_MAX_QQ_NAME_CHARS]

    @staticmethod
    def _normalize_event(value: Any) -> str:
        normalized = _CONTROL_RE.sub(" ", str(value or ""))
        normalized = " ".join(normalized.split()).strip()
        if len(normalized) > _MAX_EVENT_CHARS:
            return ""
        return normalized

    @staticmethod
    def _normalize_delete_condition(value: Any) -> str:
        return _clean_text(value)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    def _cfg(self):
        return self.config.plugin

    @staticmethod
    def _fail(reason: str) -> dict[str, Any]:
        return {
            "success": False,
            "content": reason,
            "message": reason,
        }


def create_plugin() -> ShortTermMemoryPlugin:
    return ShortTermMemoryPlugin()
