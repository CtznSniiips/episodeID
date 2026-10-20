"""Seed a demo EpisodeID library with invented shows and run the real scan and plan on
made-up dialogue — for the README screenshots. Writes fake (empty) video files.

    MEDIA_ROOT=/media CONFIG_DIR=/tmp/demo/config CACHE_DIR=/tmp/demo/cache \
        WHISPER_ENABLED=false python docs/screenshots/demo_seed.py
    MEDIA_ROOT=/media CONFIG_DIR=/tmp/demo/config CACHE_DIR=/tmp/demo/cache \
        uvicorn app.main:app --port 8699 &
    node docs/screenshots/demo_shots.js docs/screenshots      # needs playwright
"""
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import autocheck, db, pipeline, references  # noqa: E402
from app.jobs import JobContext  # noqa: E402

random.seed(5)
MEDIA = Path(os.environ["MEDIA_ROOT"])
SYL = "ba be bo da di do fa fe ka ki ko la le lo ma me mo na ne no pa pe ra ri ro sa se so ta te to va vo za".split()


def text():
    return " ".join("".join(random.choice(SYL) for _ in range(random.randint(1, 3))) for _ in range(1100))


TITLES: dict[str, str] = {}
CONTENT: dict[str, list] = {}   # absolute path → [text, text…] (episodes in the file)
SERIES: list[dict] = []


def show(tvdb, name, year, seasons, files, refs_missing=(), title_cards=None):
    root = MEDIA / "tv" / f"{name} ({year})"
    eps, texts = [], {}
    for sn, titles in seasons.items():
        for i, t in enumerate(titles, 1):
            code = f"S{sn:02d}E{i:02d}"
            eps.append({"season": sn, "episode": i, "title": t, "runtime": 11,
                        "aired": f"{2014 + sn}-0{1 + i // 10}-{1 + i % 28:02d}"})
            texts[code] = text()
            TITLES[code] = t
    sid = db.add_series(tvdb, name, str(year), str(root), None)
    db.update_series(sid, episodes=eps, episodes_updated=db.now(), options={"title_cards": "off"})
    for code, t in texts.items():
        if code not in refs_missing:
            references._save(tvdb, code, {"source": random.choice(["fandom", "opensubtitles"]), "text": t,
                                          "via": "IMDb", "page": f"{TITLES.get(code, code)}/Transcript"})
    for rel, inside in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0" * random.randint(150, 400) * 1024 * 4)
        CONTENT[str(p)] = [texts[c] if c in texts else text() for c in inside]
    SERIES.append({"id": sid, "title_cards": title_cards, "name": name})
    return sid, root


