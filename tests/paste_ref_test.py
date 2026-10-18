"""Pasting a reference: a transcript copied from a web page (speaker names, stage
directions, page junk — even with the lines run together) or a subtitle file's text.
Saved like an upload; speaker names don't end up in the dialogue."""
import os
import random
import shutil
import sys
from pathlib import Path

W = Path(os.environ.get("EPID_TEST_DIR", "/tmp/epid_paste"))
if W.exists():
    shutil.rmtree(W)
os.environ.update(MEDIA_ROOT=str(W / "media"), CONFIG_DIR=str(W / "config"), CACHE_DIR=str(W / "cache"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402
from app import db, main, references  # noqa: E402

(W / "media" / "Show").mkdir(parents=True)
db.init()
sid = db.add_series(5, "The Office", "2005", str(W / "media" / "Show"), None)
c = TestClient(main.app)
random.seed(1)
words = lambda n: " ".join(random.choice("paper sales dunder branch beets stapler jello client party".split())  # noqa: E731
                           for _ in range(n))

web = ("Home | Episodes | Characters | Quotes\nThe Office Transcript: Launch Party\nAdvertisement\n" +
       "\n".join(f"{random.choice(['MICHAEL', 'DWIGHT', 'Jim', 'Pam Beesly'])}: {words(12)} [laughs]" for _ in range(60)) +
       "\nShare this page")
r = c.post(f"/api/series/{sid}/references/S04E05/text", json={"text": web}).json()
print(" ", r)
assert r["format"] == "transcript" and r["words"] > 500 and not r["warning"]
ref = references.load_refs(5)["S04E05"]
assert "MICHAEL" not in ref["text"] and "Pam Beesly" not in ref["text"] and "laughs" not in ref["text"]
assert ref["source"].startswith("manual:")

# Run together into one line, as some pages copy.
one = " ".join(f"{random.choice(['MICHAEL', 'DWIGHT', 'Jim'])}: {words(10)}" for _ in range(60))
r = c.post(f"/api/series/{sid}/references/S04E06/text", json={"text": one}).json()
assert "DWIGHT" not in references.load_refs(5)["S04E06"]["text"], r

srt = "\n".join(f"{i}\n00:{i // 60:02d}:{i % 60:02d},000 --> 00:{i // 60:02d}:{i % 60:02d},900\n{words(8)}\n" for i in range(1, 80))
r = c.post(f"/api/series/{sid}/references/S04E07/text", json={"text": srt}).json()
assert r["format"] == "SRT subtitle" and "-->" not in references.load_refs(5)["S04E07"]["text"]

# Replaces an earlier reference for the episode, whatever its format.
r = c.post(f"/api/series/{sid}/references/S04E07/text", json={"text": web}).json()
files = sorted(p.name for p in references.manual_dir(5).iterdir())
print("  files:", files)
assert files == ["S04E05.txt", "S04E06.txt", "S04E07.txt"]

assert c.post(f"/api/series/{sid}/references/S04E08/text", json={"text": "too short"}).status_code == 400
assert c.post(f"/api/series/{sid}/references/S04E08/text", json={"text": "| a | b | c |\n" * 40}).status_code == 400
assert not any(p.name.startswith(("_check", "S04E08")) for p in references.manual_dir(5).iterdir())
short = "\n".join(f"MICHAEL: {words(8)}" for _ in range(12))
r = c.post(f"/api/series/{sid}/references/S04E09/text", json={"text": short}).json()
assert r["warning"], r
print("PASS")
