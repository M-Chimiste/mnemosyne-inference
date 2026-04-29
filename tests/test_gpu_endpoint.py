from types import SimpleNamespace

import vllm_manager


def test_gpu_endpoint_parses_nvidia_smi_rows(client, monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd[0] == "nvidia-smi"
        assert kwargs["timeout"] == 5
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "0, NVIDIA RTX 6000 Ada, 1024, 49140, 12\n"
                "1, NVIDIA RTX 6000 Ada, 2048, 49140, 24\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(vllm_manager.subprocess, "run", fake_run)

    response = client.get("/manager/gpu")

    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "gpus": [
            {
                "index": 0,
                "name": "NVIDIA RTX 6000 Ada",
                "memory_used_mb": 1024,
                "memory_total_mb": 49140,
                "utilization_pct": 12,
            },
            {
                "index": 1,
                "name": "NVIDIA RTX 6000 Ada",
                "memory_used_mb": 2048,
                "memory_total_mb": 49140,
                "utilization_pct": 24,
            },
        ],
    }


def test_gpu_endpoint_returns_unavailable_when_nvidia_smi_missing(client, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(vllm_manager.subprocess, "run", fake_run)

    response = client.get("/manager/gpu")

    assert response.status_code == 200
    assert response.json() == {"available": False, "gpus": []}


def test_gpu_endpoint_returns_unavailable_on_nvidia_smi_failure(client, monkeypatch):
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="driver unavailable")

    monkeypatch.setattr(vllm_manager.subprocess, "run", fake_run)

    response = client.get("/manager/gpu")

    assert response.status_code == 200
    assert response.json() == {"available": False, "gpus": []}
