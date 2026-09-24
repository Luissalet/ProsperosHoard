"""Tests for `prosperos_hoard.trainers`: trainer status detection, plan
sizing/validation, command building for each trainer kind, progress
parsing, output discovery, and an end-to-end run (including cancel and
failure) against the fake trainer."""

from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

import pytest

from prosperos_hoard import trainers as T


# --------------------------------------------------------------------------- trainer_status


def test_status_ai_toolkit_ok(tmp_path: Path) -> None:
    trainer_dir = tmp_path / "ai-toolkit"
    (trainer_dir / "venv" / "bin").mkdir(parents=True)
    (trainer_dir / "run.py").write_text("# stub", encoding="utf-8")
    (trainer_dir / "venv" / "bin" / "python").write_text("#!/bin/sh", encoding="utf-8")

    cfg = {"trainers": [{"kind": "ai_toolkit", "name": "ait", "dir": str(trainer_dir)}]}
    rows = T.trainer_status(cfg)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "ait"
    assert row["kind"] == "ai_toolkit"
    assert row["ok"] is True
    assert row["reason"] == ""
    assert "qwen_image" in row["archs"]
    assert "sdxl" in row["archs"]


def test_status_ai_toolkit_missing_run_py(tmp_path: Path) -> None:
    trainer_dir = tmp_path / "ai-toolkit"
    trainer_dir.mkdir()
    cfg = {"trainers": [{"kind": "ai_toolkit", "name": "ait", "dir": str(trainer_dir)}]}
    row = T.trainer_status(cfg)[0]
    assert row["ok"] is False
    assert "run.py" in row["reason"]


def test_status_ai_toolkit_no_python(tmp_path: Path) -> None:
    trainer_dir = tmp_path / "ai-toolkit"
    trainer_dir.mkdir()
    (trainer_dir / "run.py").write_text("# stub", encoding="utf-8")
    cfg = {"trainers": [{"kind": "ai_toolkit", "name": "ait", "dir": str(trainer_dir)}]}
    row = T.trainer_status(cfg)[0]
    assert row["ok"] is False
    assert "python" in row["reason"]


def test_status_ai_toolkit_explicit_python(tmp_path: Path) -> None:
    trainer_dir = tmp_path / "ai-toolkit"
    trainer_dir.mkdir()
    (trainer_dir / "run.py").write_text("# stub", encoding="utf-8")
    py = tmp_path / "custom_python"
    py.write_text("#!/bin/sh", encoding="utf-8")
    cfg = {"trainers": [{"kind": "ai_toolkit", "name": "ait", "dir": str(trainer_dir), "python": str(py)}]}
    row = T.trainer_status(cfg)[0]
    assert row["ok"] is True


def test_status_musubi_ok_via_package_dir(tmp_path: Path) -> None:
    trainer_dir = tmp_path / "musubi"
    (trainer_dir / "src" / "musubi_tuner").mkdir(parents=True)
    cfg = {"trainers": [{"kind": "musubi", "name": "mus", "dir": str(trainer_dir)}]}
    row = T.trainer_status(cfg)[0]
    assert row["ok"] is True
    assert "flux1" in row["archs"]
    assert "sdxl" not in row["archs"]  # musubi does not support sdxl per spec


def test_status_musubi_ok_via_script_glob(tmp_path: Path) -> None:
    trainer_dir = tmp_path / "musubi"
    trainer_dir.mkdir()
    (trainer_dir / "qwen_image_train_network.py").write_text("# stub", encoding="utf-8")
    cfg = {"trainers": [{"kind": "musubi", "name": "mus", "dir": str(trainer_dir)}]}
    row = T.trainer_status(cfg)[0]
    assert row["ok"] is True


def test_status_musubi_missing(tmp_path: Path) -> None:
    trainer_dir = tmp_path / "musubi"
    trainer_dir.mkdir()
    cfg = {"trainers": [{"kind": "musubi", "name": "mus", "dir": str(trainer_dir)}]}
    row = T.trainer_status(cfg)[0]
    assert row["ok"] is False