def fake_dialogue(v, ctx=None, allow_whisper=True):
    parts = CONTENT[str(v)]
    cues, t0 = [], 0.0
    for txt in parts:
        w = txt.split()
        for i in range(len(w) // 8):
            cues.append([t0 + i * 6.0, t0 + i * 6.0 + 5, " ".join(w[i * 8:i * 8 + 8])])
        t0 += 660.0
    return {"source": "embedded", "duration": t0, "cues": cues}


pipeline.get_dialogue = fake_dialogue
pipeline.cached_dialogue = lambda v: fake_dialogue(v) if str(v) in CONTENT else None
db.init()

LK = "The Lighthouse Keepers"
s1 = ["The Lost Kite", "Foghorn Blues", "Gull Trouble", "The Night-Light", "Low Tide", "Message in a Bottle",
      "The Big Storm (1)", "The Big Storm (2)", "Shell Collectors", "Pip's Birthday", "Harbour Lights", "Starfish Day"]
s2 = ["New Lamp", "Captain Brine", "The Map Room", "Whale Song", "Lantern Parade", "Seaweed Soup",
      "Rocky Point", "The Sandcastle Contest"]
s3 = ["Back to the Bay", "Pebble Hunt", "The Ferry", "Cloud Watching", "Midnight Swim", "Buoy Oh Buoy"]
n = lambda s, e, t, q="WEBDL-1080p": f"Season {s}/{LK} - S{s:02d}E{e:02d} - {t} {q}.mkv"  # noqa: E731
files = [(n(1, 1, s1[0]), ["S01E01"]), (n(1, 2, s1[1]), ["S01E02"]),
         (n(1, 3, s1[2]), ["S01E04"]), (n(1, 4, s1[3]), ["S01E03"]),            # swapped names
         (n(1, 5, s1[4]), ["S01E05"]), (n(1, 6, s1[5]), ["S01E06"]),
         (f"Season 1/{LK} - S01E07E08 - The Big Storm WEBDL-1080p.mkv", ["S01E07", "S01E08"]),
         (n(1, 9, s1[8]), ["S01E09", "S01E10"]),                                # two episodes, one name
         (n(1, 11, s1[10]), ["S01E11"]), (n(1, 12, s1[11]), ["S01E12"]),
         (n(1, 2, s1[1], "HDTV-720p"), ["S01E02"]),                              # duplicate
         (n(2, 1, s2[0]), ["S02E01"]), (n(2, 2, s2[1]), ["S02E02"]), (n(2, 3, s2[2]), ["S02E03"]),
         (n(2, 4, s2[3]), ["S02E04"]), (n(2, 5, s2[4]), ["junk"]),               # unrecognisable
         (n(2, 6, s2[5]), ["S02E07"]), (n(2, 8, s2[7]), ["S02E08"]),
         (n(3, 1, s3[0]), ["S03E01"]), (n(3, 2, s3[1]), ["S03E02"]), (n(3, 3, s3[2]), ["S03E03"])]
lk, lk_root = show(91001, LK, 2015, {1: s1, 2: s2, 3: s3}, files, refs_missing={"S03E04", "S03E05", "S03E06"},
                   title_cards={"title_cards": "auto", "title_cards_status": {"has_cards": True, "probed": 6, "hits": 5,
                                                                              "window": [20, 140]}})

CC = "Captain Comet"
cc_t = {1: ["Launch Day", "Moon Dust", "The Orbit Race", "Comet Tail", "Space Picnic", "Robo Pal"],
        2: ["Mars Market", "Gravity Games", "Star Map", "The Long Night"]}
cc_files = [(f"Season {s}/{CC} - S{s:02d}E{e:02d} - {t} Bluray-1080p.mkv", [f"S{s:02d}E{e:02d}"])
            for s, ts in cc_t.items() for e, t in enumerate(ts, 1)]
show(91002, CC, 2019, cc_t, cc_files,
     title_cards={"title_cards": "auto", "title_cards_status": {"has_cards": False, "probed": 6, "hits": 0}})

MP = "Moss & Pebble"
mp_t = {1: ["Rainy Day", "The Snail Race", "Puddle Jump", "Acorn Hunt", "Frost", "Mushroom Ring", "Bramble", "Owl Hoot"]}
mp_files = [(f"Season 1/{MP} - S01E{e:02d} - {t} WEBRip-720p.mkv", [f"S01E{e:02d}"]) for e, t in enumerate(mp_t[1], 1)]
mp, _ = show(91003, MP, 2021, mp_t, mp_files[:6], refs_missing={"S01E05", "S01E06", "S01E07", "S01E08"})
db.update_series(mp, options={})

NB = "The Night Bakery"
nb_t = {1: ["Flour Power", "Crumbs", "Rise and Shine", "The Sourdough Thief", "Midnight Muffins"]}
nb_files = [(f"Season 1/{NB} - S01E{e:02d} - {t} WEBDL-1080p.mkv", [f"S01E{e:02d}"]) for e, t in enumerate(nb_t[1], 1)]
nb, nb_root = show(91004, NB, 2018, nb_t, nb_files)
bk = MEDIA / "tv" / "_episodeid_backup" / f"{NB} (2018)" / "duplicates" / "Season 1"
bk.mkdir(parents=True)
for e in (2, 4, 5):
    (bk / f"{NB} - S01E{e:02d} - x HDTV-720p.mkv").write_bytes(b"\0" * 1024 * 1024 * (180 + e * 37))

for s in SERIES:
    if s["name"] == MP:
        continue  # not scanned yet
    pipeline.job_scan(JobContext(db.add_job("scan", s["id"], {}), s["id"]), {})
    if s["title_cards"]:
        o = db.get_series(s["id"])["options"]
        o.update(s["title_cards"])
        db.update_series(s["id"], options=o)
    autocheck.record(db.get_series(s["id"]))
# A new file since the last scan, for the file-check flag.
(nb_root / "Season 1" / f"{NB} - S01E06 - Bread Moon WEBDL-1080p.mkv").write_bytes(b"\0" * 1000)
autocheck.check(db.get_series(nb), submit=False)
db.conn().execute("DELETE FROM jobs")
from app import config  # noqa: E402
config.save_settings({"tvdb_api_key": "demo", "opensubtitles_api_key": "demo", "sonarr_url": "http://sonarr:8989",
                      "sonarr_api_key": "demo", "whisper_enabled": True})
print("seeded", [s["id"] for s in SERIES])
