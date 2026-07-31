import pytest

from scripts.fleet_acceptance import _require_drained_baseline, _usage_counts


def test_usage_counts_aggregate_alias_rows_per_serving_node() -> None:
    payload = {
        "rows": [
            {
                "node_id": "metis",
                "model": "coder-primary",
                "public_model": "qwen-coder",
                "request_count": 2,
            },
            {
                "node_id": "metis",
                "model": "coder-compatible",
                "public_model": "qwen-coder",
                "request_count": 3,
            },
            {
                "node_id": "cuda-box",
                "model": "coder",
                "public_model": "qwen-coder",
                "request_count": 1,
            },
            {
                "node_id": "metis",
                "model": "embed",
                "public_model": "qwen-embed",
                "request_count": 99,
            },
        ]
    }

    assert _usage_counts(payload, "qwen-coder") == {
        "metis": 5,
        "cuda-box": 1,
    }


def _drained_status() -> dict:
    return {
        "scheduler": {
            "active_total": 0,
            "queues": {"qwen-coder": {"depth": 0}},
        },
        "nodes": [
            {
                "node_id": node_id,
                "capacity": {"active": 0},
                "admission": {"queue_depth": 0},
                "usage_delivery": {
                    "enabled": True,
                    "writer_ready": True,
                    "outbox_pending": 0,
                    "last_error_code": None,
                },
            }
            for node_id in ("metis", "cuda-box")
        ],
    }


def test_acceptance_baseline_requires_every_relevant_surface_drained() -> None:
    status = _drained_status()
    _require_drained_baseline(
        status,
        eligible_nodes={"metis", "cuda-box"},
        require_usage=True,
    )

    status["nodes"][1]["usage_delivery"]["outbox_pending"] = 1
    with pytest.raises(RuntimeError, match="usage delivery"):
        _require_drained_baseline(
            status,
            eligible_nodes={"metis", "cuda-box"},
            require_usage=True,
        )


def test_acceptance_baseline_can_skip_usage_but_not_active_work() -> None:
    status = _drained_status()
    status["nodes"][0]["usage_delivery"] = None
    _require_drained_baseline(
        status,
        eligible_nodes={"metis", "cuda-box"},
        require_usage=False,
    )

    status["scheduler"]["active_total"] = 1
    with pytest.raises(RuntimeError, match="active routes"):
        _require_drained_baseline(
            status,
            eligible_nodes={"metis", "cuda-box"},
            require_usage=False,
        )
