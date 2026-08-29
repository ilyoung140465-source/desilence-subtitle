# -*- coding: utf-8 -*-
"""
aligner.py - Whisper 단어 타임스탬프로 자막 싱크를 실측 보정한다.

글자 수 비례 추정 대신, 정리된 오디오를 실제로 인식해서 단어가 언제 발화되는지
측정한 뒤 대본 글자를 그 타이밍에 정렬한다. (forced alignment)

인식 결과가 대본과 많이 다르면(음악만 있는 구간 등) 보정을 포기하고
원래 타이밍을 그대로 둔다.
"""

from __future__ import annotations

import glob
import os
import re
import sys
from bisect import bisect_right
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path


def _register_cuda_dlls() -> None:
    """
    pip 로 설치한 nvidia-* 패키지의 DLL 을 Windows 검색 경로에 등록한다.
    (Python 3.8+ 는 site-packages 안의 DLL 을 자동으로 찾지 않는다)
    """
    if os.name != "nt":
        return
    import site
    roots = list(site.getsitepackages())
    if hasattr(site, "getusersitepackages"):
        roots.append(site.getusersitepackages())
    for root in roots:
        for d in glob.glob(os.path.join(root, "nvidia", "*", "bin")):
            if os.path.isdir(d):
                try:
                    os.add_dll_directory(d)
                except OSError:
                    pass
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


_register_cuda_dlls()


@dataclass
class Word:
    start: float
    end: float
    text: str


# 한글 음절/자모, 숫자, 라틴 문자만 남긴다 (문장부호·공백은 정렬에 방해)
_KEEP = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ0-9a-z]")


def normalize(text: str) -> str:
    return "".join(_KEEP.findall(text.lower()))


# --------------------------------------------------------------------------
# 음성 인식
# --------------------------------------------------------------------------

def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def pick_device(device: str, compute_type: str) -> tuple[str, str]:
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"
    return device, compute_type


_MODEL_CACHE: dict[tuple, object] = {}


def transcribe_words(audio: Path, *, model_size: str = "medium", language: str = "ko",
                     device: str = "auto", compute_type: str = "auto",
                     verbose: bool = False) -> list[Word]:
    """오디오를 인식해 단어 단위 타임스탬프를 돌려준다."""
    from faster_whisper import WhisperModel

    def load(dev: str, ctype: str):
        key = (model_size, dev, ctype)
        if key not in _MODEL_CACHE:
            if verbose:
                print(f"    Whisper {model_size} 로드 ({dev}/{ctype})...", flush=True)
            _MODEL_CACHE[key] = WhisperModel(model_size, device=dev, compute_type=ctype)
        return _MODEL_CACHE[key]

    def run(model) -> list[Word]:
        segments, _ = model.transcribe(
            str(audio),
            language=language,
            word_timestamps=True,
            vad_filter=False,             # 무음은 이미 제거됨
            beam_size=5,
            condition_on_previous_text=False,
        )
        words: list[Word] = []
        for seg in segments:              # 실제 연산은 여기서 일어난다
            for w in (seg.words or []):
                t = w.word.strip()
                if t:
                    words.append(Word(float(w.start), float(w.end), t))
        return words

    device, compute_type = pick_device(device, compute_type)
    try:
        return run(load(device, compute_type))
    except Exception as e:
        if device != "cuda":
            raise
        # cuBLAS/cuDNN 미설치, VRAM 부족 등 → CPU 로 후퇴
        msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        print(f"    [!] GPU 사용 불가 ({msg})\n"
              f"        CPU 로 진행합니다 (많이 느립니다).\n"
              f"        해결: install.bat 을 다시 실행하거나, 최신 그래픽카드라면\n"
              f"              pip install -U ctranslate2",
              file=sys.stderr, flush=True)
        return run(load("cpu", "int8"))


# --------------------------------------------------------------------------
# 정렬
# --------------------------------------------------------------------------

def _char_timeline(words: list[Word]) -> tuple[str, list[float]]:
    """인식 결과를 '글자 문자열 + 글자별 시작시각' 으로 편다."""
    chars: list[str] = []
    times: list[float] = []
    for w in words:
        norm = normalize(w.text)
        if not norm:
            continue
        span = max(1e-3, w.end - w.start)
        step = span / len(norm)
        for i, c in enumerate(norm):
            chars.append(c)
            times.append(w.start + i * step)
    return "".join(chars), times


