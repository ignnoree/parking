from helpers.light_profile import resolve_light_profile


def test_resolve_camera_profile():
    assert resolve_light_profile("low_light") == "low_light"


def test_resolve_invalid_uses_global(monkeypatch):
    import helpers.light_profile as lp

    monkeypatch.setattr(lp, "global_light_profile", lambda: "high_glare")
    assert resolve_light_profile("") == "high_glare"
