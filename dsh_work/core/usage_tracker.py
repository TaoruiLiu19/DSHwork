"""用量与消耗追踪核心模块。

移植自 dsh-usage-plugin 的核心逻辑，适配 PySide6 桌面端：
  - 记录每次模型调用的完整 token 维度（input/cacheHit/cacheWrite/output/reasoning/finishReason）
  - 支持峰谷计费（高峰：北京时间 9:00–12:00, 14:00–18:00）
  - 2026-08-17 自动切换基础价→峰谷价（插件 EFFECTIVE_AT）
  - 价格表可编辑，持久化 pricing.json
  - 记录持久化 usage-records.json，上限 10 万条
  - 日历统计聚合、记录筛选、CSV/JSON 导出

与插件的区别：
  - 插件是 Cordis Host 插件（监听 llm/stream middleware），我们通过 WS 的 TOKEN_USAGE + TURN_END 事件记录
  - 余额查询沿用 DSH Work 的双通道容错设计（balance_client.py），不使用插件的单通道
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from ..utils.logger import get_logger

log = get_logger("core.usage_tracker")

# ===== 价格表（与插件 PRICING 常量一致）=====
DEFAULT_PRICING: dict = {
    "base": {
        "deepseek-v4-flash": {"cacheHit": 0.02, "cacheMiss": 1.0, "output": 2.0},
        "deepseek-v4-pro":   {"cacheHit": 0.025, "cacheMiss": 3.0, "output": 6.0},
    },
    "peakValley": {
        "deepseek-v4-flash": {
            "offPeak": {"cacheHit": 0.05, "cacheMiss": 1.5, "output": 4.5},
            "peak":    {"cacheHit": 0.10, "cacheMiss": 3.0, "output": 9.0},
        },
        "deepseek-v4-pro": {
            "offPeak": {"cacheHit": 0.15, "cacheMiss": 4.5,  "output": 13.5},
            "peak":    {"cacheHit": 0.30, "cacheMiss": 9.0,  "output": 27.0},
        },
    },
}
PRICE_MODELS = ["deepseek-v4-flash", "deepseek-v4-pro"]
# 新价格表（峰谷价）生效时间：北京时间 2026-08-17 00:00
EFFECTIVE_AT_MS = int(time.mktime(time.strptime("2026-08-17 00:00:00", "%Y-%m-%d %H:%M:%S"))) * 1000 + 8 * 3600 * 1000
# 实际 EFFECTIVE_AT 用 UTC 毫秒：2026-08-16T16:00:00Z = 北京时间 2026-08-17 00:00
EFFECTIVE_AT_MS = 1792224000000  # 2026-08-17 00:00:00 +08:00 的 UTC 毫秒数

MAX_RECORDS = 100000
USAGE_DIRNAME = "dsh-usage"
USAGE_RECORDS_FILENAME = "usage-records.json"
PRICING_FILENAME = "pricing.json"

# 延迟写入配置（批量持久化，避免每次 add_record 都刷盘）
_PERSIST_DELAY_MS = 2000  # 2 秒内的批量写入合并
_PERSIST_MAX_BATCH = 50   # 超过 50 条待写入时立即刷盘


def _model_key(model: str) -> str:
    """将模型名标准化为 price key（与插件 modelKey 一致）。"""
    m = (model or "").lower()
    if "flash" in m:
        return "deepseek-v4-flash"
    if "pro" in m:
        return "deepseek-v4-pro"
    return "unknown"


def _is_peak(ts_ms: int) -> bool:
    """判断北京时间是否为高峰时段：9:00-12:00、14:00-18:00。"""
    # ts_ms 是 UTC 毫秒，+8h 转北京时间
    bj_ts = ts_ms + 8 * 3600 * 1000
    d = time.gmtime(bj_ts / 1000)
    minutes = d.tm_hour * 60 + d.tm_min
    return (9 * 60 <= minutes < 12 * 60) or (14 * 60 <= minutes < 18 * 60)


PricingRegime = Literal["base", "peakValley", "auto"]


def _cost_for(rec: UsageRecord, regime: PricingRegime, pricing: dict) -> float:
    """按指定计价模式计算单次调用消耗（单位：元）。与插件 costFor 完全一致。"""
    mk = _model_key(rec.model)
    hit = rec.cache_read_tokens
    miss = rec.input_tokens
    out = rec.output_tokens

    if regime == "base":
        p = pricing["base"].get(mk)
        if not p:
            return 0.0
        return (hit * p["cacheHit"] + miss * p["cacheMiss"] + out * p["output"]) / 1e6

    if regime == "auto":
        if rec.time < EFFECTIVE_AT_MS:
            p = pricing["base"].get(mk)
            if not p:
                return 0.0
            return (hit * p["cacheHit"] + miss * p["cacheMiss"] + out * p["output"]) / 1e6
        pv = pricing["peakValley"].get(mk)
        if not pv:
            return 0.0
        tier = pv["peak"] if _is_peak(rec.time) else pv["offPeak"]
        return (hit * tier["cacheHit"] + miss * tier["cacheMiss"] + out * tier["output"]) / 1e6

    # regime == "peakValley"
    pv = pricing["peakValley"].get(mk)
    if not pv:
        return 0.0
    tier = pv["peak"] if _is_peak(rec.time) else pv["offPeak"]
    return (hit * tier["cacheHit"] + miss * tier["cacheMiss"] + out * tier["output"]) / 1e6


@dataclass
class UsageRecord:
    """单条用量记录（对应插件 usage-records.json 的元素）。"""
    time: int                    # UTC 毫秒时间戳
    model: str = ""
    provider: str = ""
    purpose: str = ""
    input_tokens: int = 0            # 输入未命中 token（cache miss）
    output_tokens: int = 0           # 输出 token
    cache_read_tokens: int = 0       # 缓存读取 token（cache hit）
    cache_write_tokens: int = 0      # 缓存写入 token
    reasoning_tokens: int = 0        # 推理 token
    finish_reason: str = ""          # 结束原因

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> UsageRecord | None:
        try:
            ts = int(d.get("time", 0))
            if ts <= 0:
                return None
            return cls(
                time=ts,
                model=str(d.get("model", "")),
                provider=str(d.get("provider", "")),
                purpose=str(d.get("purpose", "")),
                input_tokens=int(d.get("input_tokens", d.get("inputTokens", 0))),
                output_tokens=int(d.get("output_tokens", d.get("outputTokens", 0))),
                cache_read_tokens=int(d.get("cache_read_tokens", d.get("cacheReadTokens", 0))),
                cache_write_tokens=int(d.get("cache_write_tokens", d.get("cacheWriteTokens", 0))),
                reasoning_tokens=int(d.get("reasoning_tokens", d.get("reasoningTokens", 0))),
                finish_reason=str(d.get("finish_reason", d.get("finishReason", ""))),
            )
        except (ValueError, TypeError):
            return None


@dataclass
class DayAggregate:
    """每日聚合（用于日历热力图）。"""
    day: str                     # YYYY-MM-DD 北京时间
    calls: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    peak_calls: int = 0
    off_peak_calls: int = 0
    base_cost: float = 0.0
    peak_valley_cost: float = 0.0
    auto_cost: float = 0.0


def _bj_day_key(ts_ms: int) -> str:
    """UTC 毫秒 → 北京时间日期字符串 YYYY-MM-DD。"""
    bj_ts = ts_ms + 8 * 3600 * 1000
    t = time.gmtime(bj_ts / 1000)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


class UsageTracker:
    """用量追踪器：记录、持久化、统计聚合、导出。

    线程安全：所有写操作通过 _lock 保护。
    性能优化：
      - _time_index: set[int] 去重 O(1)，替代 any() 线性扫描
      - 延迟写入：add_record 先落内存，批量/定时刷盘
      - 聚合缓存：_daily_cache + _cache_version，records 不变时直接复用
    """

    def __init__(self, data_root: Path | None = None):
        """
        Args:
            data_root: 数据根目录（默认 ~/.dsh-work/，与 config 目录同级）
        """
        if data_root is None:
            home = Path(os.path.expanduser("~"))
            data_root = home / ".dsh-work"
        self._data_root = Path(data_root)
        self._usage_dir = self._data_root / USAGE_DIRNAME
        self._records_path = self._usage_dir / USAGE_RECORDS_FILENAME
        self._pricing_path = self._usage_dir / PRICING_FILENAME

        self._lock = threading.RLock()
        self._records: list[UsageRecord] = []
        self._time_index: set[int] = set()  # 去重索引 O(1)
        self._pricing: dict = json.loads(json.dumps(DEFAULT_PRICING))  # deep copy
        self._listeners: list[Callable[[], None]] = []

        # 聚合缓存（数据版本号未变化时直接返回）
        self._cache_version: int = 0
        self._daily_cache: dict[str, DayAggregate] | None = None
        self._daily_cache_version: int = -1  # 独立整数版本戳，避免给 dict 动态挂属性

        # 延迟写入（合并小批量写入，减少 JSON 序列化和 IO）
        self._dirty_count: int = 0
        self._persist_timer: threading.Timer | None = None

        self._init_dirs()
        self._load_pricing()
        self._load_records()

        log.info(
            "UsageTracker 初始化完成: records=%d, dir=%s",
            len(self._records), self._usage_dir,
        )

    # ===== 持久化基础 =====

    def _init_dirs(self) -> None:
        try:
            self._usage_dir.mkdir(parents=True, exist_ok=True)
            (self._usage_dir / "csv").mkdir(exist_ok=True)
            (self._usage_dir / "json").mkdir(exist_ok=True)
            (self._usage_dir / "images").mkdir(exist_ok=True)
        except OSError as e:
            log.warning("创建用量数据目录失败: %s", e)

    def _load_pricing(self) -> None:
        if not self._pricing_path.exists():
            self._save_pricing()
            return
        try:
            data = json.loads(self._pricing_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            for regime in ("base", "peakValley"):
                src = data.get(regime)
                dst = self._pricing.get(regime)
                if not isinstance(src, dict) or not isinstance(dst, dict):
                    continue
                for mk in PRICE_MODELS:
                    row = src.get(mk)
                    dst_row = dst.get(mk)
                    if not isinstance(row, dict) or not isinstance(dst_row, dict):
                        continue
                    if regime == "peakValley":
                        for tier in ("peak", "offPeak"):
                            src_tier = row.get(tier)
                            dst_tier = dst_row.get(tier)
                            if not isinstance(src_tier, dict) or not isinstance(dst_tier, dict):
                                continue
                            for k in ("cacheHit", "cacheMiss", "output"):
                                v = src_tier.get(k)
                                if isinstance(v, (int, float)) and v >= 0:
                                    dst_tier[k] = float(v)
                    else:
                        for k in ("cacheHit", "cacheMiss", "output"):
                            v = row.get(k)
                            if isinstance(v, (int, float)) and v >= 0:
                                dst_row[k] = float(v)
        except (OSError, json.JSONDecodeError) as e:
            log.warning("读取 pricing.json 失败，使用默认价格表: %s", e)

    def _save_pricing(self) -> None:
        try:
            self._pricing_path.write_text(
                json.dumps(self._pricing, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("写入 pricing.json 失败: %s", e)

    def _load_records(self) -> None:
        if not self._records_path.exists():
            return
        try:
            data = json.loads(self._records_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return
            seen: set[int] = set()
            loaded: list[UsageRecord] = []
            for raw in data:
                rec = UsageRecord.from_dict(raw)
                if rec is None or rec.time in seen:
                    continue
                seen.add(rec.time)
                loaded.append(rec)
            loaded.sort(key=lambda r: r.time)
            if len(loaded) > MAX_RECORDS:
                loaded = loaded[-MAX_RECORDS:]
            self._records = loaded
            self._time_index = seen  # 复用去重集合，无需再构建
            self._cache_version += 1
            log.info("加载用量记录 %d 条", len(loaded))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("读取 usage-records.json 失败: %s", e)

    def _schedule_persist(self) -> None:
        """调度延迟写入：合并短时间内的多次写入。"""
        if self._persist_timer is not None:
            return  # 已排期
        def _tick():
            with self._lock:
                self._persist_timer = None
                if self._dirty_count > 0:
                    self._save_records_locked()
        self._persist_timer = threading.Timer(_PERSIST_DELAY_MS / 1000.0, _tick)
        self._persist_timer.daemon = True
        self._persist_timer.start()

    def _force_persist(self) -> None:
        """立即刷盘（用于 import 等需要持久化的场景）。"""
        with self._lock:
            if self._persist_timer is not None:
                self._persist_timer.cancel()
                self._persist_timer = None
            if self._dirty_count > 0:
                self._save_records_locked()

    def _save_records_locked(self) -> None:
        """已持有锁时内部调用的保存逻辑。"""
        try:
            self._records_path.write_text(
                json.dumps([r.to_dict() for r in self._records], ensure_ascii=False),
                encoding="utf-8",
            )
            self._dirty_count = 0
        except OSError as e:
            log.warning("写入 usage-records.json 失败: %s", e)

    def _save_records(self) -> None:
        """公共保存入口（加锁）。"""
        with self._lock:
            self._save_records_locked()

    # ===== 监听（UI 刷新用）=====

    def add_listener(self, cb: Callable[[], None]) -> None:
        with self._lock:
            if cb not in self._listeners:
                self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[], None]) -> None:
        with self._lock:
            if cb in self._listeners:
                self._listeners.remove(cb)

    def _notify(self) -> None:
        listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb()
            except Exception as e:
                log.warning("usage listener 异常: %s", e)

    # ===== 记录 API =====

    def add_record(
        self,
        *,
        time_ms: int | None = None,
        model: str = "",
        provider: str = "",
        purpose: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        finish_reason: str = "",
    ) -> UsageRecord:
        """新增一条用量记录。如果 time_ms 已存在则忽略（去重）。

        性能优化：
          - 使用 _time_index (set) 做 O(1) 去重（原 any() 线性扫描 O(n)）
          - 延迟写入：短时间内的多条记录合并为一次磁盘 IO
        """
        with self._lock:
            ts = time_ms if time_ms and time_ms > 0 else int(time.time() * 1000)
            # 防重复：同一毫秒视为同一条 —— 用 set 索引 O(1)
            while ts in self._time_index:
                ts += 1
            rec = UsageRecord(
                time=ts,
                model=model,
                provider=provider,
                purpose=purpose,
                input_tokens=int(input_tokens),
                output_tokens=int(output_tokens),
                cache_read_tokens=int(cache_read_tokens),
                cache_write_tokens=int(cache_write_tokens),
                reasoning_tokens=int(reasoning_tokens),
                finish_reason=finish_reason,
            )
            self._records.append(rec)
            self._time_index.add(ts)
            # LRU 淘汰：超过 MAX_RECORDS 时移除最旧的一条
            if len(self._records) > MAX_RECORDS:
                old = self._records.pop(0)
                self._time_index.discard(old.time)
            # 失效聚合缓存
            self._cache_version += 1
            # 延迟持久化
            self._dirty_count += 1
            if self._dirty_count >= _PERSIST_MAX_BATCH:
                self._save_records_locked()
            else:
                self._schedule_persist()
        self._notify()
        return rec

    def get_records(
        self,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        regime: PricingRegime = "auto",
    ) -> list[UsageRecord]:
        """获取记录，可按时间区间过滤。返回新列表（时间倒序：最新在前）。"""
        with self._lock:
            recs = [r for r in self._records]
        if start_ms is not None:
            recs = [r for r in recs if r.time >= start_ms]
        if end_ms is not None:
            recs = [r for r in recs if r.time <= end_ms]
        recs.sort(key=lambda r: r.time, reverse=True)
        return recs

    def cost_of(self, rec: UsageRecord, regime: PricingRegime = "auto") -> float:
        return _cost_for(rec, regime, self._pricing)

    # ===== 统计聚合 =====

    def get_daily_aggregates(self, regime: PricingRegime = "auto") -> dict[str, DayAggregate]:
        """按北京时间日期聚合，返回 {day_key: DayAggregate}。

        性能优化：
          - 若 regime=="auto" 且 _cache_version 未变化，直接返回缓存（避免 UI 每次刷新重算）
          - 单次遍历计算三个 regime 的 cost（原先对每条记录做 3 次 _cost_for 重复查表）
        """
        # 仅 "auto" 模式走缓存（base / peakValley 用户极少调用）
        use_cache = (regime == "auto")
        if use_cache and self._daily_cache is not None:
            with self._lock:
                if self._cache_version == self._daily_cache_version:
                    return self._daily_cache

        days: dict[str, DayAggregate] = {}
        with self._lock:
            records = list(self._records)
            pricing = self._pricing

        # 预先取 pricing 各层级，避免内层循环反复 dict.get
        base_p = pricing.get("base", {})
        pv_p = pricing.get("peakValley", {})
        model_cache: dict[str, tuple] = {}  # mk -> (base_row, pv_row) or (None, None)

        for r in records:
            key = _bj_day_key(r.time)
            d = days.get(key)
            if d is None:
                d = DayAggregate(day=key)
                days[key] = d
            d.calls += 1
            d.input_tokens += r.input_tokens
            d.cache_read_tokens += r.cache_read_tokens
            d.cache_write_tokens += r.cache_write_tokens
            d.output_tokens += r.output_tokens
            d.reasoning_tokens += r.reasoning_tokens

            peak = _is_peak(r.time)
            if peak:
                d.peak_calls += 1
            else:
                d.off_peak_calls += 1

            # cost 计算：内联 _cost_for 逻辑 + 缓存 model key 查表结果
            mk = _model_key(r.model)
            if mk not in model_cache:
                model_cache[mk] = (base_p.get(mk), pv_p.get(mk))
            base_row, pv_row = model_cache[mk]
            hit = r.cache_read_tokens
            miss = r.input_tokens
            out = r.output_tokens

            base_c = 0.0
            if base_row:
                base_c = (hit * base_row["cacheHit"] + miss * base_row["cacheMiss"] + out * base_row["output"]) / 1e6
            d.base_cost += base_c

            pv_c = 0.0
            if pv_row:
                tier = pv_row["peak"] if peak else pv_row["offPeak"]
                pv_c = (hit * tier["cacheHit"] + miss * tier["cacheMiss"] + out * tier["output"]) / 1e6
            d.peak_valley_cost += pv_c

            if regime == "base":
                d.auto_cost += base_c
            elif regime == "peakValley":
                d.auto_cost += pv_c
            else:  # auto
                if r.time < EFFECTIVE_AT_MS:
                    d.auto_cost += base_c
                else:
                    d.auto_cost += pv_c

        if use_cache:
            self._daily_cache = days
            self._daily_cache_version = self._cache_version
        return days

    def get_summary(
        self,
        regime: PricingRegime = "auto",
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> dict[str, Any]:
        """全量汇总：总调用、总 token、总消耗。

        性能优化：
          - 单次遍历同时累加所有字段（原代码对同一列表做 7+ 次独立遍历）
        """
        recs = self.get_records(start_ms=start_ms, end_ms=end_ms, regime=regime)
        total_calls = len(recs)
        total_input = 0
        total_cache_hit = 0
        total_cache_write = 0
        total_output = 0
        total_reasoning = 0
        total_cost = 0.0
        peak_calls = 0

        # 内联成本计算，减少函数调用 + 重复查表
        pricing = self._pricing
        base_p = pricing.get("base", {})
        pv_p = pricing.get("peakValley", {})
        model_cache: dict[str, tuple] = {}

        for r in recs:
            total_input += r.input_tokens
            total_cache_hit += r.cache_read_tokens
            total_cache_write += r.cache_write_tokens
            total_output += r.output_tokens
            total_reasoning += r.reasoning_tokens
            peak = _is_peak(r.time)
            if peak:
                peak_calls += 1

            mk = _model_key(r.model)
            if mk not in model_cache:
                model_cache[mk] = (base_p.get(mk), pv_p.get(mk))
            base_row, pv_row = model_cache[mk]
            hit = r.cache_read_tokens
            miss = r.input_tokens
            out = r.output_tokens

            if regime == "base" and base_row:
                total_cost += (hit * base_row["cacheHit"] + miss * base_row["cacheMiss"] + out * base_row["output"]) / 1e6
            elif regime == "peakValley" and pv_row:
                tier = pv_row["peak"] if peak else pv_row["offPeak"]
                total_cost += (hit * tier["cacheHit"] + miss * tier["cacheMiss"] + out * tier["output"]) / 1e6
            else:  # auto
                if r.time < EFFECTIVE_AT_MS:
                    if base_row:
                        total_cost += (hit * base_row["cacheHit"] + miss * base_row["cacheMiss"] + out * base_row["output"]) / 1e6
                elif pv_row:
                    tier = pv_row["peak"] if peak else pv_row["offPeak"]
                    total_cost += (hit * tier["cacheHit"] + miss * tier["cacheMiss"] + out * tier["output"]) / 1e6

        return {
            "total_calls": total_calls,
            "total_input_tokens": total_input,
            "total_cache_read_tokens": total_cache_hit,
            "total_cache_write_tokens": total_cache_write,
            "total_output_tokens": total_output,
            "total_reasoning_tokens": total_reasoning,
            "total_cost": total_cost,
            "peak_calls": peak_calls,
            "off_peak_calls": total_calls - peak_calls,
        }

    # ===== 价格表 =====

    @property
    def pricing(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._pricing))

    def update_pricing(self, new_pricing: dict) -> bool:
        """更新价格表并持久化。返回 True 表示成功。"""
        try:
            # 校验格式
            for regime in ("base", "peakValley"):
                if regime not in new_pricing or not isinstance(new_pricing[regime], dict):
                    return False
                for mk in PRICE_MODELS:
                    if mk not in new_pricing[regime]:
                        continue
                    row = new_pricing[regime][mk]
                    if not isinstance(row, dict):
                        return False
                    if regime == "peakValley":
                        for tier in ("peak", "offPeak"):
                            if tier not in row:
                                return False
                            for k in ("cacheHit", "cacheMiss", "output"):
                                v = row[tier].get(k)
                                if not isinstance(v, (int, float)) or v < 0:
                                    return False
                    else:
                        for k in ("cacheHit", "cacheMiss", "output"):
                            v = row.get(k)
                            if not isinstance(v, (int, float)) or v < 0:
                                return False
        except Exception:
            return False

        with self._lock:
            self._pricing = json.loads(json.dumps(new_pricing))
            # 价格变更影响 cost，失效所有聚合缓存
            self._cache_version += 1
            self._daily_cache = None
            self._save_pricing()
        self._notify()
        return True

    def reset_pricing(self) -> None:
        with self._lock:
            self._pricing = json.loads(json.dumps(DEFAULT_PRICING))
            self._cache_version += 1
            self._daily_cache = None
            self._save_pricing()
        self._notify()

    # ===== 导入导出 =====

    def export_json(self, path: str | os.PathLike) -> int:
        """导出全部记录为 JSON，返回导出条数。"""
        recs = self.get_records()
        data = [r.to_dict() for r in recs]
        Path(path).write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return len(data)

    def export_csv(self, path: str | os.PathLike) -> int:
        """导出全部记录为 CSV，返回导出条数。"""
        recs = self.get_records()
        headers = [
            "time", "datetime_bj", "model", "provider", "purpose",
            "input_tokens", "cache_read_tokens", "cache_write_tokens",
            "output_tokens", "reasoning_tokens", "finish_reason",
            "cost_base", "cost_peak_valley", "cost_auto",
        ]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for r in recs:
                bj = time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.gmtime((r.time + 8 * 3600 * 1000) / 1000),
                )
                writer.writerow([
                    r.time, bj, r.model, r.provider, r.purpose,
                    r.input_tokens, r.cache_read_tokens, r.cache_write_tokens,
                    r.output_tokens, r.reasoning_tokens, r.finish_reason,
                    f"{_cost_for(r, 'base', self._pricing):.6f}",
                    f"{_cost_for(r, 'peakValley', self._pricing):.6f}",
                    f"{_cost_for(r, 'auto', self._pricing):.6f}",
                ])
        return len(recs)

    def import_json(self, path: str | os.PathLike) -> tuple[int, int]:
        """从 JSON 导入记录，按 time 去重。返回 (新增, 跳过)。"""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("导入 JSON 失败: %s", e)
            return 0, 0
        if not isinstance(data, list):
            return 0, 0
        added = skipped = 0
        with self._lock:
            existing = self._time_index
            for raw in data:
                rec = UsageRecord.from_dict(raw)
                if rec is None or rec.time in existing:
                    skipped += 1
                    continue
                existing.add(rec.time)
                self._records.append(rec)
                added += 1
            self._records.sort(key=lambda r: r.time)
            if len(self._records) > MAX_RECORDS:
                overflow = len(self._records) - MAX_RECORDS
                # 从索引中移除被淘汰的记录
                for old in self._records[:overflow]:
                    existing.discard(old.time)
                self._records = self._records[-MAX_RECORDS:]
                added = max(0, added - overflow)
            self._time_index = existing
            self._cache_version += 1
            self._daily_cache = None
            # 导入是明确的用户操作，立即持久化
            self._dirty_count += added
            if self._persist_timer is not None:
                self._persist_timer.cancel()
                self._persist_timer = None
            self._save_records_locked()
        if added:
            self._notify()
        return added, skipped

    def import_csv(self, path: str | os.PathLike) -> tuple[int, int]:
        """从 CSV 导入记录，按 time 去重。CSV 首行为表头，需包含 time 列。"""
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except (OSError, csv.Error) as e:
            log.warning("导入 CSV 失败: %s", e)
            return 0, 0
        added = skipped = 0
        with self._lock:
            existing = self._time_index
            for row in rows:
                rec = UsageRecord.from_dict(row)
                if rec is None or rec.time in existing:
                    skipped += 1
                    continue
                existing.add(rec.time)
                self._records.append(rec)
                added += 1
            self._records.sort(key=lambda r: r.time)
            if len(self._records) > MAX_RECORDS:
                overflow = len(self._records) - MAX_RECORDS
                for old in self._records[:overflow]:
                    existing.discard(old.time)
                self._records = self._records[-MAX_RECORDS:]
                added = max(0, added - overflow)
            self._time_index = existing
            self._cache_version += 1
            self._daily_cache = None
            self._dirty_count += added
            if self._persist_timer is not None:
                self._persist_timer.cancel()
                self._persist_timer = None
            self._save_records_locked()
        if added:
            self._notify()
        return added, skipped

    # ===== 便捷查询 =====

    @property
    def data_dir(self) -> Path:
        return self._usage_dir

    @property
    def total_records(self) -> int:
        with self._lock:
            return len(self._records)
