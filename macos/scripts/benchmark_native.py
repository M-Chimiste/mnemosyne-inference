#!/usr/bin/env python3
"""Content-redacted benchmark for Unified Inference and compatible endpoints."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import statistics
import time
from typing import Any

import httpx


PROMPT = (
    "Explain why a bounded work queue improves the stability of a local "
    "inference server. Use three concise points."
)


@dataclass(frozen=True)
class Sample:
    status_code: int
    headers_ms: float
    first_token_ms: float | None
    total_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None
    output_tokens_per_second: float | None
    error_code: str | None = None


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return round(ordered[index], 3)


def summarize(samples: list[Sample]) -> dict[str, Any]:
    successful = [sample for sample in samples if sample.error_code is None]
    ttft = [
        sample.first_token_ms
        for sample in successful
        if sample.first_token_ms is not None
    ]
    totals = [sample.total_ms for sample in successful]
    rates = [
        sample.output_tokens_per_second
        for sample in successful
        if sample.output_tokens_per_second is not None
    ]
    return {
        "requests": len(samples),
        "successful": len(successful),
        "errors": len(samples) - len(successful),
        "ttft_ms": {
            "average": round(statistics.fmean(ttft), 3) if ttft else None,
            "p50": percentile(ttft, 0.50),
            "p95": percentile(ttft, 0.95),
        },
        "total_ms": {
            "average": round(statistics.fmean(totals), 3) if totals else None,
            "p50": percentile(totals, 0.50),
            "p95": percentile(totals, 0.95),
        },
        "output_tokens_per_second": {
            "average": round(statistics.fmean(rates), 3) if rates else None,
            "p50": percentile(rates, 0.50),
            "p95": percentile(rates, 0.95),
        },
    }


def _usage_from_event(event: dict[str, Any]) -> tuple[int | None, int | None]:
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return None, None
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    return (
        prompt if isinstance(prompt, int) and not isinstance(prompt, bool) else None,
        completion
        if isinstance(completion, int) and not isinstance(completion, bool)
        else None,
    )


async def run_sample(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    model: str,
    headers: dict[str, str],
    max_tokens: int,
) -> Sample:
    started = time.perf_counter()
    first_token_at: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    status_code = 0
    headers_at = started
    try:
        async with client.stream(
            "POST",
            f"{base_url.rstrip('/')}/v1/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": [{"role": "user", "content": PROMPT}],
                "temperature": 0,
                "max_tokens": max_tokens,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        ) as response:
            status_code = response.status_code
            headers_at = time.perf_counter()
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_prompt, event_completion = _usage_from_event(event)
                prompt_tokens = event_prompt or prompt_tokens
                completion_tokens = event_completion or completion_tokens
                choices = event.get("choices")
                if not isinstance(choices, list):
                    continue
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta")
                    text = delta.get("content") if isinstance(delta, dict) else None
                    if text and first_token_at is None:
                        first_token_at = time.perf_counter()
        ended = time.perf_counter()
        decode_seconds = (
            ended - first_token_at if first_token_at is not None else None
        )
        rate = (
            completion_tokens / decode_seconds
            if completion_tokens is not None
            and decode_seconds is not None
            and decode_seconds > 0
            else None
        )
        return Sample(
            status_code=status_code,
            headers_ms=round((headers_at - started) * 1000, 3),
            first_token_ms=round((first_token_at - started) * 1000, 3)
            if first_token_at is not None
            else None,
            total_ms=round((ended - started) * 1000, 3),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            output_tokens_per_second=round(rate, 3) if rate is not None else None,
        )
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        ended = time.perf_counter()
        return Sample(
            status_code=status_code,
            headers_ms=round((headers_at - started) * 1000, 3),
            first_token_ms=None,
            total_ms=round((ended - started) * 1000, 3),
            prompt_tokens=None,
            completion_tokens=None,
            output_tokens_per_second=None,
            error_code=type(exc).__name__,
        )


async def benchmark_target(
    *,
    label: str,
    base_url: str,
    model: str,
    api_key: str,
    requests: int,
    concurrency: int,
    warmups: int,
    max_tokens: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    limits = httpx.Limits(
        max_connections=max(1, concurrency),
        max_keepalive_connections=max(1, concurrency),
    )
    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        trust_env=False,
        follow_redirects=False,
        limits=limits,
    ) as client:
        for _ in range(warmups):
            await run_sample(
                client,
                base_url=base_url,
                model=model,
                headers=headers,
                max_tokens=max_tokens,
            )

        semaphore = asyncio.Semaphore(concurrency)

        async def bounded_sample() -> Sample:
            async with semaphore:
                return await run_sample(
                    client,
                    base_url=base_url,
                    model=model,
                    headers=headers,
                    max_tokens=max_tokens,
                )

        started = time.perf_counter()
        samples = await asyncio.gather(
            *(bounded_sample() for _ in range(requests))
        )
        wall_seconds = time.perf_counter() - started
    result = {
        "label": label,
        "base_url": base_url,
        "model": model,
        "concurrency": concurrency,
        "warmups": warmups,
        "wall_seconds": round(wall_seconds, 3),
        "request_throughput_per_second": round(requests / wall_seconds, 3)
        if wall_seconds > 0
        else None,
        "summary": summarize(samples),
        "samples": [asdict(sample) for sample in samples],
    }
    return result


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Measure warm TTFT, total latency, throughput, and concurrency without "
            "recording prompt or response content."
        )
    )
    value.add_argument("--model", required=True)
    value.add_argument("--base-url", default="http://127.0.0.1:1240")
    value.add_argument("--label", default="unified-inference")
    value.add_argument("--api-key-env", default="INFERENCE_API_KEY")
    value.add_argument("--requests", type=int, default=5)
    value.add_argument("--concurrency", type=int, default=1)
    value.add_argument("--warmups", type=int, default=1)
    value.add_argument("--max-tokens", type=int, default=128)
    value.add_argument("--timeout-seconds", type=float, default=900)
    value.add_argument("--compare-base-url")
    value.add_argument("--compare-model")
    value.add_argument("--compare-label", default="comparison")
    value.add_argument("--compare-api-key-env", default="")
    value.add_argument("--output", type=Path)
    return value


async def async_main(args: argparse.Namespace) -> dict[str, Any]:
    if args.requests < 1 or args.concurrency < 1 or args.warmups < 0:
        raise SystemExit("requests/concurrency must be positive and warmups non-negative")
    if args.max_tokens < 1 or args.timeout_seconds <= 0:
        raise SystemExit("max-tokens and timeout-seconds must be positive")
    targets = [
        await benchmark_target(
            label=args.label,
            base_url=args.base_url,
            model=args.model,
            api_key=os.environ.get(args.api_key_env, "") if args.api_key_env else "",
            requests=args.requests,
            concurrency=args.concurrency,
            warmups=args.warmups,
            max_tokens=args.max_tokens,
            timeout_seconds=args.timeout_seconds,
        )
    ]
    if args.compare_base_url:
        targets.append(
            await benchmark_target(
                label=args.compare_label,
                base_url=args.compare_base_url,
                model=args.compare_model or args.model,
                api_key=(
                    os.environ.get(args.compare_api_key_env, "")
                    if args.compare_api_key_env
                    else ""
                ),
                requests=args.requests,
                concurrency=args.concurrency,
                warmups=args.warmups,
                max_tokens=args.max_tokens,
                timeout_seconds=args.timeout_seconds,
            )
        )
    return {
        "schema_version": 1,
        "content_recorded": False,
        "generated_at": time.time(),
        "targets": targets,
    }


def main() -> None:
    args = parser().parse_args()
    report = asyncio.run(async_main(args))
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
