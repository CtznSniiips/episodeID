"""Dialogue matching.

Each file's dialogue is cut into overlapping windows (default 90 s every 45 s).
Every window is scored against every episode's reference text with TF-IDF
cosine similarity. A Viterbi pass then assigns one episode per window with a
penalty for switching, which yields clean segments even when individual windows
are noisy. Each segment is finally re-scored as a whole for its confidence.

This handles paraphrased subtitles, Whisper transcription errors, two-episode
files (including pairs in the wrong order), and files that are simply
mislabelled across seasons.
"""
from __future__ import annotations

import re

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

EP_RE = re.compile(r"(?i)\bS(\d{1,3})((?:[ ._-]*E\d{1,4})+)(?:-(\d{1,4})\b)?")
EP_X_RE = re.compile(r"(?i)\b(\d{1,2})x(\d{2,3})(?:-(\d{2,3}))?\b")


def parse_filename_episodes(name: str) -> list[str]:
    m = EP_RE.search(name)
    if m:
        season = int(m[1])
        nums = [int(x) for x in re.findall(r"(?i)E(\d+)", m[2])]
        if m[3]:
            nums.append(int(m[3]))
        if len(nums) == 2 and nums[1] > nums[0] + 1 and "-" in m.group(0):
            nums = list(range(nums[0], nums[1] + 1))  # S01E01-E03 → 1,2,3
        return [f"S{season:02d}E{n:02d}" for n in dict.fromkeys(nums)]
    m = EP_X_RE.search(name)
    if m:
        season = int(m[1])
        nums = [int(m[2])] + ([int(m[3])] if m[3] else [])
        return [f"S{season:02d}E{n:02d}" for n in nums]
    return []


class Matcher:
    def __init__(self, refs: dict[str, str], settings: dict):
        if not refs:
            raise ValueError("No reference texts available — fetch references first.")
        self.codes = sorted(refs)
        self.s = settings
        n = len(self.codes)
        self.vec = TfidfVectorizer(
            lowercase=True, strip_accents="unicode", sublinear_tf=True,
            ngram_range=(1, 2), min_df=1, max_df=0.6 if n >= 10 else 1.0,
            token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z']+\b", stop_words="english")
        self.R = self.vec.fit_transform([refs[c] for c in self.codes])  # l2-normalised

    def score_text(self, text: str) -> np.ndarray:
        return (self.vec.transform([text]) @ self.R.T).toarray()[0]

    def top(self, scores: np.ndarray, k: int = 3) -> list[dict]:
        idx = np.argsort(-scores)[:k]
        return [{"code": self.codes[i], "score": round(float(scores[i]), 4)} for i in idx]

    # ------------------------------------------------------------------
    def match_file(self, cues: list, duration: float) -> dict:
        win = float(self.s["window_seconds"])
        step = float(self.s["window_step_seconds"])
        if not cues:
            return {"segments": [], "whole": []}
        end_t = max(duration, cues[-1][1])
        starts = np.arange(0, max(end_t - win * 0.5, step), step)
        windows, texts = [], []
        for st in starts:
            en = st + win
            t = " ".join(c[2] for c in cues if c[1] > st and c[0] < en)
            windows.append((float(st), float(min(en, end_t))))
            texts.append(t)

        whole_text = " ".join(c[2] for c in cues)
        whole = self.top(self.score_text(whole_text), 5)

        S = (self.vec.transform(texts) @ self.R.T).toarray()  # windows × episodes
        words = np.array([len(t.split()) for t in texts])
        S[words < 8] = 0.0  # silence / music-only windows carry no evidence

        labels = self._viterbi(S)
        runs = self._runs(labels, windows)
        min_seg = float(self.s["min_segment_seconds"])
        kept = [r for r in runs if r["label"] is not None and
                (r["end"] - r["start"] >= min_seg or r["end"] - r["start"] >= 0.35 * end_t)]
        # merge neighbouring runs that ended up with the same label
        merged: list[dict] = []
        for r in kept:
            if merged and merged[-1]["label"] == r["label"]:
                merged[-1]["end"] = r["end"]
            else:
                merged.append(dict(r))

        segments = []
        for r in merged:
            seg_text = " ".join(c[2] for c in cues if c[1] > r["start"] and c[0] < r["end"])
            sc = self.score_text(seg_text)
            alts = self.top(sc, 4)
            best = alts[0]
            second = alts[1] if len(alts) > 1 else {"code": None, "score": 0.0}
            margin = best["score"] - second["score"]
            high = (best["score"] >= self.s["min_score"] and margin >= self.s["min_margin"]
                    and best["score"] >= 1.4 * max(second["score"], 1e-6))
            segments.append({
                "start": round(r["start"], 1), "end": round(r["end"], 1),
                "code": best["code"], "score": best["score"],
                "margin": round(margin, 4), "confidence": "high" if high else "low",
                "alternatives": alts[1:],
                "words": len(seg_text.split()),
            })
        # Collapse adjacent segments that re-scored to the same episode.
        out: list[dict] = []
        for sg in segments:
            if out and out[-1]["code"] == sg["code"]:
                out[-1]["end"] = sg["end"]
                out[-1]["score"] = max(out[-1]["score"], sg["score"])
            else:
                out.append(sg)
        return {"segments": out, "whole": whole}

    def _viterbi(self, S: np.ndarray) -> list[int | None]:
        n_w, n_e = S.shape
        if n_w == 0:
            return []
        none_score = float(self.s["min_score"]) * 0.6
        # column n_e is "no episode"
        E = np.hstack([S, np.full((n_w, 1), none_score)])
        penalty = 0.22
        score = E[0].copy()
        back = np.zeros((n_w, n_e + 1), dtype=np.int64)
        for t in range(1, n_w):
            best_prev = int(np.argmax(score))
            switch = score[best_prev] - penalty
            stay = score
            use_switch = switch > stay
            back[t] = np.where(use_switch, best_prev, np.arange(n_e + 1))
            score = np.where(use_switch, switch, stay) + E[t]
        path = [int(np.argmax(score))]
        for t in range(n_w - 1, 0, -1):
            path.append(int(back[t][path[-1]]))
        path.reverse()
        return [None if p == n_e else p for p in path]

    def _runs(self, labels, windows) -> list[dict]:
        runs: list[dict] = []
        for lab, (st, en) in zip(labels, windows):
            code = None if lab is None else self.codes[lab]
            if runs and runs[-1]["label"] == code:
                runs[-1]["end"] = en
            else:
                if runs:
                    # windows overlap; split the overlap between neighbours
                    mid = (runs[-1]["end"] + st) / 2 if runs[-1]["end"] > st else st
                    runs[-1]["end"] = mid
                    st = mid
                runs.append({"label": code, "start": st, "end": en})
        return runs


def classify(expected: list[str], segments: list[dict], text_source: str) -> tuple[str, str]:
    if text_source == "none":
        return "NO_TEXT", "No subtitles and no transcription available"
    if not segments:
        return "NO_MATCH", "Dialogue did not match any reference"
    detected = [s["code"] for s in segments]
    if any(s["confidence"] != "high" for s in segments):
        return "LOW_CONFIDENCE", "At least one segment is a weak match"
    if detected == expected:
        return "OK", ""
    if len(set(detected)) == len(detected) and set(detected) == set(expected):
        return "OK_ORDER_DIFFERS", "Same episodes, different order inside the file"
    if not expected:
        return "MISMATCH", "Filename has no SxxEyy; identified from dialogue"
    return "MISMATCH", ""
