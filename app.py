# -*- coding: utf-8 -*-
"""
app.py - 편집 화면(브라우저 UI)과 파이썬 엔진을 하나로 묶은 앱 본체.

브라우저에서 못 하는 일(Whisper 싱크 보정, 캡컷 프로젝트 생성)을
내 PC 안에서 도는 작은 서버가 대신 처리한다. 외부로 나가는 통신은 없다.

    python app.py
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import localbin  # 프로그램 폴더의 bin/ 을 PATH 에 붙인다

APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "web"
VERSION = (APP_DIR / "VERSION").read_text(encoding="utf-8").strip() \
    if (APP_DIR / "VERSION").is_file() else "dev"

MAX_UPLOAD = 512 * 1024 * 1024          # 512MB
uploads: dict[str, Path] = {}

# 원본 오디오의 음성 인식 결과를 담아 둔다.
# 무음 제거 설정을 바꿔도 '원본에서 들리는 말' 은 달라지지 않으므로,
# 파일당 한 번만 인식하고 시각만 새 타임라인으로 옮긴다.
# (예전에는 잘라낸 오디오를 매번 다시 인식해서 슬라이더 한 번에 20초씩 걸렸다)
_asr_cache: "dict[tuple, list]" = {}
_ASR_MAX = 4
work_dir = Path(os.environ.get("TEMP", ".")) / "desilence_app"
work_dir.mkdir(parents=True, exist_ok=True)


def source_words(uid: str, pcm, sr: int, ch: int, model: str, language: str):
    """원본 오디오에서 단어 시각을 얻는다 (같은 파일이면 캐시)."""
    import desilence as ds
    import aligner
    key = (uid, model, language)
    hit = _asr_cache.get(key)
    if hit is not None:
        return hit
    tmp = work_dir / f"src_{uuid.uuid4().hex}.wav"
    ds.encode(pcm, sr, ch, tmp)
    try:
        words = aligner.transcribe_words(tmp, model_size=model, language=language)
    finally:
        tmp.unlink(missing_ok=True)
    if len(_asr_cache) >= _ASR_MAX:
        _asr_cache.pop(next(iter(_asr_cache)))
    _asr_cache[key] = words
    return words


def shift_words(words, tmap):
    """원본 타임라인의 단어 시각을 잘라낸 뒤 타임라인으로 옮긴다."""
    import aligner
    out = []
    for w in words:
        s, e = tmap(w.start), tmap(w.end)
        if e <= s:                     # 통째로 잘려 나간 자리 (있어선 안 되지만 대비)
            continue
        out.append(aligner.Word(s, e, w.text))
    return out


def export_root() -> Path:
    """캡컷이 참조할 오디오를 둘 곳. 임시폴더는 지워지므로 문서 폴더에 둔다."""
    home = Path(os.environ.get("USERPROFILE", Path.home()))
    for name in ("Documents", "문서"):
        d = home / name
        if d.is_dir():
            return d / "CapCut내보내기"
    return home / "CapCut내보내기"


# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):        # 콘솔을 조용히
        pass

    # ---------- 공통 ----------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"error": msg}, code)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise ValueError("파일이 너무 큽니다")
        buf = bytearray()
        while len(buf) < n:
            chunk = self.rfile.read(min(1 << 20, n - len(buf)))
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/ping":
            import shutil
            gpu = False
            try:
                import ctranslate2
                gpu = ctranslate2.get_cuda_device_count() > 0
            except Exception:
                pass
            return self._json({
                "app": "desilence", "version": VERSION,
                "align": _has("faster_whisper"), "gpu": gpu,
                "ffmpeg": bool(shutil.which("ffmpeg")),
                "capcut": _capcut_ready(),
            })
        rel = "index.html" if path == "/" else path.lstrip("/")
        f = (WEB_DIR / rel).resolve()
        if not str(f).startswith(str(WEB_DIR)) or not f.is_file():
            return self._err("없는 경로입니다", 404)
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
                 ".css": "text/css", ".json": "application/json"}.get(f.suffix, "application/octet-stream")
        self._send(200, f.read_bytes(), ctype)

    # ---------- POST ----------
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/upload":
                return self._upload()
            if path == "/api/align":
                return self._align()
            if path == "/api/process":
                return self._process()
            if path == "/api/verify":
                return self._verify()
            if path == "/api/capcut":
                return self._capcut()
            return self._err("없는 경로입니다", 404)
        except Exception as e:
            return self._err(f"{type(e).__name__}: {e}", 500)

    def _upload(self):
        data = self._body()
        if not data:
            return self._err("빈 파일입니다")
        name = self.headers.get("X-Filename", "audio.wav")
        ext = Path(name).suffix or ".wav"
        # 내용으로 이름을 정한다. 같은 파일을 다시 올리면 인식 결과를 재사용한다.
        key = hashlib.sha1(data).hexdigest()[:16]
        p = work_dir / f"{key}{ext}"
        if not p.is_file() or p.stat().st_size != len(data):
            p.write_bytes(data)
        uploads[key] = p
        return self._json({"id": key, "bytes": len(data)})

    def _align(self):
        req = json.loads(self._body() or b"{}")
        p = uploads.get(req.get("id", ""))
        if not p or not p.is_file():
            return self._err("업로드된 오디오를 찾지 못했습니다")
        cues_in = req.get("cues") or []
        if not cues_in:
            return self._err("자막이 없습니다")

        from desilence import Cue
        import aligner
        if not aligner.available():
            return self._err("faster-whisper 가 설치돼 있지 않습니다")

        cues = [Cue(float(c["start"]), float(c["end"]), c["text"]) for c in cues_in]
        total = float(req.get("total") or (cues[-1].end if cues else 0))
        words = aligner.transcribe_words(
            p, model_size=req.get("model", "medium"),
            language=req.get("language", "ko"))
        rep = aligner.align(cues, words, total,
                            trust_shift=float(req.get("trustShift", 0.25)))
        return self._json({
            "applied": rep.applied, "reason": rep.reason,
            "ratio": round(rep.ratio, 3), "corrected": rep.corrected,
            "words": len(words),
            "cues": [{"start": c.start, "end": c.end, "text": c.text} for c in cues],
        })

    def _process(self):
        """
        원본 오디오 + 원본 SRT 로 전체 파이프라인을 돌려 최종 자막을 만든다.
        (웹이 자체 로직으로 다시 쪼개면 Python 결과와 갈라지므로, 여기서 한 번에 처리한다)
        """
        req = json.loads(self._body() or b"{}")
        p = uploads.get(req.get("id", ""))
        if not p or not p.is_file():
            return self._err("업로드된 오디오를 찾지 못했습니다")
        srt_text = req.get("srt") or ""
        if not srt_text.strip():
            return self._err("SRT 내용이 없습니다")

        import desilence as ds
        import aligner
        sr, ch, _ = ds.probe(p)
        pcm = ds.decode(p, sr, ch)
        an = ds.analyze(
            pcm, sr,
            threshold=(None if req.get("auto") else float(req.get("threshold", -30))),
            min_silence=float(req.get("minSilence", 0.20)),
            keep_silence=float(req.get("keep", 0.10)),
            edge_silence=0.05, trim_edges=True)
        tmap = ds.TimeMap(an)
        new_pcm = ds.render(pcm, an)
        total = an.new_sec
        max_chars = int(req.get("maxChars", 10))

        has_times = bool(req.get("hasTimes", True))
        if has_times:
            cues = ds.parse_srt_text(srt_text)
        else:
            cues = [ds.Cue(0.0, an.orig_sec, s) for s in ds.parse_script_text(srt_text)]
        for c in cues:
            c.text = ds.clean_text(c.text)
        mono_src = (pcm.astype("float32").mean(axis=1) if ch > 1
                    else pcm[:, 0].astype("float32"))
        src_db, _ = ds.frame_db(mono_src, sr)
        tight, saved = (ds.tighten_cues(cues, src_db, an.threshold_db)
                        if has_times else (0, 0.0))

        info = {"threshold": round(an.threshold_db, 1), "cuts": len(an.removes),
                "duration": total, "tightened": tight, "silenceRemoved": round(saved, 2),
                "method": "chars", "ratio": 0.0}

        out = None
        # 대본을 무시하고 '인식한 그대로' 자막을 만드는 방식
        if req.get("mode") == "asr" and aligner.available():
            words = shift_words(
                source_words(req.get("id", ""), pcm, sr, ch,
                             req.get("model", "medium"), req.get("language", "ko")),
                tmap)
            out = []
            buf, start = [], None
            for w in words:
                txt = w.text.strip()
                if not txt:
                    continue
                if start is None:
                    start = w.start
                cand = " ".join(buf + [txt])
                if buf and len(cand) > max_chars:
                    out.append(ds.Cue(start, words[words.index(w) - 1].end, " ".join(buf)))
                    buf, start = [txt], w.start
                else:
                    buf.append(txt)
            if buf and start is not None:
                out.append(ds.Cue(start, words[-1].end, " ".join(buf)))
            info.update(method="asr", words=len(words), ratio=1.0)

        if out is None and aligner.available() and cues:
            words = shift_words(
                source_words(req.get("id", ""), pcm, sr, ch,
                             req.get("model", "medium"), req.get("language", "ko")),
                tmap)
            mapped = []
            for c in cues:
                parts = [x for x in c.text.split(ds.BREAK) if x.strip()]
                for part in (parts or [c.text]):
                    mapped.append(ds.Cue(tmap(c.start), tmap(c.end), part.strip()))
            ratio, rows = aligner.word_times([c.text for c in mapped], words, total)
            if rows is None and ratio is not None and ratio < 0.30:
                heard = "".join(w.text for w in words)[:40]
                return self._err(
                    f"오디오와 자막이 서로 다른 내용 같습니다 (일치율 {ratio:.0%}). "
                    f"자막은 '{cues[0].text[:20] if cues else ''}…', "
                    f"소리는 '{heard}…' 입니다. 파일 짝을 확인하세요.")
            if rows:
                # 앵커는 기본으로 쓰지 않는다 (실측: 0.877 vs 0.918)
                if has_times and req.get("anchor"):
                    ds.anchor_sentences(mapped, rows)
                out = ds.resync_by_words(mapped, rows, tmap, max_chars=max_chars,
                                         min_cue=0.45, total_new=total,
                                         bias=(-0.015 if has_times else 0.065))
                info.update(method="words", ratio=round(ratio, 3), words=len(words))

        if out is None and not has_times:
            return self._err("대본 텍스트는 음성 인식이 있어야 합니다. "
                             "faster-whisper 설치를 확인하세요.")
        if out is None:                       # 인식을 못 쓰면 글자 수 기준
            mono_new = (new_pcm.astype("float32").mean(axis=1) if ch > 1
                        else new_pcm[:, 0].astype("float32"))
            new_db, _ = ds.frame_db(mono_new, sr)
            out = ds.resync(cues, tmap, max_chars=max_chars, min_cue=0.45, fill=False,
                            new_db=new_db, snap_window=0.20, total_new=total)
        if not req.get("keepGaps"):
            ds.apply_fill(out, total)
        info["cues"] = len(out)
        return self._json({"info": info,
                           "cues": [{"start": c.start, "end": c.end, "text": c.text}
                                    for c in out]})

    def _verify(self):
        """자막이 실제 발화보다 얼마나 앞/뒤인지 자막마다 재서 돌려준다 (수정 안 함)."""
        req = json.loads(self._body() or b"{}")
        p = uploads.get(req.get("id", ""))
        if not p or not p.is_file():
            return self._err("업로드된 오디오를 찾지 못했습니다")
        cues_in = req.get("cues") or []
        if not cues_in:
            return self._err("자막이 없습니다")
        from desilence import Cue
        import aligner
        if not aligner.available():
            return self._err("faster-whisper 가 설치돼 있지 않습니다")
        cues = [Cue(float(c["start"]), float(c["end"]), c["text"]) for c in cues_in]
        total = float(req.get("total") or cues[-1].end)
        words = aligner.transcribe_words(p, model_size=req.get("model", "medium"),
                                         language=req.get("language", "ko"))
        ratio, rows = aligner.measure(cues, words, total)
        return self._json({"ratio": round(ratio or 0, 3), "words": len(words), "rows": rows})

    def _capcut(self):
        req = json.loads(self._body() or b"{}")
        p = uploads.get(req.get("id", ""))
        if not p or not p.is_file():
            return self._err("업로드된 오디오를 찾지 못했습니다")
        name = (req.get("name") or "무제").strip()
        cues = req.get("cues") or []

        import to_capcut as tc
        safe = tc.safe_name(name)
        out = export_root() / safe
        out.mkdir(parents=True, exist_ok=True)
        wav = out / f"{safe}.wav"
        wav.write_bytes(p.read_bytes())

        from desilence import Cue, write_srt
        cue_objs = [Cue(float(c["start"]), float(c["end"]), c["text"]) for c in cues]
        srt = out / f"{safe}.srt"
        if cue_objs:
            write_srt(cue_objs, srt)

        if tc.capcut_running():
            return self._err("캡컷이 실행 중입니다. 캡컷을 끄고 다시 시도하세요.")

        root = tc.draft_root(None)
        parts, origin = tc.load_parts(root)
        dur = tc.audio_duration(wav)
        ratio = req.get("ratio", "9:16")
        w, h = tc.RATIOS.get(ratio, tc.RATIOS["9:16"])
        folder = tc.unique_folder(root, safe)
        content = tc.build_content(wav, dur, cue_objs, parts, w, h, ratio,
                                   float(req.get("fps", 30)))
        meta = tc.build_meta(folder.name, folder, root, wav, srt, tc.us(dur))
        tc.write_project(folder, content, meta)
        return self._json({"name": folder.name, "folder": str(folder),
                           "audio": str(wav), "cues": len(cue_objs),
                           "ratio": ratio, "origin": origin})


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def _capcut_ready() -> bool:
    try:
        import to_capcut as tc
        tc.draft_root(None)
        return True
    except SystemExit:
        return False
    except Exception:
        return False


# --------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="무음 제거 + 자막 편집 앱")
    ap.add_argument("--port", type=int, default=0, help="포트 (0 이면 자동)")
    ap.add_argument("--no-browser", action="store_true", help="브라우저를 열지 않음")
    args = ap.parse_args()

    if not (WEB_DIR / "index.html").is_file():
        sys.exit(f"[!] 편집 화면 파일이 없습니다: {WEB_DIR / 'index.html'}")
    port = args.port or free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"

    print(f"\n  무음 제거 + 자막 편집  ({VERSION})")
    print(f"  {url}")
    print(f"  이 창을 닫으면 프로그램이 종료됩니다.\n")

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("  종료합니다.")


if __name__ == "__main__":
    main()
