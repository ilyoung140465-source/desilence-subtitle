# -*- coding: utf-8 -*-
"""
localbin.py - 어느 PC 에서도 똑같이 뜨게 해 주는 준비 작업.

이 파일을 맨 먼저 import 하면 두 가지가 된다.

1) 한글 출력이 죽지 않는다.
   윈도우에서 출력이 파일이나 파이프로 넘어가면 stdout 이 cp949 가 된다.
   그러면 '—' 같은 글자에서 UnicodeEncodeError 로 프로그램이 통째로 죽는다.
   창에는 앞부분만 찍히고 끝나서, 무슨 일인지 알 수가 없다.

2) 프로그램 폴더의 bin/ 에 둔 ffmpeg 을 찾는다.
   시스템에 ffmpeg 을 못 까는 PC 가 있다 (winget 이 없거나, 깔았는데
   PATH 가 갱신되지 않거나). 시스템은 건드리지 않고 여기서만 쓴다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent / "bin"


def safe_console() -> None:
    """한글을 찍다가 죽지 않게 한다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass          # 준비 작업이 프로그램을 막아서는 안 된다


def register() -> None:
    """bin/ 이 있으면 이 프로세스의 PATH 앞에 붙인다."""
    if not BIN.is_dir():
        return
    p = str(BIN)
    cur = os.environ.get("PATH", "")
    if p.lower() not in [x.lower() for x in cur.split(os.pathsep)]:
        os.environ["PATH"] = p + os.pathsep + cur


safe_console()
register()