def test_status_custom(tmp_path: Path) -> None:
    cfg = {"trainers": [
        {"kind": "custom", "name": "good", "command": ["python", "train.py"]},
        {"kind": "custom", "name": "bad", "command": []},
    ]}
    rows = T.trainer_status(cfg)
    assert rows[0]["ok"] is True
    assert rows[1]["ok"] is False


def test_status_fake_always_ok() -> None:
    cfg = {"trainers": [{"kind": "fake", "name": "fk"}]}
    row = T.trainer_status(cfg)[0]
    assert row["ok"] is True
    assert set(row["archs"]) == set(T.ARCHS)


def test_status_unknown_kind() -> None:
    cfg = {"trainers": [{"kind": "mystery", "name": "m"}]}
    row = T.trainer_status(cfg)[0]
    assert row["ok"] is False
    assert row["archs"] == []


def test_status_empty_cfg() -> None:
    assert T.trainer_status({}) == []


# --------------------------------------------------------------------------- plan_training


def test_plan_scales_with_images() -> None:
    plan_small = T.plan_training("sdxl", 10)
    plan_large = T.plan_training("sdxl", 20)
    assert plan_small["steps"] == 1000
    assert plan_large["steps"] == 2000
    assert plan_large["steps"] > plan_small["steps"]


def test_plan_clamps_to_max_steps() -> None:
    plan = T.plan_training("sdxl", 1000)
    assert plan["steps"] == T.ARCH_PRESETS["sdxl"]["max_steps"]


def test_plan_clamps_to_min_steps() -> None:
    plan = T.plan_training("sdxl", 4)
    assert plan["steps"] == T.ARCH_PRESETS["sdxl"]["min_steps"]


def test_plan_defaults_from_preset() -> None:
    plan = T.plan_training("qwen_image", 15)
    preset = T.ARCH_PRESETS["qwen_image"]
    assert plan["rank"] == preset["rank"]
    assert plan["lr"] == preset["lr"]
    assert plan["resolution"] == preset["resolution"]
    assert plan["batch_size"] == 1
    assert plan["est_vram_mb"] == preset["est_vram_mb"]
    assert plan["est_minutes"] > 0
    assert plan["save_every"] > 0 and plan["sample_every"] > 0


def test_plan_few_images_warns() -> None:
    plan = T.plan_training("sdxl", 6)
    assert plan["warnings"]


def test_plan_too_few_images_raises() -> None:
    with pytest.raises(T.TrainingError) as exc:
        T.plan_training("sdxl", 2)
    assert exc.value.code == "too_few_images"


def test_plan_unknown_arch_raises() -> None:
    with pytest.raises(T.TrainingError) as exc:
        T.plan_training("not_an_arch", 10)
    assert exc.value.code == "bad_arch"


def test_plan_overrides_applied() -> None:
    plan = T.plan_training("sdxl", 10, overrides={"steps": 777, "rank": 64, "lr": 5e-4, "resolution": 768})
    assert plan["steps"] == 777
    assert plan["rank"] == 64
    assert plan["lr"] == 5e-4
    assert plan["resolution"] == 768


@pytest.mark.parametrize("overrides,code", [
    ({"steps": 50}, "bad_steps"),
    ({"steps": 50000}, "bad_steps"),
    ({"rank": 1}, "bad_rank"),
    ({"rank": 999}, "bad_rank"),
    ({"lr": 1.0}, "bad_lr"),
    ({"lr": 0}, "bad_lr"),
    ({"resolution": 100}, "bad_resolution"),
    ({"resolution": 513}, "bad_resolution"),
])
def test_plan_override_validation(overrides: dict, code: str) -> None:
    with pytest.raises(T.TrainingError) as exc:
        T.plan_training("sdxl", 10, overrides=overrides)
    assert exc.value.code == code


