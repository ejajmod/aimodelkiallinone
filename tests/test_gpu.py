from launcher.gpu import RUNTIMES, GpuDevice, evaluate, parse_nvidia_smi, query_devices


def test_nvidia_smi_output_is_parsed() -> None:
    output = "NVIDIA GeForce RTX 5090, 580.95.05, 12.0\nNVIDIA GeForce RTX 4090, 575.57.08, 8.9\n"

    assert parse_nvidia_smi(output) == [
        GpuDevice("NVIDIA GeForce RTX 5090", "580.95.05", (12, 0)),
        GpuDevice("NVIDIA GeForce RTX 4090", "575.57.08", (8, 9)),
    ]


def test_cuda_13_accepts_blackwell_and_ada_on_a_580_driver() -> None:
    result = evaluate(
        RUNTIMES["cu130"],
        [
            GpuDevice("NVIDIA GeForce RTX 5090", "580.95.05", (12, 0)),
            GpuDevice("NVIDIA GeForce RTX 4090", "581.15", (8, 9)),
        ],
    )

    assert result["ok"] is True
    assert result["problems"] == []


def test_cuda_13_rejects_an_older_driver_and_suggests_cuda_12_8() -> None:
    result = evaluate(RUNTIMES["cu130"], [GpuDevice("NVIDIA GeForce RTX 4090", "575.57.08", (8, 9))])

    assert result["ok"] is False
    assert "580" in result["problems"][0]
    assert "CUDA 12.8" in result["problems"][0]


def test_volta_needs_the_cuda_12_8_variant() -> None:
    v100 = [GpuDevice("Tesla V100-SXM2-32GB", "580.95.05", (7, 0))]

    assert evaluate(RUNTIMES["cu130"], v100)["ok"] is False
    assert evaluate(RUNTIMES["cu128"], v100)["ok"] is True


def test_missing_nvidia_smi_is_unknown_not_a_failure() -> None:
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("nvidia-smi")

    assert query_devices(run=missing) is None
    result = evaluate(RUNTIMES["cu130"], None)
    assert result["ok"] is True
    assert result["detected"] is False


def test_pod_without_gpu_is_reported() -> None:
    assert evaluate(RUNTIMES["cu130"], [])["ok"] is False
