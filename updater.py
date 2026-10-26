# -*- coding: utf-8 -*-
"""
updater.py - 프로그램을 켤 때 새 버전이 있으면 받아서 교체한다.

원칙
  · 인터넷이 없거나 서버가 죽어 있으면 조용히 넘어간다 (프로그램은 무조건 켜진다)
  · 바꾸기 전에 통째로 백업한다 (--rollback 으로 되돌릴 수 있다)
  · 작업 중에는 절대 건드리지 않는다 (프로그램 시작 시점에만 확인)

update.json 이 있어야 동작한다. 없으면 아무것도 하지 않는다.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

try:
    import localbin      # 한글 출력 보호
except Exception:
    pass

APP = Path(__file__).resolve().parent
CONF = APP / "update.json"
BACKUP = APP / "_backup"
TIMEOUT = 3          # 인터넷이 없을 때 오래 기다리지 않는다

# 사용자가 만든 것·환경마다 다른 것은 덮어쓰지 않는다
KEEP = {"_backup", "__pycache__", "capcut_skeleton.json", "bin"}


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def fetch(url: str, token: str = "") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "desilence-updater"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def local_version() -> str:
    f = APP / "VERSION"
    return f.read_text(encoding="utf-8").strip() if f.is_file() else ""


def restore() -> bool:
    if not BACKUP.is_dir():
        return False
    for src in BACKUP.rglob("*"):
        if src.is_file():
            dst = APP / src.relative_to(BACKUP)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return True


def backup(files: list[Path]) -> None:
    if BACKUP.is_dir():
        shutil.rmtree(BACKUP, ignore_errors=True)
    for f in files:
        if f.is_file():
            dst = BACKUP / f.relative_to(APP)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)


def apply_zip(data: bytes) -> int:
    """받은 zip 을 프로그램 폴더에 덮어쓴다. 바꾼 파일 수를 돌려준다."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        if not names:
            return 0
        # GitHub zip 은 최상위에 폴더가 하나 더 있다 — 벗겨낸다
        roots = {n.split("/", 1)[0] for n in names}
        strip = len(roots) == 1 and all("/" in n for n in names)

        plan: list[tuple[str, Path]] = []
        for n in names:
            rel = n.split("/", 1)[1] if strip else n
            if not rel or rel.split("/")[0] in KEEP:
                continue
            plan.append((n, APP / rel))

        backup([p for _, p in plan])
        for n, dst in plan:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(z.read(n))
        return len(plan)


def update() -> None:
    if not CONF.is_file():
        return
    try:
        conf = json.loads(CONF.read_text(encoding="utf-8"))
    except Exception:
        return
    ver_url, zip_url = conf.get("version_url"), conf.get("zip_url")
    if not ver_url or not zip_url:
        return
    token = conf.get("token", "")

    try:
        remote = fetch(ver_url, token).decode("utf-8", "replace").strip()
    except Exception:
        return                                   # 인터넷 없음 등 — 조용히 넘어간다
    local = local_version()
    # 원격이 '더 새로울 때만' 받는다. 같거나 로컬이 더 새로우면 그대로 둔다.
    # (개발 중인 PC 에서 옛 버전을 덮어쓰는 사고를 막는다)
    if not remote or remote <= local:
        return

    old = local or "(없음)"
    log(f"새 버전 {remote} 을 받는 중...")
    try:
        data = fetch(zip_url, token)
        n = apply_zip(data)
        if n:
            log(f"업데이트 완료: {old} -> {remote}  (파일 {n}개)")
            log("문제가 생기면 '되돌리기.bat' 을 실행하세요.")
    except Exception as e:
        log(f"업데이트 실패 ({type(e).__name__}) — 기존 버전으로 실행합니다")
        restore()


def main() -> int:
    if "--rollback" in sys.argv:
        if restore():
            print(f"  이전 버전으로 되돌렸습니다 (현재 {local_version()})")
        else:
            print("  되돌릴 백업이 없습니다.")
        return 0
    update()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