# --------------------------------------------------------------------------- build_command: ai_toolkit


def _ai_toolkit_trainer(tmp_path: Path) -> dict:
    trainer_dir = tmp_path / "ai-toolkit"
    (trainer_dir / "venv" / "bin").mkdir(parents=True)
    (trainer_dir / "run.py").write_text("# stub", encoding="utf-8")
    (trainer_dir / "venv" / "bin" / "python").write_text("#!/bin/sh", encoding="utf-8")
    return {"kind": "ai_toolkit", "name": "ait", "dir": str(trainer_dir)}


def test_build_command_ai_toolkit(tmp_path: Path) -> None:
    trainer = _ai_toolkit_trainer(tmp_path)
    plan = T.plan_training("qwen_image", 20)
    dataset_dir = tmp_path / "dataset"
    output_dir = tmp_path / "out"
    argv, files, cwd = T.build_command(trainer, plan, dataset_dir=dataset_dir, output_dir=output_dir,
                                        name="hero", trigger="ohwx hero", base_model="Qwen/Qwen-Image")
    assert argv[-2] == "run.py"
    config_path = output_dir / "config.yaml"
    assert Path(argv[-1]) == config_path
    assert cwd == Path(trainer["dir"])
    assert config_path in files
    yaml_text = files[config_path]

    assert "job: extension" in yaml_text
    assert "type: sd_trainer" in yaml_text
    assert output_dir.as_posix() in yaml_text  # forward slashes: no YAML escaping of Windows paths
    assert "trigger_word: " in yaml_text and "ohwx hero" in yaml_text
    assert "type: lora" in yaml_text
    assert f"linear: {plan['rank']}" in yaml_text
    assert "caption_ext: txt" in yaml_text
    assert f"steps: {plan['steps']}" in yaml_text
    assert "optimizer: adamw8bit" in yaml_text
    assert "gradient_checkpointing: true" in yaml_text
    assert "name_or_path: " in yaml_text and "Qwen/Qwen-Image" in yaml_text
    assert "quantize: true" in yaml_text
    assert "arch: qwen_image" in yaml_text
    assert "ohwx hero, portrait" in yaml_text

    # actually write it and confirm it round-trips as plain text (no
    # PyYAML dependency requirement, but should look like YAML)
    output_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml_text, encoding="utf-8")
    assert config_path.read_text(encoding="utf-8") == yaml_text


def test_build_command_ai_toolkit_arch_flags() -> None:
    assert T._ai_toolkit_model_flags("flux1") == {"is_flux": True}
    assert T._ai_toolkit_model_flags("sdxl") == {"is_xl": True}
    assert T._ai_toolkit_model_flags("wan22_5b") == {"arch": "wan22_5b"}
    assert T._ai_toolkit_model_flags("z_image") == {"arch": "zimage"}


def test_build_command_ai_toolkit_no_base_model(tmp_path: Path) -> None:
    trainer = _ai_toolkit_trainer(tmp_path)
    plan = T.plan_training("qwen_image", 20)
    with pytest.raises(T.TrainingError) as exc:
        T.build_command(trainer, plan, dataset_dir=tmp_path / "d", output_dir=tmp_path / "o",
                         name="hero", trigger="t", base_model=None)
    assert exc.value.code == "no_base_model"
    assert "qwen_image" in exc.value.message


def test_build_command_ai_toolkit_arch_unsupported(tmp_path: Path) -> None:
    # ai_toolkit supports every arch per spec, so force unsupported via musubi instead
    trainer_dir = tmp_path / "musubi"
    (trainer_dir / "src" / "musubi_tuner").mkdir(parents=True)
    trainer = {"kind": "musubi", "name": "mus", "dir": str(trainer_dir)}
    plan = T.plan_training("sdxl", 20)  # musubi does not support sdxl
    with pytest.raises(T.TrainingError) as exc:
        T.build_command(trainer, plan, dataset_dir=tmp_path / "d", output_dir=tmp_path / "o",
                         name="hero", trigger="t", base_model="x")
    assert exc.value.code == "arch_unsupported"


