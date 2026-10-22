"""usage 计费记账：按调用方提供的粒度追加一行 JSONL（本地 out/usage.jsonl，不入库）。

current Agents SDK 调用方每 run 写一行聚合 usage；历史 RelayModelAdapter 按成功返回的请求写行。
费用 = 报告的 prompt/completion tokens × config.PRICING 单价；usage 缺失或
模型无单价时费用记 null（不估算），并在 note 说明。
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from . import config

DEFAULT_LEDGER_PATH = Path(__file__).resolve().parent / 'out' / 'usage.jsonl'


class UsageLedger:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else DEFAULT_LEDGER_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, *, purpose: str, model: str, usage: dict | None,
               elapsed_ms: int | None = None, finish_reason: str | None = None,
               attempts: int = 1, empty_content_events: int = 0,
               note: str | None = None) -> dict:
        pricing = None if model in config.UNKNOWN_COST_MODELS else config.PRICING.get(model)
        prompt_tokens = (usage or {}).get('prompt_tokens')
        completion_tokens = (usage or {}).get('completion_tokens')
        total_tokens = (usage or {}).get('total_tokens')
        cached_tokens = ((usage or {}).get('prompt_tokens_details') or {}).get('cached_tokens')
        cost = None
        if prompt_tokens is not None and completion_tokens is not None and pricing is not None:
            cached = cached_tokens if isinstance(cached_tokens, (int, float)) else 0
            uncached = max(prompt_tokens - cached, 0)
            cost = round(uncached / 1e6 * pricing.input_per_mtok
                         + cached / 1e6 * pricing.cache_read_per_mtok
                         + completion_tokens / 1e6 * pricing.output_per_mtok, 6)
        line = {
            'ts_utc': _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'purpose': purpose,
            'model': model,
            'prompt_tokens': prompt_tokens,
            'cached_tokens': cached_tokens if isinstance(cached_tokens, (int, float)) else 0,
            'completion_tokens': completion_tokens,
            'total_tokens': total_tokens,
            'usage_details': (usage or {}).get('completion_tokens_details'),
            'price_in_per_mtok_cny': pricing.input_per_mtok if pricing else None,
            'price_out_per_mtok_cny': pricing.output_per_mtok if pricing else None,
            'cost_cny': cost,
            'elapsed_ms': elapsed_ms,
            'finish_reason': finish_reason,
            'attempts': attempts,
            'empty_content_events': empty_content_events,
            'note': note,
        }
        with self.path.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + '\n')
        return line
