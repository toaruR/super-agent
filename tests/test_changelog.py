from pathlib import Path

def test_changelog_exists():
    root_dir = Path(__file__).parent.parent
    changelog_file = root_dir / "CHANGELOG.md"
    
    assert changelog_file.exists(), f"File {changelog_file} does not exist"
    assert changelog_file.is_file(), f"{changelog_file} is not a file"
    assert len(changelog_file.read_text(encoding="utf-8").strip()) > 0, f"{changelog_file} is empty"
