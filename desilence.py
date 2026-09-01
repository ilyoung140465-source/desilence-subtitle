#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
desilence.py - TTS 오디오의 무음 구간을 자동 감지/삭제하고 SRT 자막 싱크를 맞춰주는 도구.

의존성: ffmpeg/ffprobe (PATH), numpy
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".mp4"}


# --------------------------------------------------------------------------
# ffmpeg 래퍼
# --------------------------------------------------------------------------

def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        sys.exit(f"[!] {name} 를 PATH 에서 찾을 수 없습니다. ffmpeg 를 설치하고 PATH 에 등록해 주세요.")
    return path


def probe(path: Path) -> tuple[int, int, float]:
    """(sample_rate, channels, duration_sec)"""
    out = subprocess.run(
        [
            _tool("ffprobe"), "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels:format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        sys.exit(f"[!] ffprobe 실패: {path}\n{out.stderr.strip()}")
    info = json.loads(out.stdout or "{}")
    streams = info.get("streams") or []
    if not streams:
        sys.exit(f"[!] 오디오 스트림이 없습니다: {path}")
    sr = int(streams[0].get("sample_rate") or 44100)
    ch = int(streams[0].get("channels") or 1)
    dur = float((info.get("format") or {}).get("duration") or 0.0)
    return sr, ch, dur


def decode(path: Path, sr: int, ch: int) -> np.ndarray:
    """오디오를 int16 PCM (n_samples, channels) 로 디코딩."""
    proc = subprocess.run(
        [
            _tool("ffmpeg"), "-v", "error", "-i", str(path),
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ar", str(sr), "-ac", str(ch), "-",
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.exit(f"[!] 디코딩 실패: {path}\n{proc.stderr.decode('utf-8', 'replace').strip()}")
    data = np.frombuffer(proc.stdout, dtype="<i2")
    if ch > 1:
        data = data[: (len(data) // ch) * ch].reshape(-1, ch)
    else:
        data = data.reshape(-1, 1)
    return data


def encode(pcm: np.ndarray, sr: int, ch: int, dst: Path, bitrate: str = "192k") -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    ext = dst.suffix.lower()
    if ext == ".wav":
        codec = ["-c:a", "pcm_s16le"]
    elif ext == ".mp3":
        codec = ["-c:a", "libmp3lame", "-b:a", bitrate]
    else:
        codec = []  # ffmpeg 기본 코덱에 맡김
    proc = subprocess.run(
        [
            _tool("ffmpeg"), "-v", "error", "-y",
            "-f", "s16le", "-ar", str(sr), "-ac", str(ch), "-i", "-",
            *codec, str(dst),
        ],
        input=pcm.astype("<i2").tobytes(), capture_output=True,
    )
    if proc.returncode != 0:
        sys.exit(f"[!] 인코딩 실패: {dst}\n{proc.stderr.decode('utf-8', 'replace').strip()}")


# --------------------------------------------------------------------------
# 무음 감지
# --------------------------------------------------------------------------

HOP_MS = 10.0


def frame_db(mono: np.ndarray, sr: int, hop_ms: float = HOP_MS) -> tuple[np.ndarray, int]:
    """프레임별 RMS 를 dBFS 로. (db_array, hop_samples)"""
    hop = max(1, int(round(sr * hop_ms / 1000.0)))
    n = len(mono) // hop
    if n == 0:
        return np.array([-120.0], dtype=np.float32), hop
    frames = mono[: n * hop].reshape(n, hop).astype(np.float32) / 32768.0
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    return 20.0 * np.log10(rms + 1e-10), hop


def auto_threshold(db: np.ndarray) -> float:
    noise = float(np.percentile(db, 10))
    speech = float(np.percentile(db, 90))
    if speech - noise < 12.0:          # 무음이 거의 없는 파일
        return -45.0
    thr = max(noise + 6.0, speech - 30.0)
    return float(min(-24.0, max(-70.0, thr)))


@dataclass
class Analysis:
    sr: int
    ch: int
    n_samples: int
    threshold_db: float
    removes: list[tuple[int, int]] = field(default_factory=list)   # 삭제할 [start,end) 샘플
    keeps: list[tuple[int, int]] = field(default_factory=list)     # 남길 [start,end) 샘플

    @property
    def orig_sec(self) -> float:
        return self.n_samples / self.sr

    @property
    def new_samples(self) -> int:
        return sum(e - s for s, e in self.keeps)

    @property
    def new_sec(self) -> float:
        return self.new_samples / self.sr


def analyze(pcm: np.ndarray, sr: int, *, threshold: float | None,
            min_silence: float, keep_silence: float,
            edge_silence: float, trim_edges: bool) -> Analysis:
    mono = pcm.astype(np.float32).mean(axis=1) if pcm.shape[1] > 1 else pcm[:, 0].astype(np.float32)
    db, hop = frame_db(mono, sr)
    thr = auto_threshold(db) if threshold is None else float(threshold)

    quiet = db < thr
    n = len(quiet)

    # 연속된 무음 프레임 런 찾기
    runs: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if quiet[i]:
            j = i
            while j < n and quiet[j]:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1

    total = len(pcm)
    keep_s = int(round(keep_silence * sr))
    edge_s = int(round(edge_silence * sr))
    min_s = int(round(min_silence * sr))

    removes: list[tuple[int, int]] = []
    for fs, fe in runs:
        s = fs * hop
        e = total if fe >= n else fe * hop
        if e - s < min_s:
            continue
        at_head = fs == 0
        at_tail = fe >= n
        if at_head or at_tail:
            if not trim_edges:
                continue
            cs = s if at_head else s + edge_s
            ce = e if at_tail else e - edge_s
        else:
            cs, ce = s + keep_s, e - keep_s
        if ce - cs > 0:
            removes.append((cs, ce))

    # 남길 구간 계산
    keeps: list[tuple[int, int]] = []
    cur = 0
    for s, e in removes:
        if s > cur:
            keeps.append((cur, s))
        cur = e
    if cur < total:
        keeps.append((cur, total))
    if not keeps:
        keeps = [(0, total)]

    return Analysis(sr=sr, ch=pcm.shape[1], n_samples=total,
                    threshold_db=thr, removes=removes, keeps=keeps)


def render(pcm: np.ndarray, an: Analysis, fade_ms: float = 4.0) -> np.ndarray:
    """남길 구간만 이어붙이고, 이음새에 micro-fade 를 적용해 클릭음 방지."""
    parts = [pcm[s:e].astype(np.float32) for s, e in an.keeps]
    f = max(1, int(round(an.sr * fade_ms / 1000.0)))
    for idx, part in enumerate(parts):
        if len(part) < 2 * f:
            continue
        ramp = np.linspace(0.0, 1.0, f, dtype=np.float32)[:, None]
        if idx > 0:
            part[:f] *= ramp
        if idx < len(parts) - 1:
            part[-f:] *= ramp[::-1]
    out = np.concatenate(parts, axis=0) if parts else pcm.astype(np.float32)
    return np.clip(np.round(out), -32768, 32767).astype(np.int16)


# --------------------------------------------------------------------------
# 시간 매핑 (원본 시각 -> 편집 후 시각)
# --------------------------------------------------------------------------

class TimeMap:
    def __init__(self, an: Analysis):
        self.sr = an.sr
        self.starts = np.array([s for s, _ in an.keeps], dtype=np.int64)
        self.ends = np.array([e for _, e in an.keeps], dtype=np.int64)
        lens = self.ends - self.starts
        self.offsets = np.concatenate([[0], np.cumsum(lens)[:-1]]).astype(np.int64)
        self.total_new = int(lens.sum())

    def sample(self, s: int) -> int:
        idx = int(np.searchsorted(self.starts, s, side="right") - 1)
        if idx < 0:
            return 0
        if s >= self.ends[idx]:                      # 삭제된 구간 안 -> 이음새로 스냅
            return int(self.offsets[idx] + (self.ends[idx] - self.starts[idx]))
        return int(self.offsets[idx] + (s - self.starts[idx]))

    def __call__(self, t: float) -> float:
        return self.sample(int(round(t * self.sr))) / self.sr


# --------------------------------------------------------------------------
# SRT
# --------------------------------------------------------------------------

@dataclass
class Cue:
    start: float
    end: float
    text: str


TS_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def parse_ts(s: str) -> float:
    m = TS_RE.search(s)
    if not m:
        raise ValueError(f"타임코드 파싱 실패: {s!r}")
    h, mm, ss, ms = m.groups()
    return int(h) * 3600 + int(mm) * 60 + int(ss) + int(ms.ljust(3, "0")) / 1000.0


def fmt_ts(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


SCRIPT_EXTS = {".txt", ".md"}


def parse_script_text(text: str) -> list[str]:
    """
    대본 텍스트를 문장 단위로 나눈다.

    SRT 와 달리 시각 정보가 없으므로, 문장 경계만 뽑아낸다.
    시각은 나중에 음성 인식으로 채운다.
    """
    out: list[str] = []
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    for line in norm.split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.fullmatch(r"\d+", line):          # SRT 를 잘못 넣은 경우의 번호 줄
            continue
        if "-->" in line:                       # 타임코드 줄
            continue
        for part in re.split(r"(?<=[.!?…])\s+", line):
            part = part.strip()
            if part:
                out.append(part)
    return out


def parse_srt(path: Path) -> list[Cue]:
    return parse_srt_text(read_text(path))


def parse_srt_text(text: str) -> list[Cue]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for block in re.split(r"\n{2,}", text.strip()):
        lines = [l for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        ti = 0 if "-->" in lines[0] else 1
        if ti >= len(lines) or "-->" not in lines[ti]:
            continue
        a, _, b = lines[ti].partition("-->")
        try:
            start, end = parse_ts(a), parse_ts(b)
        except ValueError:
            continue
        body = " ".join(l.strip() for l in lines[ti + 1:]).strip()
        if body:
            cues.append(Cue(start, end, body))
    return cues


def validate_cues(cues: list[Cue], total: float, min_dur: float = 0.10) -> list[str]:
    """자막 편집 프로그램(캡컷 등)에서 문제가 될 만한 항목을 점검한다."""
    issues: list[str] = []
    short = [i for i, c in enumerate(cues, 1) if c.end - c.start < min_dur]
    if short:
        issues.append(f"{len(short)}개 자막이 {min_dur:.2f}초보다 짧습니다 (#{short[0]} 등)")
    bad = [i for i, c in enumerate(cues, 1) if c.end <= c.start]
    if bad:
        issues.append(f"{len(bad)}개 자막의 끝이 시작보다 빠르거나 같습니다 (#{bad[0]} 등)")
    over = [i for i in range(len(cues) - 1) if cues[i].end > cues[i + 1].start + 1e-4]
    if over:
        issues.append(f"{len(over)}쌍이 시간상 겹칩니다 — 캡컷에서 자막 트랙이 나뉘어 표시됩니다")
    empty = [i for i, c in enumerate(cues, 1) if not c.text.strip()]
    if empty:
        issues.append(f"{len(empty)}개 자막이 비어 있습니다 (#{empty[0]} 등)")
    late = [i for i, c in enumerate(cues, 1) if c.end > total + 0.05]
    if late:
        issues.append(f"{len(late)}개 자막이 오디오 길이({total:.2f}s)를 넘습니다 (#{late[0]} 등)")
    if any(("\n" in c.text or "\r" in c.text) for c in cues):
        issues.append("자막 안에 줄바꿈이 남아 있습니다")
    return issues


def write_srt(cues: list[Cue], path: Path) -> None:
    blocks = [f"{i}\n{fmt_ts(c.start)} --> {fmt_ts(c.end)}\n{c.text}\n"
              for i, c in enumerate(cues, 1)]
    # BOM 을 붙여 저장 — 윈도우 프로그램(캡컷 포함)이 인코딩을 확실히 인식한다
    path.write_text("\n".join(blocks), encoding="utf-8-sig")


# --------------------------------------------------------------------------
# 자막 분할 (기본 10자, 단어 안 끊기게)
# --------------------------------------------------------------------------

def _balance(words: list[str], max_chars: int, hard_max: int) -> list[str]:
    """단어를 유지한 채, 줄 길이가 max_chars 에 고르게 가깝도록 DP 로 분할."""
    n = len(words)
    if n == 0:
        return []
    pre = [0] * (n + 1)
    for k, w in enumerate(words):
        pre[k + 1] = pre[k] + len(w)

    INF = float("inf")
    cost = [INF] * (n + 1)
    back = [0] * (n + 1)
    cost[0] = 0.0
    for i in range(1, n + 1):
        for j in range(i - 1, -1, -1):
            width = pre[i] - pre[j] + (i - j - 1)          # 공백 포함 길이
            if width > hard_max and j < i - 1:
                break                                       # 더 넓어지기만 하므로 중단
            if width <= max_chars:
                c = float((max_chars - width) ** 2)
            elif width <= hard_max:
                c = float((2 * (width - max_chars)) ** 2)
            else:
                c = 1e4                                     # 단어 하나가 이미 너무 긴 경우
            if j > 0:
                c += break_score(words[j - 1], words[j])     # 뜻이 끊기지 않는 자리 우선
            if cost[j] + c < cost[i]:
                cost[i] = cost[j] + c
                back[i] = j

    lines: list[str] = []
    i = n
    while i > 0:
        j = back[i]
        lines.append(" ".join(words[j:i]))
        i = j
    return lines[::-1]


_HANGUL = r"가-힣"
BREAK = "\u241f"          # 자막을 반드시 나눠야 하는 지점 (화면에는 나오지 않음)


def clean_text(s: str) -> str:
    """
    TTS 대본 표기를 화면용으로 다듬는다.
    타입캐스트는 쉼을 '-' 로 표시하는데, 그대로 두면 화면에 '그랬는데요-' 처럼 나온다.
    """
    # 타입캐스트는 '-' 로 쉼을 표시한다. 화면에는 안 보이게 지우되,
    # '여기서 쉰다' 는 정보는 자막을 나눌 지점(BREAK)으로 살린다.
    s = re.sub(rf"(?<=[{_HANGUL}])\s*-\s*(?=[{_HANGUL}])", BREAK, s)
    s = re.sub(rf"(?<=[{_HANGUL}])\s*-\s*(?=$|[\s])", BREAK, s)
    s = re.sub(rf"(?<=[{_HANGUL}])(?=\d)", " ", s)                    # 먹고160 → 먹고 160
    # 숫자 뒤 한글은 조사인 경우가 많아 붙여 둔다 (150까지 를 쪼개지 않음)
    s = re.sub(rf"[ 	]*{BREAK}[ 	]*", BREAK, s)
    s = re.sub(r"[ 	]{2,}", " ", s)
    return s.strip()


def split_text(text: str, max_chars: int = 10, hard_max: int | None = None) -> list[str]:
    """공백 기준 단어를 유지하며 max_chars 근처로 균등 분할."""
    if hard_max is None:
        hard_max = max_chars + 4
    # 쉼 표시가 있으면 그 지점에서 반드시 끊는다 (실제 오디오에도 쉼이 있다)
    if BREAK in text:
        out: list[str] = []
        for part in text.split(BREAK):
            if part.strip():
                out += split_text(part, max_chars, hard_max)
        return out or [text.replace(BREAK, " ").strip()]
    words = [w for w in re.split(r"\s+", text.strip()) if w]

    chunks: list[str] = []
    buf: list[str] = []
    for w in words:
        if len(w) > hard_max:            # 한 단어가 이미 너무 길면 그것만 강제로 자름
            chunks += _balance(buf, max_chars, hard_max)
            buf = []
            while len(w) > hard_max:
                chunks.append(w[:max_chars])
                w = w[max_chars:]
            if w:
                buf.append(w)
        else:
            buf.append(w)
    chunks += _balance(buf, max_chars, hard_max)
    return chunks or [text.strip()]


def weight(s: str) -> float:
    """발화 길이 가중치: 공백 제외 글자 수 (한글 = 음절 수 근사)."""
    return max(1.0, float(len(re.sub(r"\s+", "", s))))


def snap_boundary(t: float, db: np.ndarray, hop_sec: float, window: float,
                  lo: float, hi: float) -> float:
    """t 주변 ±window 안에서 에너지가 가장 낮은 지점으로 경계를 스냅."""
    if window <= 0 or len(db) == 0:
        return t
    a, b = max(lo, t - window), min(hi, t + window)
    if b <= a:
        return t
    ia = max(0, int(a / hop_sec))
    ib = min(len(db) - 1, int(b / hop_sec))
    if ib <= ia:
        return t
    return (ia + int(np.argmin(db[ia:ib + 1]))) * hop_sec


EDGE_SNAP = 0.5   # 파일 앞뒤 이 정도 틈은 자막을 0 / 끝까지 늘려서 메움


def resync(cues: list[Cue], tmap: TimeMap, *, max_chars: int, min_cue: float,
           fill: bool, new_db: np.ndarray | None, snap_window: float,
           total_new: float) -> list[Cue]:
    out: list[Cue] = []
    hop_sec = HOP_MS / 1000.0
    for c in cues:
        ns, ne = tmap(c.start), tmap(c.end)
        if ne - ns < 0.05:                          # 통째로 무음이었던 자막
            continue
        chunks = split_text(c.text, max_chars)
        ws = [weight(x) for x in chunks]
        total_w = sum(ws)
        span = ne - ns
        n = len(chunks)

        # 1) 글자 수 비례로 경계 후보 배치
        bounds = [ns]
        acc = 0.0
        for w in ws[:-1]:
            acc += w
            bounds.append(ns + span * acc / total_w)
        bounds.append(ne)

        # 2) 에너지 최저점으로 미세 조정
        if new_db is not None and snap_window > 0:
            for i in range(1, n):
                lo, hi = bounds[i - 1] + 0.12, bounds[i + 1] - 0.12
                if hi > lo:
                    bounds[i] = snap_boundary(bounds[i], new_db, hop_sec, snap_window, lo, hi)
            bounds = sorted(bounds)

        # 3) 최소 노출 시간 확보 (자막 구간을 벗어나지 않는 선에서)
        if span >= n * min_cue:
            for i in range(1, n + 1):
                if bounds[i] - bounds[i - 1] < min_cue:
                    bounds[i] = bounds[i - 1] + min_cue
            if bounds[n] > ne:
                bounds[n] = ne
                for i in range(n - 1, 0, -1):
                    bounds[i] = min(bounds[i], bounds[i + 1] - min_cue)
        else:                                       # 너무 빠듯하면 그냥 균등 분할
            bounds = [ns + span * i / n for i in range(n + 1)]

        for i, chunk in enumerate(chunks):
            out.append(Cue(bounds[i], bounds[i + 1], chunk))

    if fill:
        apply_fill(out, total_new)
    elif out:
        for i in range(len(out) - 1):
            if out[i].end > out[i + 1].start:
                out[i].end = out[i + 1].start
        out[-1].end = min(max(out[-1].end, out[-1].start + 0.2), total_new)
    return out


def snap_starts(cues: list[Cue], db: np.ndarray, window: float,
                total_new: float, min_gap: float = 0.25) -> None:
    """
    각 자막 시작을 주변 ±window 안의 에너지 최저점(= 말 사이 틈)으로 당긴다.
    Whisper 정렬이 대략의 위치를 잡아주면, 여기서 정확한 발화 시작에 붙인다.
    """
    if db is None or window <= 0 or len(cues) < 2:
        return
    hop_sec = HOP_MS / 1000.0
    for i in range(1, len(cues)):
        lo = max(cues[i - 1].start + min_gap, cues[i].start - window)
        hi = min(total_new, cues[i].start + window)
        if i + 1 < len(cues):
            hi = min(hi, cues[i + 1].start - min_gap)
        if hi <= lo:
            continue
        cues[i].start = snap_boundary(cues[i].start, db, hop_sec, window, lo, hi)
    for i in range(len(cues) - 1):          # 순서·길이 유지
        if cues[i].end <= cues[i].start:
            cues[i].end = cues[i].start + 0.15
        if cues[i].end > cues[i + 1].start:
            cues[i].end = max(cues[i].start + 0.15, cues[i + 1].start)


def detect_onsets(db: np.ndarray, thr_db: float, margin: float = 8.0) -> np.ndarray:
    """말이 시작되는 지점(조용하다가 소리가 올라오는 프레임)을 찾는다."""
    T = thr_db + margin
    loud = db >= T
    out, quiet = [], 99
    for i, v in enumerate(loud):
        if not v:
            quiet += 1
        else:
            if quiet >= 2:
                out.append(i * HOP_MS / 1000.0)
            quiet = 0
    return np.array(out, dtype=np.float64)


def _nearest(arr: np.ndarray, x: float) -> float:
    i = int(np.searchsorted(arr, x))
    cands = [arr[j] for j in (i - 1, i) if 0 <= j < len(arr)]
    return min(cands, key=lambda v: abs(v - x)) if cands else x


def estimate_bias(cues: list[Cue], onsets: np.ndarray,
                  span_ms: int = 400, step_ms: int = 5) -> int:
    """자막 전체를 몇 ms 밀어야 말 시작점과 가장 잘 맞는지 훑어서 찾는다."""
    if not len(onsets) or not cues:
        return 0
    best, best_cost = 0, float("inf")
    for ms in range(-span_ms, span_ms + 1, step_ms):
        d = ms / 1000.0
        cost = sum(min(0.25, abs(c.start + d - _nearest(onsets, c.start + d))) for c in cues)
        if cost < best_cost:
            best_cost, best = cost, ms
    return best


def align_to_onsets(cues: list[Cue], onsets: np.ndarray, total: float,
                    window: float = 0.15) -> tuple[int, int]:
    """전체 치우침을 없앤 뒤, 자막마다 가까운 말 시작점에 붙인다."""
    if not len(onsets) or not cues:
        return 0, 0
    bias = estimate_bias(cues, onsets)
    if abs(bias) > 5:
        shift_cues(cues, bias / 1000.0, total)
    moved = 0
    for i, c in enumerate(cues):
        p = cues[i - 1] if i else None
        n = cues[i + 1] if i + 1 < len(cues) else None
        o = _nearest(onsets, c.start)
        if abs(c.start - o) > window:
            continue
        lo = p.start + 0.12 if p else 0.0
        hi = (n.start if n else total) - 0.12
        if not (lo <= o <= hi):
            continue
        join_l = p is not None and abs(p.end - c.start) < 0.004
        if abs(o - c.start) > 0.005:
            moved += 1
        c.start = o
        c.end = max(c.end, c.start + 0.1)
        if join_l:
            p.end = o
    return bias, moved


def tighten_cues(cues: list[Cue], db: np.ndarray, thr_db: float) -> tuple[int, float]:
    """
    자막 구간을 '실제로 말하는 구간' 으로 좁힌다.

    타입캐스트 SRT 는 문장 앞뒤 무음까지 자막 구간에 포함시킨다(앞 ~180ms, 뒤 ~280ms).
    그 상태로 글자 수 비례 배분을 하면 늘어난 자를 쓰는 셈이라 문장 뒤로 갈수록 밀린다.
    """
    hop = HOP_MS / 1000.0
    loud = db >= thr_db
    n = len(loud)
    fixed, saved = 0, 0.0
    for c in cues:
        i = max(0, min(n - 1, int(c.start / hop)))
        j = max(0, min(n - 1, int(c.end / hop)))
        k = i
        while k <= j and not loud[k]:
            k += 1
        if k > j:                     # 이 자막 구간엔 소리가 없다 — 건드리지 않는다
            continue
        m = j
        while m >= k and not loud[m]:
            m -= 1
        ns, ne = k * hop, (m + 1) * hop
        if ns > c.start + 0.02 or ne < c.end - 0.02:
            saved += (ns - c.start) + (c.end - ne)
            fixed += 1
        c.start, c.end = ns, max(ne, ns + 0.05)
    return fixed, saved


def shift_cues(cues: list[Cue], delta: float, total_new: float) -> None:
    """자막 전체를 delta 초만큼 민다 (+ 는 늦게, - 는 일찍)."""
    for c in cues:
        c.start = max(0.0, min(total_new - 0.05, c.start + delta))
        c.end = max(c.start + 0.05, min(total_new, c.end + delta))


# 앞말에 기대는 말들. 이런 말로 자막이 시작하면 뜻이 끊겨 읽기 어렵다. ("무서웠던 / 게 ...")
DEPENDENT = {"게", "것", "거", "수", "줄", "데", "때", "뿐", "지", "채", "터",
             "만큼", "대로", "듯", "및", "등", "적", "번", "개", "년", "월", "일",
             "말", "분", "초", "명", "원", "쪽", "편"}
# 문장이 끊어지기 좋은 자리 (종결어미·연결어미)
GOOD_END = ("요", "다", "죠", "까", "군", "네", "지", "고", "서", "며", "면",
            "만", "데", "니", "야", "봐", "래")
PUNCT_END = (".", "!", "?", ",", "…")
# 뒷말에 붙어야 하는 말. 이런 말로 줄이 끝나면 뜻이 잘린다. ("... 거 안 / 써본 게")
FORWARD = {"안", "못", "잘", "더", "좀", "또", "그", "이", "저", "새", "온",
           "한", "두", "세", "네", "몇", "무슨", "어떤", "다른", "같은", "매"}


def break_score(prev_w: str, next_w: str) -> float:
    """
    prev_w 와 next_w 사이에서 자막을 끊었을 때의 점수 (낮을수록 좋은 자리).
    자막은 읽고 바로 이해돼야 하므로, 뜻 덩어리를 깨지 않는 자리를 고른다.
    """
    s = 0.0
    if next_w in DEPENDENT:
        s += 45.0                    # '게 / 것 / 수' 로 시작하면 크게 어색하다
    elif len(next_w) == 1:
        s += 22.0                    # 한 글자로 줄을 시작하는 것도 피한다
    if prev_w.endswith(PUNCT_END):
        s -= 35.0                    # 문장이 끝난 자리가 가장 좋다
    elif prev_w.endswith(GOOD_END):
        s -= 14.0                    # 어미로 끝나면 끊어도 자연스럽다
    if prev_w in DEPENDENT:
        s -= 10.0                    # '...던 게' 처럼 앞말에 붙어 끝나면 좋은 자리다
    elif prev_w in FORWARD:
        s += 45.0                    # '안 / 못 / 한' 으로 줄을 끝내면 뜻이 잘린다
    elif len(prev_w) == 1:
        s += 30.0                    # '남 / 새' 처럼 한 글자로 줄을 끝내면 뒷말과 갈라진다
    return s


def chunk_by_words(row, max_chars: int, hard_max: int, gap_weight: float = 15.0):
    """
    단어별 실제 발화 시각을 근거로 자막을 나눈다.

    글자 수만 맞추면 '실제로 쉬는 자리' 를 무시하게 된다.
    여기서는 줄 길이 비용에 더해, 단어 사이 쉼이 큰 곳에서 끊으면 보상을 준다.
    """
    ws = [w for w, s, e in row]
    n = len(ws)
    if n == 0:
        return []
    pre = [0]
    for w in ws:
        pre.append(pre[-1] + len(w))

    # i 번째 단어 앞의 쉼 (초)
    gap = [0.0] * (n + 1)
    for i in range(1, n):
        _, s, _ = row[i]
        _, _, pe = row[i - 1]
        if s is not None and pe is not None:
            gap[i] = max(0.0, s - pe)

    INF = float("inf")
    cost = [INF] * (n + 1)
    back = [0] * (n + 1)
    cost[0] = 0.0
    for i in range(1, n + 1):
        for j in range(i - 1, -1, -1):
            width = pre[i] - pre[j] + (i - j - 1)
            if width > hard_max and j < i - 1:
                break
            if width <= max_chars:
                c = float((max_chars - width) ** 2)
            elif width <= hard_max:
                c = float((2 * (width - max_chars)) ** 2)   # 조금 넘는 건 관대하게
            else:
                c = 1e4
            if j > 0:            # 각 경계는 '어떤 줄의 시작' 으로 딱 한 번만 평가한다
                c += break_score(ws[j - 1], ws[j])
                c -= gap_weight * min(gap[j], 0.5)   # 실제로 쉬는 자리면 보상
            if cost[j] + c < cost[i]:
                cost[i] = cost[j] + c
                back[i] = j

    parts, i = [], n
    while i > 0:
        j = back[i]
        parts.append((j, i))
        i = j
    return parts[::-1]


def anchor_sentences(mapped: list[Cue], rows, max_shift: float = 0.4) -> int:
    """
    문장마다 Whisper 단어 시각을 통째로 밀어, 첫 단어가 '파형에서 구한 발화 시작' 에 오게 한다.

    Whisper 는 단어 시작을 일관되게 이르게 보고하지만(실측 -110~-230ms),
    단어 사이의 상대 간격은 정확하다. 그래서 절대 위치는 파형에서,
    상대 위치는 Whisper 에서 가져온다.
    """
    n = 0
    for c, row in zip(mapped, rows):
        starts = [s for _, s, _ in row if s is not None]
        if not starts:
            continue
        delta = c.start - starts[0]
        if abs(delta) > max_shift:          # 인식이 크게 어긋난 문장은 건드리지 않는다
            continue
        for i, (w, s, e) in enumerate(row):
            if s is not None:
                row[i] = (w, s + delta, (e + delta) if e is not None else None)
        n += 1
    return n


def resync_by_words(src_cues: list[Cue], rows, tmap: TimeMap, *, max_chars: int,
                    min_cue: float, total_new: float, bias: float = 0.0) -> list[Cue]:
    """Whisper 단어 시각을 1차 근거로 자막을 만든다 (비례 추정을 쓰지 않는다)."""
    hard_max = max_chars + 4
    out: list[Cue] = []
    for c, row in zip(src_cues, rows):
        if not row:
            continue
        for a, b in chunk_by_words(row, max_chars, hard_max):
            words = [w for w, _, _ in row[a:b]]
            starts = [s for _, s, _ in row[a:b] if s is not None]
            ends = [e for _, _, e in row[a:b] if e is not None]
            if not starts:
                continue
            s = max(0.0, min(total_new - 0.05, starts[0] + bias))
            e = max(s + 0.05, min(total_new, (ends[-1] if ends else s + min_cue) + bias))
            out.append(Cue(s, e, " ".join(words)))
    out.sort(key=lambda c: c.start)
    for i in range(len(out) - 1):               # 겹침 정리
        if out[i].end > out[i + 1].start:
            out[i].end = max(out[i].start + 0.05, out[i + 1].start)
    return out


def apply_fill(cues: list[Cue], total_new: float) -> None:
    """자막 사이 빈틈 없이 이어 붙인다 (앞 자막이 다음 자막 시작까지 유지)."""
    if not cues:
        return
    for i in range(len(cues) - 1):
        cues[i].end = cues[i + 1].start
    if cues[0].start <= EDGE_SNAP:
        cues[0].start = 0.0
    if total_new - cues[-1].end <= EDGE_SNAP:
        cues[-1].end = total_new


# --------------------------------------------------------------------------
# 파이프라인
# --------------------------------------------------------------------------

def find_srt(audio: Path, explicit: Path | None) -> Path | None:
    """자막(.srt) 또는 대본(.txt) 파일을 찾는다."""
    if explicit:
        return explicit if explicit.exists() else None
    for ext in (".srt", ".txt", ".md"):
        cand = audio.with_suffix(ext)
        if cand.exists():
            return cand
    return None


def load_cues(path: Path) -> tuple[list[Cue], bool]:
    """
    자막/대본 파일을 읽어 (문장 목록, 시각정보 있음 여부) 를 돌려준다.

    .srt 는 시각이 있고, .txt 는 문장만 있다. 텍스트만 있어도
    시각은 음성 인식으로 채우므로 동작한다.
    """
    if path.suffix.lower() in SCRIPT_EXTS:
        return [Cue(0.0, 0.0, s) for s in parse_script_text(read_text(path))], False
    return parse_srt(path), True


def human(sec: float) -> str:
    m, s = divmod(sec, 60)
    return f"{int(m):02d}:{s:05.2f}"


def process(audio: Path, args) -> None:
    sr, ch, _ = probe(audio)
    pcm = decode(audio, sr, ch)
    an = analyze(pcm, sr,
                 threshold=None if args.auto_threshold else args.threshold,
                 min_silence=args.min_silence,
                 keep_silence=args.keep_silence,
                 edge_silence=args.edge_silence,
                 trim_edges=not args.no_trim_edges)

    saved = an.orig_sec - an.new_sec
    pct = (saved / an.orig_sec * 100.0) if an.orig_sec else 0.0
    print(f"\n■ {audio.name}")
    print(f"  임계값 {an.threshold_db:.1f} dBFS | 무음 {len(an.removes)}곳 제거")
    print(f"  {human(an.orig_sec)} → {human(an.new_sec)}  (-{human(saved)}, {pct:.1f}% 단축)")

    if args.verbose:
        for s, e in an.removes[:200]:
            print(f"    cut {s / sr:8.3f}s ~ {e / sr:8.3f}s  ({(e - s) / sr:.3f}s)")

    if args.dry_run:
        return

    outdir = Path(args.outdir) if args.outdir else audio.parent
    outdir.mkdir(parents=True, exist_ok=True)
    stem = audio.stem + args.suffix
    ext = args.format if args.format.startswith(".") else "." + args.format
    dst_audio = outdir / (stem + ext)

    new_pcm = render(pcm, an, fade_ms=args.fade)
    encode(new_pcm, sr, ch, dst_audio, bitrate=args.bitrate)
    print(f"  → {dst_audio.name}")

    tmap = TimeMap(an)
    srt_path = find_srt(audio, Path(args.srt) if args.srt else None)
    if srt_path:
        cues, has_times = load_cues(srt_path)
        if not cues:
            print(f"  [!] {srt_path.name} 에서 문장을 읽지 못했습니다", file=sys.stderr)
        if not has_times:
            print(f"  · 대본 텍스트 {len(cues)}문장 — 시각은 음성 인식으로 채웁니다")
        if not args.raw_text:
            for c in cues:
                c.text = clean_text(c.text)
        if has_times and not args.no_tighten and cues:
            mono_src = (pcm.astype(np.float32).mean(axis=1) if ch > 1
                        else pcm[:, 0].astype(np.float32))
            src_db, _ = frame_db(mono_src, sr)
            fixed, saved = tighten_cues(cues, src_db, an.threshold_db)
            if fixed:
                print(f"  · 자막 구간을 실제 발화에 맞춰 좁힘 "
                      f"({fixed}개, 무음 {saved:.2f}초 제외)")
        mono_new = (new_pcm.astype(np.float32).mean(axis=1) if ch > 1
                    else new_pcm[:, 0].astype(np.float32))
        new_db, _ = frame_db(mono_new, sr)
        new_cues = None
        if args.split == "words" and args.align and cues:
            import aligner
            if not aligner.available():
                print("  [!] faster-whisper 가 없어 음성 인식 분할을 건너뜁니다 "
                      "(pip install faster-whisper)", file=sys.stderr)
            else:
                words = aligner.transcribe_words(
                    dst_audio, model_size=args.model, language=args.language,
                    device=args.device, compute_type=args.compute_type,
                    verbose=args.verbose)
                # 원본 시각 -> 편집 후 시각으로 옮긴 자막 구간 기준으로 정렬
                # 쉼 표시(BREAK)에서 미리 쪼개 두면 그 자리에서 반드시 나뉜다
                if not has_times:
                    for c in cues:
                        c.start, c.end = 0.0, an.orig_sec
                mapped = []
                for c in cues:
                    parts = [x for x in c.text.split(BREAK) if x.strip()]
                    for part in (parts or [c.text]):
                        mapped.append(Cue(tmap(c.start), tmap(c.end), part.strip()))
                ratio, rows = aligner.word_times([c.text for c in mapped], words, an.new_sec)
                if rows:
                    # 문장 시작점 고정은 실측에서 오히려 정확도를 떨어뜨려 기본에서 뺐다
                    # (구간별 인식 대조: 앵커 0.877 vs 없음 0.918)
                    anchored = (anchor_sentences(mapped, rows)
                                if (args.anchor and has_times) else 0)
                    new_cues = resync_by_words(
                        mapped, rows, tmap, max_chars=args.max_chars,
                        min_cue=args.min_cue, total_new=an.new_sec,
                        bias=(args.whisper_bias if has_times
                              else max(args.whisper_bias, 80.0)) / 1000.0)
                    msg = (f"  ✓ 음성 인식 기반 분할 (단어 {len(words)}개, "
                           f"일치율 {ratio:.0%}, 자막 {len(new_cues)}개")
                    print(msg + (f", 문장 {anchored}개 고정)" if anchored else ")"))
                elif ratio < 0.30:
                    # 이 정도면 오디오와 자막이 아예 다른 내용이다.
                    # 그대로 두면 엉뚱한 자막이 붙은 파일이 조용히 만들어진다.
                    heard = "".join(w.text for w in words)[:40]
                    mismatch = (
                        f"\n  [X] 오디오와 자막이 서로 다른 내용 같습니다 "
                        f"(일치율 {ratio:.0%})\n"
                        f"      자막 : {cues[0].text[:40] if cues else ''}\n"
                        f"      소리 : {heard}\n"
                        f"      파일 짝이 맞는지 확인하세요. "
                        f"그래도 진행하려면 --force-mismatch\n")
                    if not args.force_mismatch:
                        print(mismatch, file=sys.stderr)
                        sys.exit("  중단했습니다 (잘못된 자막 파일을 만들지 않기 위해).")
                    print(mismatch + "  --force-mismatch 로 계속합니다.", file=sys.stderr)
                else:
                    print(f"  [!] 인식 일치율 {ratio:.0%} — 글자 수 기준으로 나눕니다",
                          file=sys.stderr)

        if new_cues is None and not has_times:
            sys.exit("[!] 대본 텍스트(.txt)는 음성 인식이 있어야 시각을 만들 수 있습니다.\n"
                     "    faster-whisper 를 설치하거나, 시각이 든 .srt 를 사용하세요.")
        if new_cues is None:                     # 인식을 못 쓰면 기존 방식
            mono_new = (new_pcm.astype(np.float32).mean(axis=1) if ch > 1
                        else new_pcm[:, 0].astype(np.float32))
            new_db, _ = frame_db(mono_new, sr)
            new_cues = resync(cues, tmap,
                              max_chars=args.max_chars, min_cue=args.min_cue,
                              fill=False, new_db=None if args.no_snap else new_db,
                              snap_window=args.snap_window, total_new=an.new_sec)

        if args.offset:
            shift_cues(new_cues, args.offset / 1000.0, an.new_sec)
        if not args.keep_gaps:
            apply_fill(new_cues, an.new_sec)

        dst_srt = outdir / (stem + ".srt")
        write_srt(new_cues, dst_srt)
        print(f"  → {dst_srt.name}  (자막 {len(cues)}개 → {len(new_cues)}개, 최대 {args.max_chars}자)")
        for msg in validate_cues(new_cues, an.new_sec):
            print(f"     ⚠ {msg}")
    else:
        print("  (같은 이름의 .srt 가 없어 자막은 건너뜀)")

    if args.save_map:
        (outdir / (stem + ".map.json")).write_text(json.dumps({
            "source": str(audio), "sample_rate": sr, "channels": ch,
            "threshold_db": an.threshold_db,
            "orig_sec": an.orig_sec, "new_sec": an.new_sec,
            "removed": [[s / sr, e / sr] for s, e in an.removes],
            "kept": [[s / sr, e / sr] for s, e in an.keeps],
        }, ensure_ascii=False, indent=2), encoding="utf-8")


def collect(inputs: Iterable[str], suffix: str = "") -> list[Path]:
    files: list[Path] = []
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            files += sorted(x for x in p.iterdir()
                            if x.suffix.lower() in AUDIO_EXTS
                            and not (suffix and x.stem.endswith(suffix)))  # 결과물 재처리 방지
        elif p.exists():
            files.append(p)
        else:
            print(f"[!] 없는 경로: {p}", file=sys.stderr)
    return files


def main() -> None:
    ap = argparse.ArgumentParser(
        description="TTS 오디오의 무음을 잘라내고 SRT 싱크를 다시 맞춥니다.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="오디오 파일 또는 폴더")
    ap.add_argument("--srt", help="자막(.srt) 또는 대본(.txt) 경로 "
                                  "(기본: 오디오와 같은 이름을 자동 탐색)")
    ap.add_argument("-o", "--outdir", help="출력 폴더 (기본: 원본과 같은 폴더)")
    ap.add_argument("--suffix", default="_clean", help="출력 파일 이름 접미사")
    ap.add_argument("--format", default="mp3", help="출력 오디오 형식 (mp3/wav)")
    ap.add_argument("--bitrate", default="192k", help="mp3 비트레이트")

    ap.add_argument("-t", "--threshold", type=float, default=-30.0,
                    help="무음 임계값 dBFS. 'auto' 대신 숫자로 지정 (--auto-threshold 로 자동 계산)")
    ap.add_argument("--auto-threshold", action="store_true",
                    help="임계값을 파일마다 자동 계산 (-t 무시)")
    ap.add_argument("-m", "--min-silence", type=float, default=0.10,
                    help="이 길이 이상인 무음만 제거 대상 (초)")
    ap.add_argument("-k", "--keep-silence", type=float, default=0.05,
                    help="자를 때 앞뒤로 남길 여백 (초). 무음이 이 값의 2배로 줄어듭니다")
    ap.add_argument("--edge-silence", type=float, default=0.05,
                    help="파일 맨 앞/뒤 무음을 남길 길이 (초)")
    ap.add_argument("--no-trim-edges", action="store_true", help="앞뒤 무음을 건드리지 않음")
    ap.add_argument("--fade", type=float, default=4.0, help="이음새 micro-fade 길이 (ms)")

    ap.add_argument("-c", "--max-chars", type=int, default=10, help="자막 한 줄 최대 글자 수")
    ap.add_argument("--min-cue", type=float, default=0.45, help="자막 최소 노출 시간 (초)")
    ap.add_argument("--raw-text", action="store_true",
                    help="대본 표기를 그대로 둠 (기본은 '-' 쉼 표시 등을 정리)")
    ap.add_argument("--no-tighten", action="store_true",
                    help="자막 구간을 실제 발화 구간으로 좁히지 않음")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="자막 전체를 밀기 (ms). 음수면 자막이 먼저 뜹니다")
    ap.add_argument("--keep-gaps", action="store_true",
                    help="자막 사이 빈틈을 그대로 둠 (기본은 빈틈 없이 이어 붙임)")
    ap.add_argument("--snap-window", type=float, default=0.20,
                    help="자막 경계를 에너지 최저점으로 당길 탐색 범위 (초)")
    ap.add_argument("--no-snap", action="store_true", help="경계 스냅 없이 글자 수 비례로만 배분")

    g = ap.add_argument_group("싱크 보정 (Whisper 로 실제 발화 시각 측정)")
    g.add_argument("--no-align", dest="align", action="store_false",
                   help="싱크 보정을 끄고 글자 수 비례 추정만 사용")
    g.add_argument("--model", default="medium",
                   help="Whisper 모델 (tiny/base/small/medium/large-v3). 클수록 정확하고 느림")
    g.add_argument("--language", default="ko", help="음성 언어 코드")
    g.add_argument("--device", default="auto", help="auto/cuda/cpu")
    g.add_argument("--compute-type", default="auto", help="auto/float16/int8 등")
    g.add_argument("--split", choices=("words", "chars"), default="words",
                   help="words=인식한 단어의 실제 발화 시각으로 나눔, chars=글자 수로 나눔")
    g.add_argument("--force-mismatch", action="store_true",
                   help="오디오와 자막 내용이 달라도 강행")
    g.add_argument("--anchor", action="store_true",
                   help="문장 첫 단어를 파형에서 잡은 발화 시작에 고정 "
                        "(실측상 정확도가 떨어져 기본은 끔)")
    g.add_argument("--whisper-bias", type=float, default=-30.0,
                   help="자막 전체를 이만큼(ms) 밀어 보정합니다. "
                        "실측 최적값 -30 (자막이 약간 먼저 뜨는 편이 읽기 좋습니다)")
    g.add_argument("--trust-shift", type=float, default=0.25,
                   help="비례 추정과 이 값(초) 이상 어긋날 때만 Whisper 결과를 채택")
    g.add_argument("--post-snap", type=float, default=0.0,
                   help="정렬 후 자막 시작을 말 사이 틈으로 당길 탐색 범위 (초). 0 이면 끔")
    g.add_argument("--max-shift", type=float, default=3.0,
                   help="이 값(초)보다 크게 밀리는 자막은 오인식으로 보고 건드리지 않음")

    ap.add_argument("-n", "--dry-run", action="store_true", help="분석 결과만 보고 파일은 쓰지 않음")
    ap.add_argument("-v", "--verbose", action="store_true", help="잘라낸 구간 목록 출력")
    ap.add_argument("--save-map", action="store_true", help="시간 매핑 json 저장")

    args = ap.parse_args()
    files = collect(args.inputs, args.suffix)
    if not files:
        sys.exit("[!] 처리할 오디오가 없습니다.")
    if args.srt and len(files) > 1:
        sys.exit("[!] --srt 는 오디오 1개일 때만 사용할 수 있습니다.")
    for f in files:
        process(f, args)
    print()


if __name__ == "__main__":
    main()
