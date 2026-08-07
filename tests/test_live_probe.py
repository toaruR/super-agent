from pathlib import Path

def test_live_probe():
    probe_file = Path(__file__).parent.parent / "live_probe.txt"
    assert probe_file.exists(), "live_probe.txt does not exist"
    assert probe_file.read_text(encoding="utf-8").strip() == "hello from live probe"
