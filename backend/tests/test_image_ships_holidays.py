"""The backend image must ship data/nse_holidays.json next to symbols.json —
inside Docker the holiday file resolved to /data/nse_holidays.json, which the
Dockerfile never copied, so every holiday check was silently empty."""
from pathlib import Path


def test_dockerfile_copies_the_holiday_file():
    for cand in (Path(__file__).resolve().parents[2] / "docker" / "Dockerfile.backend",
                 Path("/docker/Dockerfile.backend")):
        if cand.exists():
            text = cand.read_text(encoding="utf-8")
            assert "COPY data/symbols.json /data/symbols.json" in text
            assert "COPY data/nse_holidays.json /data/nse_holidays.json" in text
            return
    import pytest
    pytest.skip("Dockerfile not mounted in this test container")
