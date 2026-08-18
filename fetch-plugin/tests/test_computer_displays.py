from pathlib import Path
import sys

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import _computer_displays as displays  # noqa: E402


def test_host_desktop_opt_in_reads_env(monkeypatch) -> None:
    monkeypatch.setenv(displays.HOST_OPT_IN_ENV, "1")
    assert displays.host_desktop_opt_in() is True

    monkeypatch.setenv(displays.HOST_OPT_IN_ENV, "0")
    monkeypatch.setenv("HERMES_HOME", "/tmp/missing-hermes-home")
    assert displays.host_desktop_opt_in() is False


def test_host_desktop_opt_in_reads_dotenv(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(displays.HOST_OPT_IN_ENV, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(
        f"{displays.HOST_OPT_IN_ENV}=true\n", encoding="utf-8"
    )

    assert displays.host_desktop_opt_in() is True


def test_allocate_display_pins_profile(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "grok")

    first = displays.allocate_display()
    second = displays.allocate_display("inbox")

    assert first == 1
    assert second == 2
    assert displays.allocate_display("grok") == 1
    assert displays.vnc_port_for(1) == 5901
    assert displays.vnc_port_for(2) == 5902
    assert Path(tmp_path / "fetch-computer-displays.json").is_file()


def test_hermes_profile_names_include_disk_profiles(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    (tmp_path / "profiles" / "researcher").mkdir(parents=True)
    (tmp_path / "profiles" / "signal-monitor").mkdir()

    names = displays.hermes_profile_names()

    assert "default" in names
    assert "researcher" in names
    assert "signal-monitor" in names