def _interpolator(anchors: list[tuple[int, float]], n_ref: int, total: float):
    """(참조 글자 인덱스 → 시각) 앵커들로 임의 인덱스의 시각을 보간한다."""
    idxs = [a for a, _ in anchors]
    ts = [t for _, t in anchors]

    def at(r: int) -> float:
        if r <= idxs[0]:
            # 앞쪽 바깥: 앞 구간 속도로 외삽
            if len(idxs) > 1 and idxs[1] > idxs[0]:
                rate = (ts[1] - ts[0]) / (idxs[1] - idxs[0])
            else:
                rate = 0.0
            return max(0.0, ts[0] - (idxs[0] - r) * rate)
        if r >= idxs[-1]:
            if len(idxs) > 1 and idxs[-1] > idxs[-2]:
                rate = (ts[-1] - ts[-2]) / (idxs[-1] - idxs[-2])
            else:
                rate = 0.0
            return min(total, ts[-1] + (r - idxs[-1]) * rate)
        k = bisect_right(idxs, r) - 1
        span = idxs[k + 1] - idxs[k]
        if span <= 0:
            return ts[k]
        f = (r - idxs[k]) / span
        return ts[k] + f * (ts[k + 1] - ts[k])

    return at


def measure(cues, words: list[Word], total: float, *, min_ratio: float = 0.45):
    """
    고치지 않고 '재기만' 한다. 각 자막이 실제 발화보다 얼마나 앞/뒤인지 ms 로 돌려준다.
    align() 과 같은 정렬을 쓰되 결과를 적용하지 않는다.
    """
    if not cues or not words:
        return None, []
    ref_parts, bounds = [], []
    pos = 0
    for c in cues:
        n = normalize(c.text)
        bounds.append((pos, pos + len(n)))
        ref_parts.append(n)
        pos += len(n)
    ref = "".join(ref_parts)
    hyp, hyp_times = _char_timeline(words)
    if not ref or not hyp:
        return None, []

    sm = SequenceMatcher(None, ref, hyp, autojunk=False)
    anchors, matched = [], 0
    for a, b, size in sm.get_matching_blocks():
        anchors += [(a + k, hyp_times[b + k]) for k in range(size)]
        matched += size
    ratio = matched / len(ref)
    if ratio < min_ratio or len(anchors) < 2:
        return ratio, []

    anchors.sort()
    at = _interpolator(anchors, len(ref), total)
    out = []
    for i, ((s_idx, e_idx), c) in enumerate(zip(bounds, cues)):
        if e_idx <= s_idx:
            continue
        spoken = at(s_idx)
        out.append({"i": i, "text": c.text, "cue": c.start,
                    "spoken": spoken, "delta_ms": (c.start - spoken) * 1000.0})
    return ratio, out


def word_times(texts, words: list[Word], total: float, *, min_ratio: float = 0.45):
    """
    대본의 단어마다 '실제로 언제 발화됐는지' 를 돌려준다.

    반환: (일치율, [[(단어, 시작, 끝), ...], ...])  - 바깥 리스트는 texts 와 같은 길이
    인식이 대본과 너무 다르면 (일치율, None) 을 준다.
    """
    ref_parts, spans = [], []          # spans[i] = 그 자막 안 단어들의 (문자시작, 문자끝)
    pos = 0
    for txt in texts:
        ws = [w for w in re.split(r"\s+", txt.strip()) if w]
        cur = []
        for w in ws:
            n = normalize(w)
            cur.append((w, pos, pos + len(n)))
            pos += len(n)
            ref_parts.append(n)
        spans.append(cur)
    ref = "".join(ref_parts)
    if not ref or not words:
        return 0.0, None

    hyp, hyp_times = _char_timeline(words)
    if not hyp:
        return 0.0, None
    sm = SequenceMatcher(None, ref, hyp, autojunk=False)
    anchors, matched = [], 0
    for a, b, size in sm.get_matching_blocks():
        anchors += [(a + k, hyp_times[b + k]) for k in range(size)]
        matched += size
    ratio = matched / len(ref)
    if ratio < min_ratio or len(anchors) < 2:
        return ratio, None

    anchors.sort()
    at = _interpolator(anchors, len(ref), total)
    out = []
    for cur in spans:
        row = []
        for w, s, e in cur:
            if e <= s:                       # 정렬에 쓸 글자가 없는 단어(기호 등)
                row.append((w, None, None))
            else:
                row.append((w, at(s), at(e)))
        out.append(row)
    return ratio, out


