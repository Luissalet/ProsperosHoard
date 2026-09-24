"""Local LoRA training for a character: architecture presets, trainer
discovery, a training plan sized to the dataset, command/config building
for each trainer kind, progress parsing from a trainer's stdout, and
running the trainer as a subprocess with cancellation.

Supported trainer kinds (see `backend.json`'s `training` section,
documented in `SPEC_CHARKIT.md`):
- `ai_toolkit`: ostris/ai-toolkit, driven with `python run.py config.yaml`.
- `musubi`: kohya-ss musubi-tuner, driven with `python -m musubi_tuner.<script>`.
- `custom`: a user-provided command template with `{placeholders}`.
- `fake`: `prosperos_hoard.devtools.fake_trainer`, for tests and demos.

Every subprocess goes through `procutil` (see `AGENTS.md`); this module has
no FastAPI and no hoard_link import.
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import procutil

# --------------------------------------------------------------------------- errors


class TrainingError(Exception):
    """A training setup or run failure. `code` is a short machine-readable
    reason (e.g. "no_base_model", "cancelled"); `message` is the text shown
    to the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- arch presets

ARCHS = ["qwen_image", "flux1", "sdxl", "sd15", "wan22_5b", "z_image"]

# kind -> set of archs it can train. "custom" and "fake" support every arch
# (the user's own command, or the test double, decide what that means).
_TRAINER_KIND_ARCHS: dict[str, set[str]] = {
    "ai_toolkit": {"qwen_image", "flux1", "sdxl", "sd15", "wan22_5b", "z_image"},
    "musubi": {"qwen_image", "flux1", "wan22_5b"},
    "custom": set(ARCHS),
    "fake": set(ARCHS),
}

# Estimates only - a real run varies with dataset, trainer version and GPU.
# rank/lr follow the common wisdom for each family: SDXL/SD1.5 UNets are
# small enough to afford a higher rank and learning rate than the DiT-based
# models (Qwen-Image, Flux, Wan, Z-Image), which are usually trained with a
# lower LR to avoid destabilising the (much larger, quantized) base.
ARCH_PRESETS: dict[str, dict[str, Any]] = {
    "qwen_image": {
        "label": "Qwen-Image", "steps_per_image": 100, "min_steps": 500, "max_steps": 3000,
        "rank": 16, "lr": 1e-4, "resolution": 512, "est_vram_mb": 15500, "sec_per_step": 3.2,
        "base_hint": "Qwen/Qwen-Image (Hugging Face repo id, or a local folder with the same files)",
    },
    "flux1": {
        "label": "FLUX.1", "steps_per_image": 100, "min_steps": 500, "max_steps": 3000,
        "rank": 16, "lr": 1e-4, "resolution": 512, "est_vram_mb": 13500, "sec_per_step": 2.4,
        "base_hint": "black-forest-labs/FLUX.1-dev (Hugging Face repo id, or a local folder)",
    },
    "sdxl": {
        "label": "SDXL", "steps_per_image": 100, "min_steps": 500, "max_steps": 3000,
        "rank": 32, "lr": 4e-4, "resolution": 512, "est_vram_mb": 9000, "sec_per_step": 1.0,
        "base_hint": "stabilityai/stable-diffusion-xl-base-1.0 (Hugging Face repo id, or a local .safetensors)",
    },
    "sd15": {
        "label": "SD 1.5", "steps_per_image": 100, "min_steps": 500, "max_steps": 3000,
        "rank": 32, "lr": 4e-4, "resolution": 512, "est_vram_mb": 6000, "sec_per_step": 0.5,
        "base_hint": "runwayml/stable-diffusion-v1-5 (Hugging Face repo id, or a local .safetensors)",
    },
    "wan22_5b": {
        "label": "Wan 2.2 5B", "steps_per_image": 100, "min_steps": 500, "max_steps": 3000,
        "rank": 16, "lr": 1e-4, "resolution": 512, "est_vram_mb": 14000, "sec_per_step": 3.0,
        "base_hint": "Wan-AI/Wan2.2-TI2V-5B (Hugging Face repo id, or a local folder)",
    },
    "z_image": {
        "label": "Z-Image", "steps_per_image": 100, "min_steps": 500, "max_steps": 3000,
        "rank": 16, "lr": 1e-4, "resolution": 512, "est_vram_mb": 13400, "sec_per_step": 1.8,
        "base_hint": "Tongyi-MAI/Z-Image-Turbo (Hugging Face repo id, or a local folder)",
    },
}

