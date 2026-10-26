# -*- coding: utf-8 -*-
"""
typecast.py - 타입캐스트 API 로 대본을 음성으로 만든다.

음성과 함께 '각 단어가 몇 초에 나왔는지' 를 받는다 (with-timestamps).
만든 쪽이 직접 알려주는 시각이라, 음성을 다시 알아듣게 해서 추정하는
Whisper 정렬이 필요 없다.

API 키는 이 PC 의 사용자 폴더에만 둔다 (%APPDATA%\\desilence\\typecast.json).
프로그램 폴더·배포 압축·GitHub 에는 들어가지 않는다.
키는 api.typecast.ai 로만 보낸다.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

API_BASE = os.environ.get("TYPECAST_API_BASE", "https://api.typecast.ai").rstrip("/")
MAX_CHARS = 1900            # 요청당 한도 2000자에서 여유를 둔다
TIMEOUT = 120

_voices_cache: tuple[float, list] | None = None
_VOICES_TTL = 600


class TypecastError(Exception):
    """사용자에게 그대로 보여줄 수 있는 오류."""


# --------------------------------------------------------------------------
# API 키
# --------------------------------------------------------------------------

def _config_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "desilence" / "typecast.json"


def get_key() -> str:
    env = os.environ.get("TYPECAST_API_KEY", "").strip()
    if env:
        return env
    p = _config_path()
    if p.is_file():
        try:
            return str(json.loads(p.read_text(encoding="utf-8")).get("api_key", "")).strip()
        except Exception:
            return ""
    return ""


def set_key(key: str) -> None:
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"api_key": key.strip()}), encoding="utf-8")


def clear_key() -> None:
    _config_path().unlink(missing_ok=True)


def masked(key: str) -> str:
    return (key[:4] + "…" + key[-4:]) if len(key) > 10 else "설정됨"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _request(method: str, path: str, *, key: str | None = None, body: dict | None = None,
             query: str = "") -> bytes:
    key = key if key is not None else get_key()
    if not key:
        raise TypecastError("타입캐스트 API 키가 없습니다. 먼저 키를 넣어 주세요.")
    url = f"{API_BASE}{path}" + (f"?{query}" if query else "")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "X-API-KEY": key,
        "Content-Type": "application/json",
        "User-Agent": "desilence-typecast",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            raw = e.read().decode("utf-8", "replace")
            j = json.loads(raw)
            detail = str(j.get("detail") or j.get("message") or j.get("error") or raw)
        except Exception:
            pass
        msg = {
            401: "API 키가 맞지 않습니다. 키를 다시 확인해 주세요.",
            402: "크레딧이 부족합니다. 타입캐스트 API 요금제를 확인해 주세요.",
            403: "이 키로는 쓸 수 없는 기능입니다.",
            404: "목소리를 찾지 못했습니다. 다른 목소리를 골라 주세요.",
            429: "요청이 너무 많습니다. 잠시 뒤 다시 시도해 주세요.",
        }.get(e.code, f"타입캐스트 오류 ({e.code})")
        raise TypecastError(msg + (f"\n{detail[:200]}" if detail else "")) from None
    except urllib.error.URLError as e:
        raise TypecastError(f"타입캐스트에 연결하지 못했습니다. 인터넷을 확인해 주세요. ({e.reason})") from None


# --------------------------------------------------------------------------
# 조회
# --------------------------------------------------------------------------

def subscription(key: str | None = None) -> dict:
    j = json.loads(_request("GET", "/v1/users/me/subscription", key=key))
    cr = j.get("credits") or {}
    total, used = int(cr.get("plan_credits") or 0), int(cr.get("used_credits") or 0)
    return {"plan": j.get("plan", ""), "total": total, "used": used,
            "left": max(0, total - used)}


def list_voices(force: bool = False) -> list[dict]:
    """쓸 수 있는 목소리. 화면에 필요한 것만 추린다."""
    global _voices_cache
    if not force and _voices_cache and time.time() - _voices_cache[0] < _VOICES_TTL:
        return _voices_cache[1]
    raw = json.loads(_request("GET", "/v3/voices"))
    out = []
    for v in raw:
        models = {m.get("version"): m.get("emotions") or ["normal"] for m in v.get("models") or []}
        model = "ssfm-v30" if "ssfm-v30" in models else ("ssfm-v21" if "ssfm-v21" in models else None)
        if not model:
            continue
        name = v.get("voice_name") or {}
        out.append({
            "id": v.get("voice_id"),
            "name": name.get("kor") or name.get("eng") or v.get("voice_id"),
            "eng": name.get("eng", ""),
            "gender": v.get("gender"), "age": v.get("age"),
            "custom": v.get("voice_type") == "custom",
            "useCases": v.get("use_cases") or [],
            "preview": v.get("preview_url"),
            "model": model, "emotions": models[model],
        })
    _voices_cache = (time.time(), out)
    return out


# --------------------------------------------------------------------------
# 음성 만들기
# --------------------------------------------------------------------------

def split_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    """요청당 글자 한도를 넘으면 문장 경계에서 나눈다."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts = re.split(r"(?<=[.!?。…\n])\s*", text)
    chunks, cur = [], ""
    for p in parts:
        if not p:
            continue
        while len(p) > limit:                 # 문장 하나가 한도보다 길면 공백에서 자른다
            cut = p.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                chunks.append(cur.strip()); cur = ""
            chunks.append(p[:cut].strip()); p = p[cut:].strip()
        if len(cur) + len(p) + 1 > limit:
            chunks.append(cur.strip()); cur = p
        else:
            cur = (cur + " " + p) if cur else p
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def _one(text: str, voice: dict, emotion: str, intensity: float,
         tempo: float, pitch: int) -> tuple[bytes, list[dict]]:
    if voice["model"] == "ssfm-v30":
        if emotion == "smart":
            prompt = {"emotion_type": "smart"}
        else:
            prompt = {"emotion_type": "preset", "emotion_preset": emotion,
                      "emotion_intensity": intensity}
    else:
        prompt = {"emotion_preset": emotion if emotion != "smart" else "normal",
                  "emotion_intensity": intensity}
    body = {
        "text": text, "voice_id": voice["id"], "model": voice["model"],
        "language": "kor", "prompt": prompt,
        "output": {"audio_format": "wav", "audio_tempo": tempo, "audio_pitch": pitch},
    }
    j = json.loads(_request("POST", "/v1/text-to-speech/with-timestamps",
                            body=body, query="granularity=word"))
    audio = base64.b64decode(j.get("audio") or "")
    if not audio:
        raise TypecastError("타입캐스트가 빈 음성을 돌려줬습니다.")
    return audio, j.get("words") or []


