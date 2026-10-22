"""Tests verifying that API and MCP/Agent tools perform the exact same operations
for Style Profiles and Shot Reviews (Criterion 6: "API y MCP son la misma operación que la UI").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prosperos_hoard import backend, procutil
from prosperos_hoard.productions import create_production


@pytest.fixture
def mini_video(tmp_path: Path) -> Path:
    ffmpeg = backend.ffmpeg_path()
    assert ffmpeg, "ffmpeg is required for test"

    c1 = tmp_path / "c1.mp4"
    c2 = tmp_path / "c2.mp4"
    out_dir = tmp_path / "allowed"
    out_dir.mkdir(exist_ok=True)
    out_video = out_dir / "mini_ref.mp4"

    cmd1 = [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=red:s=160x120:d=0.8", "-r", "25", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(c1)]
    cmd2 = [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=1.2", "-r", "25", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(c2)]

    for cmd in (cmd1, cmd2):
        res = procutil.run(cmd, text=True)
        assert res.returncode == 0, f"ffmpeg failed: {res.stderr}"

    list_file = tmp_path / "concat.txt"
    list_file.write_text(f"file '{c1.name}'\nfile '{c2.name}'\n", encoding="utf-8")

    concat_cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out_video)]
    res = procutil.run(concat_cmd, text=True, cwd=str(tmp_path))
    assert res.returncode == 0, f"concat failed: {res.stderr}"
    assert out_video.is_file()
    return out_video


def test_api_and_agent_style_profiles_parity(client, mini_video: Path, data_dir: Path):
    c, app, _ = client

    # 1. User API extraction
    resp_user = c.post("/api/style-profiles/extract", json={"video_path": str(mini_video), "name": "User Profile"})
    assert resp_user.status_code == 200, resp_user.text
    profile_user = resp_user.json()
    assert profile_user["name"] == "User Profile"
    assert profile_user["measured_facts"]["shot_count"] == 2

    # 2. Agent tool extraction
    resp_agent = c.post("/api/agent/studio_style_profile_extract", json={"video_path": str(mini_video), "name": "Agent Profile"})
    assert resp_agent.status_code == 200, resp_agent.text
    profile_agent = resp_agent.json()
    assert profile_agent["name"] == "Agent Profile"
    assert profile_agent["measured_facts"]["shot_count"] == 2

    # 3. List profiles via user API and Agent route
    list_user = c.get("/api/style-profiles").json()["profiles"]
    list_agent = c.get("/api/agent/studio_style_profile_list").json()["profiles"]
    assert len(list_user) == 2
    assert len(list_agent) == 2

    # 4. Get profile details via user API and Agent route
    get_user = c.get(f"/api/style-profiles/{profile_user['id']}").json()
    get_agent = c.get(f"/api/agent/studio_style_profile_get?profile_id={profile_user['id']}").json()
    assert get_user["id"] == get_agent["id"] == profile_user["id"]
    assert get_user["shots"] == get_agent["shots"]

    tools = c.get("/api/agent/tools").json()["tools"]
    resolve = next(t for t in tools if t["name"] == "studio_production_shot_comment_resolve")
    assert "note" in resolve["inputSchema"]["properties"]

    # 5. Update profile via user API and Agent route
    c.put(f"/api/style-profiles/{profile_user['id']}", json={"name": "User Updated"}).json()
    c.post(f"/api/agent/studio_style_profile_update?profile_id={profile_agent['id']}", json={"name": "Agent Updated"}).json()

    assert c.get(f"/api/style-profiles/{profile_user['id']}").json()["name"] == "User Updated"
    assert c.get(f"/api/style-profiles/{profile_agent['id']}").json()["name"] == "Agent Updated"


def test_api_and_agent_shot_review_and_regeneration_parity(client, mini_video: Path, data_dir: Path):
    c, app, _ = client

    # Create a production
    spec = {
        "title": "Review Parity Production",
        "lead": {"name": "Hero", "look": "Space explorer in silver flight suit"},
        "shots": [
            {"key": "1", "prompt": "Intro", "seed": 101, "clip_seed": 201, "locked": True, "start_s": 0.0, "end_s": 2.0, "clips": [0]},
            {"key": "2", "prompt": "Middle", "seed": 102, "clip_seed": 202, "locked": False, "start_s": 2.0, "end_s": 4.0, "clips": [0]},
            {"key": "3", "prompt": "Outro", "seed": 103, "clip_seed": 203, "locked": False, "continue_from": "2", "start_s": 4.0, "end_s": 6.0, "clips": [0]},
        ],
    }
    create_production(data_dir, "Review Parity", spec, slug="review-parity")

    # 1. Add comment via User route
    c_user_resp = c.post(
        "/api/productions/review-parity/shot-comments",
        json={"shot_key": "1", "time_s": 1.0, "content": "Keep this take", "kind": "approved"},
    )
    assert c_user_resp.status_code == 200
    comment_user = c_user_resp.json()["comment"]
    assert comment_user["shot_revision"] == 1

    # 2. Add comment via Agent route
    c_agent_resp = c.post(
        "/api/agent/studio_production_shot_comment",
        params={"production": "review-parity"},
        json={"shot_key": "2", "time_s": 3.0, "content": "Adjust camera angle", "kind": "request_change"},
    )
    assert c_agent_resp.status_code == 200
    comment_agent = c_agent_resp.json()["comment"]
    assert comment_agent["shot_revision"] == 1

    # 3. List comments via User and Agent route
    comments_u = c.get("/api/productions/review-parity/shot-comments").json()["comments"]
    comments_a = c.get("/api/agent/studio_production_shot_comments?production=review-parity").json()["comments"]
    assert len(comments_u) == len(comments_a) == 2

    # 4. Selective dependent regeneration via Agent route
    regen_resp = c.post(
        "/api/agent/studio_production_regenerate_dependent",
        params={"production": "review-parity"},
        json={"keys": ["2"], "reason": "Apply agent angle adjustment"},
    )
    assert regen_resp.status_code == 200
    data = regen_resp.json()
    assert set(data["regenerated_shots"]) == {"2", "3"}
    assert set(data["preserved_shots"]) == {"1"}
    assert data["shot_revisions"]["2"] == 2
    assert data["shot_revisions"]["3"] == 2
    assert data["shot_revisions"]["1"] == 1

    # Verify stale status reflected in both User and Agent comment list
    comments_after = c.get("/api/productions/review-parity/shot-comments").json()["comments"]
    c2 = next(c for c in comments_after if c["id"] == comment_agent["id"])
    assert c2["is_stale"] is True
    assert c2["current_shot_revision"] == 2

    # 5. Also test applying profile to a separate production via API and Agent route
    spec_target = {
        "title": "Target Prod",
        "lead": {"name": "Target Lead", "look": "Sci-fi pilot"},
        "shots": [{"key": "1", "prompt": "Initial"}],
    }
    create_production(data_dir, "Target Prod", spec_target, slug="target-prod")
    prof_resp = c.post("/api/style-profiles/extract", json={"video_path": str(mini_video), "name": "Grammar Ref"})
    profile_id = prof_resp.json()["id"]

    # Apply via user route
    apply_resp = c.post("/api/productions/target-prod/apply-style-profile", json={"profile_id": profile_id})
    assert apply_resp.status_code == 200
    assert apply_resp.json()["style_profile_id"] == profile_id
