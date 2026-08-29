# -*- coding: utf-8 -*-
"""
capcut_export.py - capcut.bat 이 부르는 대화형 내보내기.

오디오(+ SRT)를 끌어다 놓으면 프로젝트 이름과 화면 비율을 물어보고
캡컷 프로젝트를 만든다.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
RATIOS = ["9:16", "1:1", "4:5", "16:9"]


def ask(prompt: str, default: str) -> str:
    try:
        v = input(f"{prompt} [{default}]: ")
    except EOFError:
        return default
    v = v.replace("﻿", "").strip()      # 붙여넣기로 딸려오는 BOM 제거
    return v or default


def main() -> int:
    args = [Path(a) for a in sys.argv[1:]]
    audio = next((p for p in args if p.suffix.lower() in AUDIO_EXTS), None)
    srt = next((p for p in args if p.suffix.lower() == ".srt"), None)

    print("\n" + "=" * 58)
    print("   캡컷 프로젝트로 내보내기")
    print("=" * 58 + "\n")

    if not audio:
        print("오디오 파일(또는 오디오+SRT)을 capcut.bat 위로 끌어다 놓으세요.\n")
        return 1
    if srt is None:
        cand = audio.with_suffix(".srt")
        srt = cand if cand.is_file() else None

    print(f"  오디오 : {audio.name}")
    print(f"  자막   : {srt.name if srt else '(없음 — 오디오만 넣습니다)'}\n")

    today = dt.date.today().strftime("%y%m%d")
    name = ask("프로젝트 이름", f"{today}_{audio.stem[:20]}")

    print(f"\n  화면 비율: {' / '.join(f'{i+1}) {r}' for i, r in enumerate(RATIOS))}")
    pick = ask("  번호 선택", "1")
    try:
        ratio = RATIOS[int(pick) - 1]
    except (ValueError, IndexError):
        ratio = "9:16"

    cmd = [sys.executable, str(Path(__file__).with_name("to_capcut.py")),
           str(audio)]
    if srt:
        cmd.append(str(srt))
    cmd += ["--name", name, "--ratio", ratio]
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