def synthesize(text: str, voice_id: str, *, emotion: str = "normal",
               intensity: float = 1.0, tempo: float = 1.0, pitch: int = 0
               ) -> tuple[bytes, list[dict], dict]:
    """
    대본 -> (wav 바이트, 단어 시각 [{start,end,text}], 정보).
    길면 나눠 만든 뒤 이어 붙이고, 뒤 조각의 단어 시각은 앞 조각 길이만큼 민다.
    """
    voice = next((v for v in list_voices() if v["id"] == voice_id), None)
    if voice is None:
        raise TypecastError("고른 목소리를 찾지 못했습니다. 목록을 새로고침해 주세요.")
    if emotion != "smart" and emotion not in voice["emotions"]:
        emotion = "normal"

    chunks = split_text(text)
    if not chunks:
        raise TypecastError("대본이 비어 있습니다.")

    frames, words, params, offset = [], [], None, 0.0
    for c in chunks:
        audio, ws = _one(c, voice, emotion, intensity, tempo, pitch)
        with wave.open(io.BytesIO(audio)) as w:
            p = w.getparams()
            if params is None:
                params = p
            elif (p.nchannels, p.sampwidth, p.framerate) != \
                 (params.nchannels, params.sampwidth, params.framerate):
                raise TypecastError("조각마다 음성 형식이 다릅니다.")
            frames.append(w.readframes(p.nframes))
            dur = p.nframes / p.framerate          # audio_duration 대신 실제 샘플 수로
        for x in ws:
            words.append({"start": float(x["start"]) + offset,
                          "end": float(x["end"]) + offset,
                          "text": str(x.get("text", ""))})
        offset += dur

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        w.writeframes(b"".join(frames))
    info = {"voice": voice["name"], "chars": len(text.strip()), "chunks": len(chunks),
            "duration": round(offset, 3), "words": len(words), "emotion": emotion}
    return buf.getvalue(), words, info
