import pytest

from app import models
from tests.conftest import register_and_login

GOOD = {"enabled": True, "level": "info", "title": "Maintenance", "text": "Back at noon."}


@pytest.mark.parametrize("ch", ["‮", "‭", "⁦", "​", "‍", "⁠", "﻿", " ", "­"])
def test_banner_rejects_bidi_and_invisible_characters(make_client, db_session, ch):
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    for field in ("title", "text"):
        assert admin.put("/admin/banner", json={**GOOD, field: f"Sign in at x{ch}y"}).status_code == 422
    assert db_session.query(models.PlatformSetting).filter(models.PlatformSetting.key.like("banner_%")).count() == 0
