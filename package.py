# -*- coding: utf-8 -*-
"""
package.py - 다른 PC 로 옮길 배포용 zip 을 바탕화면에 만든다.

코드를 고친 뒤 이걸 실행해서 나온 zip 을 옮기면 된다.
받는 쪽은 기존 폴더에 덮어쓰기만 하면 되고, install.bat 을 다시 돌릴 필요는 없다.
"""

from __future__ import annotations

import datetime as dt
import os
import zipfile
from pathlib import Path

APP = Path(__file__).resolve().parent

FILES = [
    "desilence.py", "aligner.py", "app.py", "to_capcut.py", "capcut_export.py",
    "setup.py", "package.py", "capcut_skeleton.json", "VERSION", "README.md",
    "app.ico", "updater.py", "update.json", "되돌리기.bat",
    "install.bat", "run.bat", "capcut.bat", "무음제거 열기.bat",
    "web/index.html",
]


def desktop() -> Path:
    home = Path(os.environ.get("USERPROFILE", Path.home()))
    for name in ("Desktop", "바탕 화면", "OneDrive/Desktop", "OneDrive/바탕 화면"):
        d = home / name
        if d.is_dir():
            return d
    return home


def main() -> int:
    stamp = dt.date.today().strftime("%Y.%m.%d")
    (APP / "VERSION").write_text(stamp, encoding="utf-8")

    missing = [f for f in FILES if not (APP / f).is_file()]
    if missing:
        print("[X] 빠진 파일이 있습니다:")
        for m in missing:
            print("   ", m)
        return 1

    out = desktop() / f"무음제거_자막편집_{stamp}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in FILES:
            z.write(APP / f, f)

    print("\n" + "=" * 58)
    print("   배포용 압축 파일을 만들었습니다")
    print("=" * 58)
    print(f"\n  파일 : {out.name}")
    print(f"  위치 : {out.parent}")
    print(f"  크기 : {out.stat().st_size / 1024:,.0f} KB")
    print(f"  버전 : {stamp}\n")
    print("  옮기는 쪽 PC 에서 할 일:")
    print("   1) 이 zip 을 옮겨서 압축 풀기")
    print("   2) 쓰던 폴더에 통째로 덮어쓰기")
    print("   3) 끝 (install.bat 은 다시 안 돌려도 됩니다)\n")
    print("  단, 앱이 켜져 있으면 먼저 닫고 덮어쓰세요.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
