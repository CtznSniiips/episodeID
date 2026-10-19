"""The show's logo isn't an episode title: "THOMAS & FRIENDS" on screen must not be
read as "Thomas' Animal Friends" (pieced together as THOMAS … FRIENDS) and beat the
real card, "PANICKY PERCY", a minute later. A title that is only the show's name
words still counts when it's read exactly."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import titlecard as T  # noqa: E402

EPS = [{"season": 24, "episode": 23, "title": "Thomas' Animal Friends"},
       {"season": 23, "episode": 12, "title": "Panicky Percy"},
       {"season": 23, "episode": 4, "title": "The Other Big Engine"},
       {"season": 5, "episode": 1, "title": "Thomas and Friends"}]
names = T.series_names({"name": "Thomas & Friends", "path": "/tv/Thomas the Tank Engine & Friends"})
idx = T.TitleIndex(EPS, series_names=names)
plain = T.TitleIndex(EPS[:3])

LOGO = [["THOMAS", .995, .144, .3], ["&FRIENDS", .992, .106, .5]]
LOGO2 = [["THOMAS", .995, .133, .2], ["&FRIENDS", .989, .103, .35],
         ["BIGWORLD!", .98, .111, .6], ["BIGADVENTURES!", .989, .103, .75]]
assert plain.match_frame(LOGO, 29)["code"] == "S24E23", "without the series name it's misread (the bug)"
assert idx.match_frame(LOGO, 29) is None
assert idx.match_frame(LOGO2, 30) is None
assert idx.match_frame([["THOMAS&FRIENDS", .99, .12, .3]], 29) is None, "spaces lost"
assert idx.match_frame([["THOMAS THE TANK ENGINE", .99, .12, .3]], 29) is None
print("PASS (logo ignored)")

assert idx.match_frame([["PANICKYPERCY", .988, .078, .2], ["WRITTENBY", .997, .058, .4]], 70)["code"] == "S23E12"
assert idx.match_frame([["THOMAS AND FRIENDS", .99, .1, .3]], 70)["code"] == "S05E01", "exact title still counts"
assert idx.match_frame([["THOMAS' ANIMAL FRIENDS", .99, .1, .3]], 70)["code"] == "S24E23"
print("PASS (real titles still read)")

# Even when a pieced-together read gets through, a title read as written in the same
# stretch wins over it, though its text is shorter.
gap = {"code": "S24E23", "text": "THOMAS … &FRIENDS", "score": 0.684, "partial": False, "gapped": True,
       "time": 29.0, "frames": 4}
real = {"code": "S23E12", "text": "PANICKYPERCY", "score": 1.0, "partial": False, "time": 70.0, "frames": 4}
kept = T._one_per_stretch([gap, real], ["S23E04"])
assert [c["code"] for c in kept] == ["S23E12"], kept
print("PASS (direct read wins the stretch)")