# --------------------------------------------------------------------------- build_command: musubi


def test_build_command_musubi(tmp_path: Path) -> None:
    trainer_dir = tmp_path / "musubi"
    (trainer_dir / "src" / "musubi_tuner").mkdir(parents=True)
    trainer = {"kind": "musubi", "name": "mus", "dir": str(trainer_dir)}
    plan = T.plan_training("flux1", 20)
    dataset_dir = tmp_path / "dataset"
    output_dir = tmp_path / "out"
    argv, files, cwd = T.build_command(trainer, plan, dataset_dir=dataset_dir, output_dir=output_dir,
                                        name="hero", trigger="ohwx hero", base_model="black-forest-labs/FLUX.1-dev")
    assert "-m" in argv
    assert "musubi_tuner.flux_kontext_train_network" in argv
    assert "--network_module" in argv
    assert "networks.lora_flux" in argv
    assert "--dit" in argv
    assert "black-forest-labs/FLUX.1-dev" in argv
    assert "--max_train_steps" in argv and str(plan["steps"]) in argv
    assert cwd == trainer_dir

    toml_path = output_dir / "dataset.toml"
    assert toml_path in files
    toml_text = files[toml_path]
    assert "[general]" in toml_text
    assert "[[datasets]]" in toml_text
    assert f"resolution = [{plan['resolution']}, {plan['resolution']}]" in toml_text
    assert str(dataset_dir.as_posix()) in toml_text


def test_build_command_musubi_no_base_model(tmp_path: Path) -> None:
    trainer_dir = tmp_path / "musubi"
    (trainer_dir / "src" / "musubi_tuner").mkdir(parents=True)
    trainer = {"kind": "musubi", "name": "mus", "dir": str(trainer_dir)}
    plan = T.plan_training("wan22_5b", 20)
    with pytest.raises(T.TrainingError) as exc:
        T.build_command(trainer, plan, dataset_dir=tmp_path / "d", output_dir=tmp_path / "o",
                         name="hero", trigger="t", base_model="")
    assert exc.value.code == "no_base_model"


# --------------------------------------------------------------------------- build_command: custom


def test_build_command_custom_placeholders(tmp_path: Path) -> None:
    trainer = {
        "kind": "custom", "name": "cust", "dir": str(tmp_path / "custdir"),
        "command": ["python", "train.py", "--data", "{dataset_dir}", "--out", "{output_dir}",
                    "--name", "{name}", "--trigger", "{trigger}", "--steps", "{steps}", "--rank", "{rank}",
                    "--lr", "{lr}", "--res", "{resolution}", "--base", "{base_model}", "--arch", "{arch}"],
    }
    plan = T.plan_training("sd15", 10)
    dataset_dir = tmp_path / "ds"
    output_dir = tmp_path / "out"
    argv, files, cwd = T.build_command(trainer, plan, dataset_dir=dataset_dir, output_dir=output_dir,
                                        name="hero", trigger="ohwx hero", base_model="runwayml/stable-diffusion-v1-5")
    assert files == {}
    assert cwd == Path(trainer["dir"])
    assert str(dataset_dir) in argv
    assert str(output_dir) in argv
    assert "hero" in argv
    assert "ohwx hero" in argv
    assert str(plan["steps"]) in argv
    assert str(plan["rank"]) in argv
    assert str(plan["lr"]) in argv
    assert str(plan["resolution"]) in argv
    assert "runwayml/stable-diffusion-v1-5" in argv
    assert "sd15" in argv


def test_build_command_custom_empty_command_raises() -> None:
    trainer = {"kind": "custom", "name": "cust", "command": []}
    plan = T.plan_training("sd15", 10)
    with pytest.raises(T.TrainingError) as exc:
        T.build_command(trainer, plan, dataset_dir=Path("d"), output_dir=Path("o"),
                         name="hero", trigger="t", base_model=None)
    assert exc.value.code == "bad_trainer_config"