for _arch, _preset in ARCH_PRESETS.items():
    _preset["trainers"] = sorted(k for k, archs in _TRAINER_KIND_ARCHS.items() if _arch in archs)

# --------------------------------------------------------------------------- trainer status


def _python_candidates(entry: dict[str, Any], trainer_dir: Path) -> list[Path]:
    py = entry.get("python")
    if py:
        return [Path(py)]
    return [
        trainer_dir / "venv" / "Scripts" / "python.exe",
        trainer_dir / "venv" / "bin" / "python",
        trainer_dir / ".venv" / "Scripts" / "python.exe",
        trainer_dir / ".venv" / "bin" / "python",
    ]


def _resolve_python(entry: dict[str, Any], trainer_dir: Path) -> Optional[Path]:
    for cand in _python_candidates(entry, trainer_dir):
        if cand.is_file():
            return cand
    return None


def _ai_toolkit_status(entry: dict[str, Any], trainer_dir: Path) -> tuple[bool, str]:
    if not trainer_dir.is_dir():
        return False, "trainer dir not found"
    if not (trainer_dir / "run.py").is_file():
        return False, "run.py not found in trainer dir"
    if _resolve_python(entry, trainer_dir) is None:
        return False, "no python in venv"
    return True, ""


def _musubi_status(entry: dict[str, Any], trainer_dir: Path) -> tuple[bool, str]:
    if not trainer_dir.is_dir():
        return False, "trainer dir not found"
    if (trainer_dir / "src" / "musubi_tuner").is_dir():
        return True, ""
    if any(trainer_dir.rglob("*_train_network.py")):
        return True, ""
    return False, "musubi_tuner package not found"


def _custom_status(entry: dict[str, Any]) -> tuple[bool, str]:
    cmd = entry.get("command")
    if isinstance(cmd, list) and cmd:
        return True, ""
    return False, "no command configured"