@dataclass
class AlignReport:
    matched: int
    ref_len: int
    applied: bool
    reason: str = ""
    corrected: int = 0          # 실제로 시각을 고친 자막 수

    @property
    def ratio(self) -> float:
        return self.matched / self.ref_len if self.ref_len else 0.0


def align(cues, words: list[Word], total: float, *, min_ratio: float = 0.45,
          min_cue: float = 0.30, max_shift: float = 3.0,
          trust_shift: float = 0.25) -> AlignReport:
    """
    cues 의 start/end 를 인식 타이밍에 맞춰 제자리 수정한다.
    cues 는 .start/.end/.text 를 가진 객체 리스트 (desilence.Cue).

    글자 수 비례 추정은 국소적으로는 꽤 정확하고(중앙값 ~30ms), 대신 자막 안에
    숨·강조가 있으면 크게 밀린다. Whisper 는 그 큰 드리프트를 잡아주지만
    자체 오차가 ~100ms 있다. 그래서 둘이 trust_shift 이상 어긋날 때만
    Whisper 를 채택한다 (그 이하면 원래 추정이 더 정확하다).
    """
    if not cues:
        return AlignReport(0, 0, False, "자막 없음")
    if not words:
        return AlignReport(0, 0, False, "인식된 단어 없음")

    # 참조: 자막 글자를 모두 이어붙이고, 각 자막의 시작 인덱스를 기억
    ref_parts, bounds = [], []
    pos = 0
    for c in cues:
        norm = normalize(c.text)
        bounds.append((pos, pos + len(norm)))
        ref_parts.append(norm)
        pos += len(norm)
    ref = "".join(ref_parts)
    if not ref:
        return AlignReport(0, 0, False, "자막에 정렬할 글자가 없음")

    hyp, hyp_times = _char_timeline(words)
    if not hyp:
        return AlignReport(0, len(ref), False, "인식 결과가 비어 있음")

    sm = SequenceMatcher(None, ref, hyp, autojunk=False)
    anchors: list[tuple[int, float]] = []
    matched = 0
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            anchors.append((a + k, hyp_times[b + k]))
        matched += size

    report = AlignReport(matched, len(ref), False)
    if report.ratio < min_ratio or len(anchors) < 2:
        report.reason = f"인식 일치율 {report.ratio:.0%} — 보정을 건너뜁니다"
        return report

    anchors.sort()
    at = _interpolator(anchors, len(ref), total)

    new_times = []
    for (s_idx, e_idx), c in zip(bounds, cues):
        if e_idx <= s_idx:                       # 글자가 없는 자막
            new_times.append(None)
            continue
        ns, ne = at(s_idx), at(e_idx)
        if ne - ns < min_cue:
            ne = ns + min_cue
        shift = abs(ns - c.start)
        if shift > max_shift:                    # 오인식으로 보고 무시
            new_times.append(None)
            continue
        if shift < trust_shift:                  # 원래 추정이 이미 충분히 정확
            new_times.append(None)
            continue
        new_times.append((max(0.0, ns), min(total, ne)))

    report.corrected = sum(1 for t in new_times if t)
    for c, t in zip(cues, new_times):
        if t:
            c.start, c.end = t

    # 단조 증가 보정
    for i in range(1, len(cues)):
        if cues[i].start < cues[i - 1].start:
            cues[i].start = cues[i - 1].start
        if cues[i].end <= cues[i].start:
            cues[i].end = cues[i].start + min_cue
    for i in range(len(cues) - 1):
        if cues[i].end > cues[i + 1].start:
            cues[i].end = max(cues[i].start + 0.1, cues[i + 1].start)

    report.applied = True
    return report
