from pathlib import Path

def test_live_probe():
    # Find root directory based on test file location
    root_dir = Path(__file__).parent.parent
    probe_file = root_dir / "live_probe.txt"
    
    assert probe_file.exists(), f"File {probe_file} does not exist"
    assert probe_file.read_text(encoding="utf-8").strip() == "hello from live probe"