# --------------------------------------------------------------------------- build_command: fake


def test_build_command_fake(tmp_path: Path) -> None:
    trainer = {"kind": "fake", "name": "fk"}
    plan = T.plan_training("sd15", 10)
    output_dir = tmp_path / "out"
    argv, files, cwd = T.build_command(trainer, plan, dataset_dir=tmp_path / "ds", output_dir=output_dir,
                                        name="hero", trigger="t", base_model=None)
    assert files == {}
    assert cwd is None
    assert "prosperos_hoard.devtools.fake_trainer" in argv
    assert "--out" in argv and str(output_dir) in argv
    assert "--name" in argv and "hero" in argv
    assert "--steps" in argv and str(plan["steps"]) in argv


def test_build_command_bad_kind() -> None:
    trainer = {"kind": "nope"}
    plan = T.plan_training("sd15", 10)
    with pytest.raises(T.TrainingError) as exc:
        T.build_command(trainer, plan, dataset_dir=Path("d"), output_dir=Path("o"),
                         name="hero", trigger="t", base_model=None)
    assert exc.value.code == "bad_trainer_kind"


# --------------------------------------------------------------------------- parse_progress


@pytest.mark.parametrize("line,step,total,loss", [
    ("  8%|▊         | 120/1500 [00:10<01:40, 1.2it/s, loss=0.123]", 120, 1500, 0.123),
    ("step 120/1500", 120, 1500, None),
    ("Step: 120 / 1500", 120, 1500, None),
    ("steps:  12%|#####     | 180/1500 [00:20<01:30, 1.1it/s]", 180, 1500, None),
    ("120/1500, loss: 0.456", 120, 1500, 0.456),
    ("epoch 1 step 5/100 avr_loss=0.02", 5, 100, 0.02),
])
def test_parse_progress_recognises_lines(line: str, step: int, total: int, loss) -> None:
    result = T.parse_progress(line)
    assert result is not None
    assert result["step"] == step
    assert result["total"] == total
    if loss is not None:
        assert result["loss"] == pytest.approx(loss)
    else:
        assert "loss" not in result or result.get("loss") is None or "loss" not in result


@pytest.mark.parametrize("line", [
    "just some log noise",
    "loading model...",
    "total 0/0",
    "step 2000/1500",  # step > total: nonsensical, ignored
])
def test_parse_progress_ignores_noise(line: str) -> None:
    assert T.parse_progress(line) is None


# --------------------------------------------------------------------------- find_output


def _write_safetensors_stub(path: Path, mtime_offset: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 16)
    if mtime_offset:
        t = time.time() + mtime_offset
        import os
        os.utime(path, (t, t))


