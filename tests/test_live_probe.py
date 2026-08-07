from pathlib import Path

def test_live_probe_file():
    root_dir = Path(__file__).resolve().parent.parent
    probe_file = root_dir / "live_probe.txt"
    assert probe_file.exists(), "live_probe.txt does not exist"
    assert probe_file.read_text().strip() == "hello from live probe"
