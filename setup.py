# -*- coding: utf-8 -*-
"""
setup.py - 다른 PC에 이 도구를 설치/점검한다. install.bat 이 호출한다.

배치 파일은 한글을 안정적으로 못 다루므로, 검사와 안내는 전부 여기서 한다.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import localbin          # 프로그램 폴더의 bin/ 을 PATH 에 붙인다

PKGS = ["numpy", "faster-whisper"]
CUDA_PKGS = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]


def pip(*args: str) -> bool:
    return subprocess.run([sys.executable, "-m", "pip", *args]).returncode == 0


def has_gpu() -> tuple[str, float] | None:
    """(그래픽카드 이름, 연산 능력) 을 돌려준다. 없으면 None."""
    if not shutil.which("nvidia-smi"):
        return None
    r = subprocess.run(["nvidia-smi", "--query-gpu=name,compute_cap",
                        "--format=csv,noheader"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not r.stdout.strip():
        return None
    line = r.stdout.strip().splitlines()[0]
    name, _, cap = line.partition(",")
    try:
        cc = float(cap.strip())
    except ValueError:
        cc = 0.0
    return name.strip(), cc


def make_shortcuts() -> None:
    """바탕화면과 시작 메뉴에 바로가기를 만든다 (일반 프로그램처럼 쓰기 위해)."""
    app = Path(__file__).resolve().parent
    target = app / "무음제거 열기.bat"
    icon = app / "app.ico"
    if not target.is_file():
        return

    ps = f"""
$ws = New-Object -ComObject WScript.Shell
$targets = @()
$targets += [Environment]::GetFolderPath('Desktop')
$sm = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
if (Test-Path $sm) {{ $targets += $sm }}
foreach ($d in $targets) {{
  $lnk = $ws.CreateShortcut((Join-Path $d '무음제거 자막편집.lnk'))
  $lnk.TargetPath = '{target}'
  $lnk.WorkingDirectory = '{app}'
  $lnk.Description = 'TTS 무음 제거 + 자막 싱크 편집'
  {"$lnk.IconLocation = '" + str(icon) + ",0'" if icon.is_file() else ""}
  $lnk.WindowStyle = 7
  $lnk.Save()
}}
"""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-ExecutionPolicy", "Bypass", "-Command", ps],
                           capture_output=True, text=True, errors="replace")
        if r.returncode == 0:
            print("[O] 바탕화면·시작메뉴에 바로가기를 만들었습니다")
        else:
            print("[!] 바로가기 생성 실패 — '무음제거 열기.bat' 을 직접 쓰세요")
    except Exception:
        print("[!] 바로가기 생성 실패 — '무음제거 열기.bat' 을 직접 쓰세요")


def usage() -> None:
    print("""
  오디오 파일 또는 폴더를 run.bat 위로 끌어다 놓으세요.

    · 같은 이름의 .srt 가 옆에 있으면 자막 싱크까지 맞춰줍니다.
    · 여러 개를 한 번에 끌어다 놓아도 됩니다.
    · 결과는 원본 옆에 _clean 이름으로 저장됩니다.

  명령줄로 쓰려면:
    python desilence.py "폴더경로"
    python desilence.py "파일.mp3" -c 12        (자막 12자로)
    python desilence.py "파일.mp3" --no-align   (싱크 보정 끄기)
