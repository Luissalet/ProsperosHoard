"""ComfyMusic resolution: it needs both the ACE-Step text-encode node
*and* an `ace_step*` checkpoint on disk - either alone still fails the
first real call."""

from __future__ import annotations

from prosperos_hoard.backend import ComfyMusic


def test_unavailable_with_empty_object_info():
    m = ComfyMusic({})
    assert m.available() is False
    assert "TextEncodeAceStepAudio1.5" in m.reason()


def test_unavailable_with_node_but_no_checkpoint():
    object_info = {
        "TextEncodeAceStepAudio1.5": {"input": {"required": {}}},
        "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["sd_xl_base_1.0.safetensors"]]}}},
    }
    m = ComfyMusic(object_info)
    assert m.available() is False
    assert "checkpoint" in m.reason()


def test_available_with_node_and_checkpoint():
    object_info = {
        "TextEncodeAceStepAudio1.5": {"input": {"required": {}}},
        "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [
            ["sd_xl_base_1.0.safetensors", "ace_step_1.5_turbo_aio.safetensors"]
        ]}}},
    }
    m = ComfyMusic(object_info)
    assert m.available() is True
    assert "ace_step_1.5_turbo_aio.safetensors" in m.reason()


def test_matches_a_real_object_info():
    from prosperos_hoard.devtools.fake_comfy import real_object_info

    m = ComfyMusic(real_object_info())
    assert m.available() is True
