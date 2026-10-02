"""The planner's second pass: a rubric that needs no model, and a
director's review that rewrites the weak shots of a planned shot list."""

from __future__ import annotations

import json

from prosperos_hoard import mv_planner


def _draft(prompts, lead=True, motion="subtle motion", lyrics=None):
    d = {"title": "t", "world_look": "", "world_negative": "",
         "shots": [{"prompt": p, "lead": lead, "motion": "move", "motion_prompt": motion, "section": ""} for p in prompts]}
    if lyrics:
        d["song"] = {"lyrics": lyrics}
    return d


def test_the_rubric_catches_a_flat_shot_list():
    same = ["the singer stands on a rooftop at night under neon signs"] * 2 + ["the singer walks down a rainy street at night",
                                                                            "the singer sits in a diner booth at night"]
    issues = mv_planner.rubric_issues(_draft(same, lyrics="[Verse]\na\n[Chorus]\nb"))
    text = " | ".join(issues)
    assert "shots 1 and 2" in text and "same size" in text and "move the same way" in text
    assert not any("lead" in x for x in issues)
    varied = _draft(["wide establishing shot of a harbour at dawn", "close-up of the singer's hands on a guitar",
                     "medium shot, the singer laughs in a doorway", "aerial shot over the city lights"], lead=False)
    varied["shots"][1]["lead"] = True
    for i, s in enumerate(varied["shots"]):
        s["motion_prompt"] = f"motion {i}"
    assert mv_planner.rubric_issues(varied) == []


def test_the_critic_rewrites_weak_shots_and_keeps_sections():
    draft = _draft(["a feeling of loss", "the singer on a stage", "the singer on a stage again"])
    draft["shots"][0]["section"] = "intro"
    calls = []

    def chat(messages, budget, temperature):
        calls.append(messages)
        return json.dumps({"issues": ["shot 1 is an abstract idea", "shots 2 and 3 repeat"], "shots": [
            {"prompt": "wide shot of an empty pier at dusk, fog, 24mm", "lead": False, "motion": "move", "motion_prompt": "slow push in"},
            {"prompt": "close-up of the singer under a red spotlight", "lead": True, "motion": "move", "motion_prompt": "handheld drift"},
            {"prompt": "medium shot, the singer walks off stage into darkness", "lead": True, "motion": "move", "motion_prompt": "tracking"},
        ]})

    out = mv_planner.critique(chat, draft, concept="a farewell song", lead_name="VERA", lead_look="silver bob")
    assert out["revised"] and len(out["shots"]) == 3 and out["shots"][0]["section"] == "intro"
    assert "shot 1 is an abstract idea" in out["issues"]
    assert "VERA" in calls[0][1]["content"] and "Already noticed" in calls[0][1]["content"]


def test_a_failing_critic_leaves_the_rubric():
    def broken(*_):
        raise RuntimeError("model down")
    out = mv_planner.critique(broken, _draft(["x on a beach"] * 2), concept="c", lead_name="L", lead_look="")
    assert out == {"issues": ["shots 1 and 2 are almost the same picture"], "revised": False}

    def short(*_):
        return json.dumps({"issues": [], "shots": [{"prompt": "only one"}]})
    out = mv_planner.critique(short, _draft(["a", "b", "c", "d"]), concept="c", lead_name="L", lead_look="")
    assert out["revised"] is False
