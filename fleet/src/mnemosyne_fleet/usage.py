from __future__ import annotations

from typing import Any

import psycopg


class UsageReader:
    """Read-only aggregate view over the existing central token ledger."""

    def __init__(self, dsn: str | None) -> None:
        self._dsn = dsn

    @property
    def configured(self) -> bool:
        return self._dsn is not None

    async def aggregate(self, *, hours: int) -> list[dict[str, Any]]:
        if self._dsn is None:
            return []
        bounded_hours = max(1, min(int(hours), 720))
        conn = await psycopg.AsyncConnection.connect(
            self._dsn,
            connect_timeout=5,
            autocommit=False,
        )
        try:
            async with conn.transaction():
                await conn.execute("SET TRANSACTION READ ONLY")
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        SELECT node_id,
                               model,
                               count(*)::bigint AS request_count,
                               coalesce(sum(prompt_tokens), 0)::bigint AS prompt_tokens,
                               coalesce(sum(completion_tokens), 0)::bigint AS completion_tokens,
                               coalesce(sum(total_tokens), 0)::bigint AS total_tokens,
                               coalesce(avg(response_ms), 0)::double precision AS avg_response_ms
                        FROM public.token_usage
                        WHERE timestamp >= now() - (%s * interval '1 hour')
                        GROUP BY node_id, model
                        ORDER BY total_tokens DESC, node_id, model
                        LIMIT 10000
                        """,
                        (bounded_hours,),
                    )
                    rows = await cursor.fetchall()
            return [
                {
                    "node_id": row[0],
                    "model": row[1],
                    "request_count": int(row[2]),
                    "prompt_tokens": int(row[3]),
                    "completion_tokens": int(row[4]),
                    "total_tokens": int(row[5]),
                    "avg_response_ms": float(row[6]),
                }
                for row in rows
            ]
        finally:
            await conn.close()