def test_find_output_prefers_final_over_checkpoints(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_safetensors_stub(out / "hero_000000500.safetensors", mtime_offset=-1)
    _write_safetensors_stub(out / "hero.safetensors", mtime_offset=0)
    found = T.find_output(out, "hero")
    assert found == out / "hero.safetensors"


def test_find_output_prefers_final_even_when_older(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_safetensors_stub(out / "hero.safetensors", mtime_offset=-5)
    _write_safetensors_stub(out / "hero-step00001000.safetensors", mtime_offset=0)
    found = T.find_output(out, "hero")
    assert found == out / "hero.safetensors"


def test_find_output_falls_back_to_newest_checkpoint(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write_safetensors_stub(out / "hero_000000500.safetensors", mtime_offset=-2)
    _write_safetensors_stub(out / "hero_000001000.safetensors", mtime_offset=-1)
    found = T.find_output(out, "hero")
    assert found == out / "hero_000001000.safetensors"


def test_find_output_none_when_empty(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    assert T.find_output(out, "hero") is None


# --------------------------------------------------------------------------- run_training (fake trainer)


def test_run_training_end_to_end(tmp_path: Path) -> None:
    trainer = {"kind": "fake", "name": "fk", "delay": 0.0}
    plan = T.plan_training("sd15", 10, overrides={"steps": 100})
    dataset_dir = tmp_path / "dataset"
    output_dir = tmp_path / "out"
    log_path = tmp_path / "train.log"

    progress_calls = []

    def on_progress(frac: float, msg: str) -> None:
        progress_calls.append((frac, msg))

    result = T.run_training(trainer, plan, dataset_dir=dataset_dir, output_dir=output_dir, name="hero",
                             trigger="ohwx hero", base_model=None, log_path=log_path,
                             progress=on_progress, should_cancel=lambda: False)

    assert result == output_dir / "hero.safetensors"
    assert result.is_file()
    assert log_path.is_file()
    log_text = log_path.read_text(encoding="utf-8")
    assert "loss" in log_text
    assert progress_calls, "progress callback should have been invoked at least once"
    assert all(0.0 <= f <= 1.0 for f, _ in progress_calls)
    # progress should be roughly increasing
    assert progress_calls[-1][0] >= progress_calls[0][0]

    # sanity-check the safetensors header is well-formed
    header_len = struct.unpack("<Q", result.read_bytes()[:8])[0]
    assert header_len > 0


def test_run_training_cancel(tmp_path: Path) -> None:
    trainer = {"kind": "fake", "name": "fk", "delay": 0.05}
    plan = T.plan_training("sd15", 10, overrides={"steps": 200})
    dataset_dir = tmp_path / "dataset"
    output_dir = tmp_path / "out"
    log_path = tmp_path / "train.log"

    calls = {"n": 0}

    def on_progress(frac: float, msg: str) -> None:
        calls["n"] += 1

    def should_cancel() -> bool:
        return calls["n"] >= 1

    with pytest.raises(T.TrainingError) as exc:
        T.run_training(trainer, plan, dataset_dir=dataset_dir, output_dir=output_dir, name="hero",
                        trigger="ohwx hero", base_model=None, log_path=log_path,
                        progress=on_progress, should_cancel=should_cancel)
    assert exc.value.code == "cancelled"
    # the fake trainer needs ~200*0.05s = 10s to finish on its own; cancel
    # must have actually stopped it well before that
    assert not (output_dir / "hero.safetensors").exists()


def test_run_training_failure(tmp_path: Path) -> None:
    trainer = {"kind": "custom", "name": "boom", "command": [sys.executable, "-c", "import sys; sys.exit(3)"]}
    plan = T.plan_training("sd15", 10, overrides={"steps": 100})
    dataset_dir = tmp_path / "dataset"
    output_dir = tmp_path / "out"
    log_path = tmp_path / "train.log"

    with pytest.raises(T.TrainingError) as exc:
        T.run_training(trainer, plan, dataset_dir=dataset_dir, output_dir=output_dir, name="hero",
                        trigger="t", base_model=None, log_path=log_path,
                        progress=lambda f, m: None, should_cancel=lambda: False)
    assert exc.value.code == "trainer_failed"


def test_run_training_no_output(tmp_path: Path) -> None:
    # a command that exits 0 but writes no .safetensors
    trainer = {"kind": "custom", "name": "noop", "command": [sys.executable, "-c", "print('done')"]}
    plan = T.plan_training("sd15", 10, overrides={"steps": 100})
    dataset_dir = tmp_path / "dataset"
    output_dir = tmp_path / "out"
    log_path = tmp_path / "train.log"

    with pytest.raises(T.TrainingError) as exc:
        T.run_training(trainer, plan, dataset_dir=dataset_dir, output_dir=output_dir, name="hero",
                        trigger="t", base_model=None, log_path=log_path,
                        progress=lambda f, m: None, should_cancel=lambda: False)
    assert exc.value.code == "no_output"