def trainer_status(training_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """One status row per configured trainer: {name, kind, ok, reason,
    archs, dir}. `archs` is the subset of `ARCHS` this trainer kind can
    handle; used by the UI to grey out unsupported arches for a trainer."""
    rows: list[dict[str, Any]] = []
    for entry in training_cfg.get("trainers", []) or []:
        kind = entry.get("kind", "")
        name = entry.get("name") or kind
        raw_dir = entry.get("dir") or ""
        trainer_dir = Path(raw_dir) if raw_dir else Path()
        archs = sorted(_TRAINER_KIND_ARCHS.get(kind, set()))
        if kind == "ai_toolkit":
            ok, reason = _ai_toolkit_status(entry, trainer_dir)
        elif kind == "musubi":
            ok, reason = _musubi_status(entry, trainer_dir)
        elif kind == "custom":
            ok, reason = _custom_status(entry)
        elif kind == "fake":
            ok, reason = True, ""
        else:
            ok, reason = False, f"unknown trainer kind: {kind!r}"
        rows.append({"name": name, "kind": kind, "ok": ok, "reason": reason, "archs": archs, "dir": raw_dir})
    return rows


# --------------------------------------------------------------------------- planning

_OVERRIDE_RANGES = {
    "steps": (100, 10000),
    "rank": (4, 128),
    "lr": (1e-6, 1e-2),
}


def _validate_resolution(res: int) -> None:
    if res < 256 or res > 1536 or res % 64 != 0:
        raise TrainingError("bad_resolution", f"resolution must be a multiple of 64 in [256, 1536], got {res}")


def plan_training(arch: str, n_images: int, overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Size a training run to the dataset: steps scaled ~100/image and
    clamped to the arch's [min_steps, max_steps], the arch's default rank/
    lr/resolution, batch_size 1, save/sample cadence, and rough VRAM/time
    estimates. `overrides` may set steps/rank/lr/resolution explicitly
    (validated); anything else raises `TrainingError("too_few_images")`
    below 4 images, or warns below 8."""
    if arch not in ARCH_PRESETS:
        raise TrainingError("bad_arch", f"unknown architecture: {arch!r}")
    preset = ARCH_PRESETS[arch]
    overrides = overrides or {}

    warnings: list[str] = []
    if n_images < 4:
        raise TrainingError("too_few_images", f"need at least 4 dataset images, got {n_images}")
    if n_images < 8:
        warnings.append(f"only {n_images} images: results may be weak or overfit; 15-30 is a good range")

    if "steps" in overrides:
        steps = int(overrides["steps"])
        lo, hi = _OVERRIDE_RANGES["steps"]
        if not (lo <= steps <= hi):
            raise TrainingError("bad_steps", f"steps must be in [{lo}, {hi}], got {steps}")
    else:
        steps = max(preset["min_steps"], min(preset["max_steps"], n_images * preset["steps_per_image"]))

    if "rank" in overrides:
        rank = int(overrides["rank"])
        lo, hi = _OVERRIDE_RANGES["rank"]
        if not (lo <= rank <= hi):
            raise TrainingError("bad_rank", f"rank must be in [{lo}, {hi}], got {rank}")
    else:
        rank = preset["rank"]

    if "lr" in overrides:
        lr = float(overrides["lr"])
        lo, hi = _OVERRIDE_RANGES["lr"]
        if not (lo <= lr <= hi):
            raise TrainingError("bad_lr", f"lr must be in [{lo}, {hi}], got {lr}")
    else:
        lr = preset["lr"]

    if "resolution" in overrides:
        resolution = int(overrides["resolution"])
        _validate_resolution(resolution)
    else:
        resolution = preset["resolution"]

    save_every = max(50, steps // 10)
    sample_every = max(50, steps // 10)
    est_minutes = round(steps * preset["sec_per_step"] / 60.0, 1)

    return {
        "arch": arch, "steps": steps, "rank": rank, "lr": lr, "resolution": resolution,
        "batch_size": 1, "save_every": save_every, "sample_every": sample_every,
        "est_vram_mb": preset["est_vram_mb"], "est_minutes": est_minutes, "warnings": warnings,
    }


# --------------------------------------------------------------------------- command building

_NOISE_SCHEDULER = {"sdxl": "ddpm", "sd15": "ddpm"}


def _ai_toolkit_model_flags(arch: str) -> dict[str, Any]:
    if arch == "qwen_image":
        return {"arch": "qwen_image"}
    if arch == "flux1":
        return {"is_flux": True}
    if arch == "sdxl":
        return {"is_xl": True}
    if arch == "wan22_5b":
        return {"arch": "wan22_5b"}
    if arch == "z_image":
        return {"arch": "zimage"}
    return {}


def _yaml_scalar(value: Any) -> str:
    """Minimal YAML scalar formatting - no PyYAML dependency (AGENTS.md /
    the module docstring say to build it by hand). Good enough for the
    plain values this module ever emits (str/int/float/bool)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    if text == "" or any(c in text for c in [":", "#", "{", "}", "[", "]", "\n"]) or text.strip() != text:
        return json.dumps(text)
    return text


def _yaml_lines(obj: Any, indent: int = 0) -> list[str]:
    """Dump a dict/list/scalar tree of the shapes used below into YAML
    lines by hand (dicts as mappings, lists of dicts as `- key: value`
    blocks, lists of scalars as flow lists)."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, dict):
                lines.append(f"{pad}{key}:")
                lines.extend(_yaml_lines(value, indent + 1))
            elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
                lines.append(f"{pad}{key}:")
                for item in value:
                    item_lines = _yaml_lines(item, indent + 1)
                    lines.append(f"{pad}- " + item_lines[0].strip())
                    lines.extend(item_lines[1:])
            elif isinstance(value, list):
                flow = ", ".join(_yaml_scalar(v) for v in value)
                lines.append(f"{pad}{key}: [{flow}]")
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(value)}")
    else:
        lines.append(f"{pad}{_yaml_scalar(obj)}")
    return lines


def _dump_yaml(obj: dict[str, Any]) -> str:
    return "\n".join(_yaml_lines(obj)) + "\n"


@dataclass
class BuiltCommand:
    """What `build_command` produces: the argv to run, the files to write
    first (path -> text content), and the working directory to run it in."""

    argv: list[str]
    files: dict[Path, str]
    cwd: Optional[Path] = None


def _require_base_model(arch: str, base_model: Optional[str]) -> str:
    if not base_model:
        hint = ARCH_PRESETS[arch]["base_hint"]
        raise TrainingError(
            "no_base_model",
            f"no base model set for {arch}: set training.base_models.{arch} to {hint}",
        )
    return base_model


def _check_arch_supported(kind: str, arch: str) -> None:
    if arch not in _TRAINER_KIND_ARCHS.get(kind, set()):
        raise TrainingError("arch_unsupported", f"trainer kind {kind!r} does not support architecture {arch!r}")


def _build_ai_toolkit(trainer: dict[str, Any], plan: dict[str, Any], *, dataset_dir: Path, output_dir: Path,
                       name: str, trigger: str, base_model: Optional[str]) -> BuiltCommand:
    arch = plan["arch"]
    _check_arch_supported("ai_toolkit", arch)
    base_model = _require_base_model(arch, base_model)
    trainer_dir = Path(trainer.get("dir") or ".")
    python = _resolve_python(trainer, trainer_dir)
    if python is None:
        raise TrainingError("no_python", f"no python found for ai_toolkit trainer {trainer.get('name')!r}")

    config: dict[str, Any] = {
        "job": "extension",
        "config": {
            "name": name,
            "process": [
                {
                    "type": "sd_trainer",
                    "training_folder": output_dir.as_posix(),
                    "device": "cuda:0",
                    "trigger_word": trigger,
                    "network": {"type": "lora", "linear": plan["rank"], "linear_alpha": plan["rank"]},
                    "save": {"dtype": "float16", "save_every": plan["save_every"], "max_step_saves_to_keep": 4},
                    "datasets": [
                        {
                            "folder_path": dataset_dir.as_posix(),
                            "caption_ext": "txt",
                            "caption_dropout_rate": 0.05,
                            "cache_latents_to_disk": True,
                            "resolution": [plan["resolution"]],
                        }
                    ],
                    "train": {
                        "batch_size": plan["batch_size"],
                        "steps": plan["steps"],
                        "gradient_accumulation_steps": 1,
                        "train_unet": True,
                        "train_text_encoder": False,
                        "gradient_checkpointing": True,
                        "noise_scheduler": _NOISE_SCHEDULER.get(arch, "flowmatch"),
                        "optimizer": "adamw8bit",
                        "lr": plan["lr"],
                        "dtype": "bf16",
                    },
                    "model": {
                        "name_or_path": base_model.replace("\\", "/"),
                        "quantize": True,
                        "low_vram": True,
                        **_ai_toolkit_model_flags(arch),
                    },
                    "sample": {
                        "sampler": "flowmatch",
                        "sample_every": plan["sample_every"],
                        "width": 512,
                        "height": 512,
                        "prompts": [f"{trigger}, portrait", f"{trigger}, full body, standing"],
                        "seed": 42,
                        "walk_seed": True,
                        "guidance_scale": 4,
                        "sample_steps": 20,
                    },
                }
            ],
        },
    }
    config_path = output_dir / "config.yaml"
    files = {config_path: _dump_yaml(config)}
    argv = [str(python), "run.py", str(config_path)]
    return BuiltCommand(argv=argv, files=files, cwd=trainer_dir)


# musubi-tuner ships one training entry point script per model family; the
# module name and the arch-specific flags below follow its documented CLI
# (see the musubi-tuner README for each arch) - kept intentionally minimal,
# a `custom` trainer entry can always add whatever extra flags a given
# checkout needs.
_MUSUBI_SCRIPTS = {
    "qwen_image": {"script": "qwen_image_train_network", "module": "networks.lora_qwen_image", "model_flag": "--dit"},
    "flux1": {"script": "flux_kontext_train_network", "module": "networks.lora_flux", "model_flag": "--dit"},
    "wan22_5b": {"script": "wan_train_network", "module": "networks.lora_wan", "model_flag": "--dit",
                 "extra": ["--task", "t2v-A14B"]},
}


def _build_musubi(trainer: dict[str, Any], plan: dict[str, Any], *, dataset_dir: Path, output_dir: Path,
                   name: str, trigger: str, base_model: Optional[str]) -> BuiltCommand:
    arch = plan["arch"]
    _check_arch_supported("musubi", arch)
    base_model = _require_base_model(arch, base_model)
    spec = _MUSUBI_SCRIPTS[arch]
    trainer_dir = Path(trainer.get("dir") or ".")
    python = _resolve_python(trainer, trainer_dir) or Path(trainer.get("python") or sys.executable)

    toml_path = output_dir / "dataset.toml"
    toml_text = (
        "[general]\n"
        f"resolution = [{plan['resolution']}, {plan['resolution']}]\n"
        'caption_extension = ".txt"\n'
        "batch_size = 1\n"
        "enable_bucket = true\n\n"
        "[[datasets]]\n"
        f'image_directory = "{dataset_dir.as_posix()}"\n'
        f'cache_directory = "{(output_dir / "cache").as_posix()}"\n'
    )

    argv = [
        str(python), "-m", f"musubi_tuner.{spec['script']}",
        "--dataset_config", str(toml_path),
        "--output_dir", str(output_dir),
        "--output_name", name,
        "--network_module", spec["module"],
        "--network_dim", str(plan["rank"]),
        "--learning_rate", str(plan["lr"]),
        "--max_train_steps", str(plan["steps"]),
        "--save_every_n_steps", str(plan["save_every"]),
        "--mixed_precision", "bf16",
        "--gradient_checkpointing",
        "--optimizer_type", "adamw8bit",
        "--sdpa",
        spec["model_flag"], base_model,
        *spec.get("extra", []),
    ]
    return BuiltCommand(argv=argv, files={toml_path: toml_text}, cwd=trainer_dir)


_PLACEHOLDER_KEYS = ["dataset_dir", "output_dir", "name", "trigger", "steps", "rank", "lr", "resolution",
                     "base_model", "arch"]


def _build_custom(trainer: dict[str, Any], plan: dict[str, Any], *, dataset_dir: Path, output_dir: Path,
                   name: str, trigger: str, base_model: Optional[str]) -> BuiltCommand:
    template = trainer.get("command") or []
    if not template:
        raise TrainingError("bad_trainer_config", f"custom trainer {trainer.get('name')!r} has no command")
    values = {
        "dataset_dir": str(dataset_dir), "output_dir": str(output_dir), "name": name, "trigger": trigger,
        "steps": str(plan["steps"]), "rank": str(plan["rank"]), "lr": str(plan["lr"]),
        "resolution": str(plan["resolution"]), "base_model": base_model or "", "arch": plan["arch"],
    }
    argv = [str(part).format(**values) for part in template]
    trainer_dir = trainer.get("dir")
    return BuiltCommand(argv=argv, files={}, cwd=Path(trainer_dir) if trainer_dir else None)


def _build_fake(trainer: dict[str, Any], plan: dict[str, Any], *, dataset_dir: Path, output_dir: Path,
                 name: str, trigger: str, base_model: Optional[str]) -> BuiltCommand:
    delay = trainer.get("delay", 0.01)
    argv = [
        sys.executable, "-m", "prosperos_hoard.devtools.fake_trainer",
        "--out", str(output_dir), "--name", name, "--steps", str(plan["steps"]), "--delay", str(delay),
    ]
    return BuiltCommand(argv=argv, files={})


_BUILDERS: dict[str, Callable[..., BuiltCommand]] = {
    "ai_toolkit": _build_ai_toolkit,
    "musubi": _build_musubi,
    "custom": _build_custom,
    "fake": _build_fake,
}


def build_command(trainer: dict[str, Any], plan: dict[str, Any], *, dataset_dir: Path, output_dir: Path,
                   name: str, trigger: str, base_model: Optional[str]) -> tuple[list[str], dict[Path, str], Optional[Path]]:
    """Build the command to run this trainer, for `trainer["kind"]`
    (ai_toolkit/musubi/custom/fake). Returns `(argv, files, cwd)`: `files`
    are written to disk (config/dataset files) before `argv` runs, and
    `cwd` is the working directory to run it in (None to use the caller's
    own cwd). Widened from the spec's `(cmd, files)` to also carry `cwd`,
    since ai_toolkit and musubi must run from the trainer's own
    checkout (`run.py` is resolved relative to it)."""
    kind = trainer.get("kind")
    builder = _BUILDERS.get(kind)
    if builder is None:
        raise TrainingError("bad_trainer_kind", f"unknown trainer kind: {kind!r}")
    built = builder(trainer, plan, dataset_dir=dataset_dir, output_dir=output_dir, name=name, trigger=trigger,
                     base_model=base_model)
    return built.argv, built.files, built.cwd


# --------------------------------------------------------------------------- progress parsing

# tqdm-style: "  8%|▊   | 120/1500 [00:10<01:40, 1.2it/s, loss=0.123]"
# also matches "steps:  12%| ... | 180/1500 ..."
_TQDM_RE = re.compile(r"(\d+)\s*/\s*(\d+)\b")
# "step 120/1500", "Step: 120 / 1500"
_STEP_RE = re.compile(r"\bstep[:\s]+(\d+)\s*/\s*(\d+)\b", re.IGNORECASE)
_LOSS_RE = re.compile(r"\b(?:avr_)?loss[:=]\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)


def parse_progress(line: str) -> Optional[dict[str, Any]]:
    """Pull {"step", "total", "loss"?} out of one line of a trainer's
    stdout, or None if the line carries no recognisable progress. Accepts
    tqdm bars (with or without a `steps:`/`step 123/1500` prefix) and a
    standalone `loss: 0.123` / `loss=0.123` / `avr_loss=0.12`."""
    m = _STEP_RE.search(line) or _TQDM_RE.search(line)
    if not m:
        return None
    step, total = int(m.group(1)), int(m.group(2))
    if total <= 0 or step > total:
        return None
    result: dict[str, Any] = {"step": step, "total": total}
    loss_m = _LOSS_RE.search(line)
    if loss_m:
        result["loss"] = float(loss_m.group(1))
    return result


# --------------------------------------------------------------------------- output discovery

_STEP_SUFFIX_RE = re.compile(r"([_-])(?:step)?0*\d{4,}$", re.IGNORECASE)


def _is_step_checkpoint(stem: str, name: str) -> bool:
    if stem == name:
        return False
    return bool(_STEP_SUFFIX_RE.search(stem))


def find_output(output_dir: Path, name: str) -> Optional[Path]:
    """The newest `.safetensors` under `output_dir` (recursive). When
    several exist (a final adapter plus intermediate checkpoints saved
    every `save_every` steps), prefer one whose stem is exactly `name` or
    that does not look like a step checkpoint (`..._000001000` /
    `...-step00001000`) over one that does."""
    candidates = sorted(output_dir.rglob("*.safetensors"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None
    preferred = [p for p in candidates if not _is_step_checkpoint(p.stem, name)]
    return preferred[0] if preferred else candidates[0]


# --------------------------------------------------------------------------- run + cancel


def _kill_process_tree(proc: Any) -> None:
    if sys.platform == "win32":
        procutil.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"])
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()


def run_training(trainer: dict[str, Any], plan: dict[str, Any], *, dataset_dir: Path, output_dir: Path,
                  name: str, trigger: str, base_model: Optional[str], log_path: Path,
                  progress: Callable[[float, str], None], should_cancel: Callable[[], bool],
                  env: Optional[dict[str, str]] = None) -> Path:
    """Write the trainer's config/command files, run it, stream its output
    to `log_path` while calling `progress(fraction, message)` on parsed
    progress lines, and return the produced `.safetensors` path.

    Cancellation is polled every ~0.5s via `should_cancel()` even when the
    trainer emits no output for a while (stdout is read on a background
    thread into a queue). On cancel the whole process tree is killed and
    `TrainingError("cancelled", ...)` is raised. A non-zero exit raises
    `TrainingError("trainer_failed", ...)` with the last lines of the log;
    a zero exit with no discoverable output raises
    `TrainingError("no_output", ...)`.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_dir.mkdir(parents=True, exist_ok=True)
    argv, files, cwd = build_command(trainer, plan, dataset_dir=dataset_dir, output_dir=output_dir, name=name,
                                      trigger=trigger, base_model=base_model)
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    run_env = dict(os.environ)
    run_env["PYTHONUNBUFFERED"] = "1"
    run_env["PYTHONIOENCODING"] = "utf-8"
    if env:
        run_env.update(env)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    popen_kwargs: dict[str, Any] = dict(
        stdout=procutil.subprocess.PIPE, stderr=procutil.subprocess.STDOUT, text=True,
        cwd=str(cwd) if cwd else None, env=run_env,
    )
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True
    proc = procutil.popen(argv, **popen_kwargs)

    line_queue: "queue.Queue[Optional[str]]" = queue.Queue()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line_queue.put(raw_line.rstrip("\n"))
        finally:
            line_queue.put(None)  # sentinel: stdout closed (process exiting)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    lines: list[str] = []
    cancelled = False
    with log_path.open("a", encoding="utf-8") as log_file:
        stdout_closed = False
        while True:
            if should_cancel():
                cancelled = True
                _kill_process_tree(proc)
                break
            try:
                line = line_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                stdout_closed = True
                break
            lines.append(line)
            log_file.write(line + "\n")
            log_file.flush()
            parsed = parse_progress(line)
            if parsed:
                total = parsed["total"]
                frac = 0.02 + 0.93 * (parsed["step"] / total if total else 0.0)
                loss = parsed.get("loss")
                msg = f"step {parsed['step']}/{total}" + (f" · loss {loss}" if loss is not None else "")
                progress(min(0.95, frac), msg)
        if stdout_closed:
            proc.wait()

    if cancelled:
        proc.wait(timeout=10)
        raise TrainingError("cancelled", "training cancelled by user")

    returncode = proc.returncode if proc.returncode is not None else proc.wait()
    if returncode != 0:
        tail = "\n".join(lines[-15:])
        raise TrainingError("trainer_failed", f"trainer exited with code {returncode}:\n{tail}")

    output = find_output(output_dir, name)
    if output is None:
        raise TrainingError("no_output", f"trainer exited 0 but produced no .safetensors under {output_dir}")
    return output