""")



def fetch_ffmpeg() -> bool:
    """ffmpeg 을 프로그램 폴더의 bin/ 에 받는다 (시스템은 건드리지 않는다)."""
    import io
    import urllib.request
    import zipfile

    url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    print("    받는 중 - 106MB, 몇 분 걸립니다...", flush=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "desilence-setup"})
        with urllib.request.urlopen(req, timeout=300) as r:
            data = r.read()
    except Exception as e:
        print(f"    다운로드 실패: {type(e).__name__}")
        return False

    localbin.BIN.mkdir(parents=True, exist_ok=True)
    got = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                name = n.rsplit("/", 1)[-1]
                if name in ("ffmpeg.exe", "ffprobe.exe"):
                    (localbin.BIN / name).write_bytes(z.read(n))
                    got += 1
    except Exception as e:
        print(f"    압축 풀기 실패: {type(e).__name__}")
        return False
    if got < 2:
        print("    압축 안에서 ffmpeg 을 찾지 못했습니다.")
        return False
    print(f"    [O] {localbin.BIN} 에 넣었습니다")
    return True


def main() -> int:
    if "--usage" in sys.argv:
        usage()
        return 0

    print("\n" + "=" * 58)
    print("   TTS 무음제거 + 자막싱크  ·  설치")
    print("=" * 58 + "\n")
    print(f"[O] Python {sys.version.split()[0]}")

    # 1) ffmpeg — winget 에 기대지 않는다. 없는 PC 도 있고, 설치돼도
    #    PATH 가 갱신되지 않아 또 막힌다. 프로그램 폴더에 직접 받는다.
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("[X] ffmpeg 가 없습니다. 프로그램 폴더에 직접 받습니다.\n")
        if not fetch_ffmpeg():
            print("\n    받지 못했습니다. 인터넷 연결을 확인하세요.")
            print("    직접 받으려면 https://www.gyan.dev/ffmpeg/builds/ 에서")
            print("    essentials 압축을 받아 ffmpeg.exe, ffprobe.exe 를")
            print(f"    {localbin.BIN} 에 넣으세요.")
            return 1
        localbin.register()
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            print("    [X] 받았는데도 찾지 못합니다.")
            return 1
    print("[O] ffmpeg")

    # 2) 파이썬 패키지 (이미 있으면 건드리지 않는다 — 기존 환경 보호)
    print("\n--- 필수 패키지 확인 중 ---")
    missing = []
    for mod, name in (("numpy", "numpy"), ("faster_whisper", "faster-whisper")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(name)
    if missing:
        print(f"    설치할 항목: {', '.join(missing)}")
        if not pip("install", "-q", *missing):
            print("\n[X] 패키지 설치 실패. 인터넷 연결을 확인하세요.")
            return 1
    print("[O] numpy, faster-whisper")

    # 3) GPU
    print()
    gpu = has_gpu()
    if gpu:
        name, cc = gpu
        print(f"[O] GPU 발견: {name}" + (f"  (연산능력 {cc})" if cc else ""))
        print("--- GPU 가속 라이브러리 확인 중 (없으면 약 1.1GB 다운로드) ---")
        ok = pip("install", "-q", *CUDA_PKGS)
        if cc >= 10.0:
            # RTX 50 시리즈 같은 최신 세대는 옛 라이브러리가 지원하지 않는다
            print("    최신 세대 GPU 입니다 — 음성인식 라이브러리를 최신으로 올립니다...")
            pip("install", "-q", "-U", "ctranslate2")
        print("[O] GPU 가속 준비 완료" if ok
              else "[!] GPU 라이브러리 설치 실패 — CPU 로 동작합니다 (느림)")
    else:
        print("[!] NVIDIA GPU 가 없습니다. CPU 로 동작합니다.")
        print("    자막 싱크 보정이 10배 이상 느립니다.")
        print("    빠르게 쓰려면 run.bat 에 --model small 을 붙이거나,")
        print("    --no-align 으로 싱크 보정을 끄세요.")

    # 4) 음성인식 모델
    print("\n음성인식 모델을 지금 받아둘까요? (약 1.5GB)")
    print("안 받으면 처음 실행할 때 자동으로 받습니다.")
    try:
        ans = input("지금 받기 [y/N]: ").strip().lower()
    except EOFError:
        ans = "n"
    if ans.startswith("y"):
        print("\n--- 모델 다운로드 중 ---")
        try:
            from faster_whisper import WhisperModel
            WhisperModel("medium", device="cpu", compute_type="int8")
            print("[O] 모델 준비 완료")
        except Exception as e:
            print(f"[!] 모델 다운로드 실패: {e}")
            print("    처음 실행할 때 다시 시도됩니다.")

    print()
    make_shortcuts()

    print("\n" + "=" * 58)
    print("   설치 완료")
    print("=" * 58)
    print("\n  바탕화면의 '무음제거 자막편집' 을 더블클릭하면 실행됩니다.")
    usage()
    return 0


if __name__ == "__main__":
    sys.exit(main())
