"""运行原始载荷冷表服务（阶段 C）。

把 `strategy_runs` 里四个大 JSON 字段搬到 `strategy_run_payloads`，用 gzip
压缩后按需读取。设计约束：

* **冷表是唯一持久化位置**：运行收尾只写冷表，热表字段保持 SQL NULL；
  冷表写入失败/不完整时才保留热表副本兜底（宁可热表变大也不丢审计载荷）；
* 读取优先冷表，历史行回退旧字段，因此迁移过渡期任何时刻都不会丢数据；
* 冷表行带 `original_size / compressed_size / sha256`，可离线校验；
* 压缩只用标准库 gzip（`mtime=0` 保证同输入稳定输出，便于比对）；
* 解压失败不能炸掉接口：返回 None 并记日志，让上层回落旧字段。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.db.models import StrategyRun, StrategyRunPayload

logger = logging.getLogger(__name__)

COMPRESSION_GZIP = "gzip"
GZIP_LEVEL = 6

# 热表字段名 → 冷表 payload_type
FIELD_TO_PAYLOAD_TYPE: dict[str, str] = {
    "llm_request_payload": "llm_request",
    "llm_response_payload": "llm_response",
    "skill_payloads": "skill",
    "decision_payload": "decision",
}
PAYLOAD_TYPE_TO_FIELD: dict[str, str] = {
    value: key for key, value in FIELD_TO_PAYLOAD_TYPE.items()
}
ALL_PAYLOAD_TYPES: tuple[str, ...] = tuple(FIELD_TO_PAYLOAD_TYPE.values())


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def compress_json(payload: Any) -> tuple[bytes, str, int, int]:
    """序列化 + gzip 压缩。返回 (blob, sha256, 原始字节数, 压缩后字节数)。"""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    blob = gzip.compress(raw, compresslevel=GZIP_LEVEL, mtime=0)
    return blob, _sha256(raw), len(raw), len(blob)


def decompress_json(
    blob: bytes | None,
    *,
    compression: str | None = COMPRESSION_GZIP,
) -> Any | None:
    if blob is None:
        return None
    normalized = str(compression or COMPRESSION_GZIP).lower()
    try:
        if normalized == COMPRESSION_GZIP:
            raw = gzip.decompress(blob)
        elif normalized in {"", "none", "raw"}:
            raw = bytes(blob)
        else:
            logger.warning("unknown payload compression: %s", compression)
            return None
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("payload decompress failed: %s", exc)
        return None


class RunPayloadService:
    # -- 写入 -------------------------------------------------------------

    def store_run_payloads(self, db: Session, run: StrategyRun) -> int:
        """把 run 上内存里的载荷写入冷表，返回写入/更新的行数。

        `None` 字段跳过（不产生空行）；已存在的行按 run_id + type 更新。
        调用方（`aniu_service._persist_run_payloads`）据此判断"是否完整"——
        只有返回条数等于内存里的非空载荷数，才允许清空热表字段。
        """
        stored = 0
        for field, payload_type in FIELD_TO_PAYLOAD_TYPE.items():
            value = getattr(run, field, None)
            if value is None:
                continue
            if isinstance(value, (str, bytes)):
                # 极端情况下历史行里存的是字符串化 JSON
                try:
                    value = json.loads(value)
                except (TypeError, ValueError):
                    value = {"raw": str(value)}
            blob, digest, original_size, compressed_size = compress_json(value)
            existing = db.scalar(
                select(StrategyRunPayload).where(
                    StrategyRunPayload.run_id == run.id,
                    StrategyRunPayload.payload_type == payload_type,
                )
            )
            if existing is None:
                db.add(
                    StrategyRunPayload(
                        run_id=run.id,
                        payload_type=payload_type,
                        payload_data=blob,
                        compression=COMPRESSION_GZIP,
                        original_size=original_size,
                        compressed_size=compressed_size,
                        sha256=digest,
                    )
                )
            else:
                existing.payload_data = blob
                existing.compression = COMPRESSION_GZIP
                existing.original_size = original_size
                existing.compressed_size = compressed_size
                existing.sha256 = digest
                db.add(existing)
            stored += 1
        return stored

    # -- 读取 -------------------------------------------------------------

    def load_run_payloads(
        self,
        db: Session,
        run_id: int,
        payload_types: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """返回 {payload_type: 解压后的对象}，缺失的类型不出现在结果里。"""
        wanted = tuple(payload_types) if payload_types else ALL_PAYLOAD_TYPES
        stmt = select(StrategyRunPayload).where(
            StrategyRunPayload.run_id == run_id,
            StrategyRunPayload.payload_type.in_(wanted),
        )
        resolved: dict[str, Any] = {}
        for row in db.execute(stmt).scalars().all():
            value = decompress_json(row.payload_data, compression=row.compression)
            if value is not None:
                resolved[row.payload_type] = value
        return resolved

    def load_payloads_for_runs(
        self,
        db: Session,
        run_ids: list[int],
        payload_types: Iterable[str] = ("skill",),
    ) -> dict[int, dict[str, Any]]:
        """批量读取多个 run 的指定载荷（每日报告等聚合场景用）。"""
        if not run_ids:
            return {}
        wanted = tuple(payload_types)
        stmt = select(
            StrategyRunPayload.run_id,
            StrategyRunPayload.payload_type,
            StrategyRunPayload.payload_data,
            StrategyRunPayload.compression,
        ).where(
            StrategyRunPayload.run_id.in_(run_ids),
            StrategyRunPayload.payload_type.in_(wanted),
        )
        grouped: dict[int, dict[str, Any]] = {}
        for row in db.execute(stmt).mappings().all():
            value = decompress_json(
                row["payload_data"], compression=row["compression"]
            )
            if value is None:
                continue
            grouped.setdefault(int(row["run_id"]), {})[row["payload_type"]] = value
        return grouped

    def hydrate_run(self, db: Session, run: StrategyRun) -> bool:
        """用冷表补全热表里已被清空的大字段。返回是否有字段被补齐。

        关键：用 `set_committed_value` 而不是普通赋值。直接 setattr 会把对象
        标脏，调用方（例如 skill 路由里的 session_scope）一提交就把几 MB 的
        载荷写回 `strategy_runs`，把 clear-legacy 当场撤销、热表重新膨胀。
        """
        missing = [
            field
            for field in FIELD_TO_PAYLOAD_TYPE
            if getattr(run, field, None) is None
        ]
        if not missing:
            return False
        wanted = [FIELD_TO_PAYLOAD_TYPE[field] for field in missing]
        loaded = self.load_run_payloads(db, int(run.id), payload_types=wanted)
        if not loaded:
            return False
        hydrated = False
        for payload_type, value in loaded.items():
            field = PAYLOAD_TYPE_TO_FIELD.get(payload_type)
            if field is None:
                continue
            # 只写入会话内的对象，不产生 UPDATE
            set_committed_value(run, field, value)
            hydrated = True
        return hydrated

    # -- 生命周期 ---------------------------------------------------------

    def purge_payloads_older_than(self, db: Session, cutoff_run_id: int) -> int:
        """删除 run_id < cutoff 的冷表载荷（按 run_id 单调性代替时间比较）。

        载荷是审计用的冷数据，交易事实与运行结论不在本表，删掉不影响
        交易追溯；需要长期留存时可调大保留窗口。
        """
        result = db.execute(
            delete(StrategyRunPayload).where(StrategyRunPayload.run_id < cutoff_run_id)
        )
        return int(result.rowcount or 0)


run_payload_service = RunPayloadService()
