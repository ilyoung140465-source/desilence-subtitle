#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
to_capcut.py - 오디오 + SRT 로 캡컷(CapCut) 프로젝트를 새로 만든다.

기존 프로젝트는 절대 건드리지 않고, 새 폴더만 만든다.
캡컷 내부 포맷의 자잘한 필드들은 이 PC 에 이미 있는 프로젝트에서 읽어와
버전이 맞도록 하고, 트랙과 자막은 전부 새로 채운다.

    python to_capcut.py "소리.wav" "자막.srt" --name "0824_광고A"
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from desilence import parse_srt, Cue

US = 1_000_000                       # 캡컷은 시간을 마이크로초로 다룬다

RATIOS = {
    "9:16": (1080, 1920),
    "1:1":  (1080, 1080),
    "4:5":  (1080, 1350),
    "16:9": (1920, 1080),
}


def uid() -> str:
    return str(uuid.uuid4()).upper()


def us(sec: float) -> int:
    return int(round(sec * US))


# --------------------------------------------------------------------------
# 캡컷 위치 찾기
# --------------------------------------------------------------------------

def draft_root(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_dir():
            sys.exit(f"[!] 폴더가 없습니다: {p}")
        return p
    import os
    base = os.environ.get("LOCALAPPDATA", "")
    p = Path(base) / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
    if not p.is_dir():
        sys.exit("[!] 캡컷 프로젝트 폴더를 찾지 못했습니다.\n"
                 f"    기대 경로: {p}\n"
                 "    캡컷을 한 번 실행해 프로젝트를 만든 뒤 다시 시도하거나,\n"
                 "    --draft-dir 로 직접 지정하세요.")
    return p


def capcut_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq CapCut.exe"],
                             capture_output=True, text=True, errors="replace")
        return "CapCut.exe" in out.stdout
    except Exception:
        return False


# --------------------------------------------------------------------------
# 기존 프로젝트에서 '부품' 을 빌려온다 (읽기 전용)
# --------------------------------------------------------------------------

class Parts:
    """캡컷 버전에 맞는 필드 구성을 기존 프로젝트에서 가져온다."""

    def __init__(self, content: dict | None):
        self.src = content
        self.text_mat = self.text_seg = None
        self.audio_mat = self.audio_seg = None
        self.font_path = ""
        self.text_transform = {"x": 0.0, "y": -0.6}
        self.font_size = 11
        if content:
            self._extract(content)

    def _extract(self, j: dict) -> None:
        mats = j.get("materials", {})
        texts = mats.get("texts") or []
        auds = mats.get("audios") or []
        if texts:
            self.text_mat = texts[0]
            try:
                c = json.loads(self.text_mat.get("content") or "{}")
                st = (c.get("styles") or [{}])[0]
                self.font_path = (st.get("font") or {}).get("path", "") or ""
                self.font_size = st.get("size", 11)
            except Exception:
                pass
        if auds:
            self.audio_mat = auds[0]
        for t in j.get("tracks", []):
            segs = t.get("segments") or []
            if not segs:
                continue
            if t["type"] == "text" and self.text_seg is None:
                self.text_seg = segs[0]
                clip = segs[0].get("clip") or {}
                tr = clip.get("transform")
                if isinstance(tr, dict):
                    self.text_transform = tr
            elif t["type"] == "audio" and self.audio_seg is None:
                self.audio_seg = segs[0]

    @property
    def ok(self) -> bool:
        return all([self.text_mat, self.text_seg, self.audio_mat, self.audio_seg])


SKELETON = Path(__file__).with_name("capcut_skeleton.json")


def iter_projects(root: Path):
    """프로젝트들을 최근 수정순으로 하나씩 읽어준다 (읽기만 한다)."""
    files = []
    for d in root.iterdir():
        f = d / "draft_content.json"
        if d.is_dir() and f.is_file():
            files.append((f.stat().st_mtime, f))
    for _, f in sorted(files, reverse=True):
        try:
            yield json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue


def load_parts(root: Path) -> tuple["Parts", str]:
    """
    이 PC 의 프로젝트에서 부품을 찾고, 없으면 함께 배포되는 기본 골격을 쓴다.
    (캡컷을 막 설치해 프로젝트가 하나도 없는 PC 에서도 동작하게 하기 위함)
    """
    for i, content in enumerate(iter_projects(root)):
        p = Parts(content)
        if p.ok:
            return p, f"이 PC 의 기존 프로젝트 (최근 {i + 1}번째)"
    if SKELETON.is_file():
        try:
            p = Parts(json.loads(SKELETON.read_text(encoding="utf-8")))
            if p.ok:
                return p, "함께 배포된 기본 골격"
        except Exception:
            pass
    sys.exit("[!] 캡컷 프로젝트 구성 정보를 찾지 못했습니다.\n"
             "    캡컷에서 오디오와 자막이 있는 프로젝트를 하나 저장한 뒤 다시 실행하거나,\n"
             "    이 도구 폴더에 capcut_skeleton.json 이 있는지 확인하세요.")


def make_skeleton(root: Path, dst: Path) -> None:
    """이 PC 의 프로젝트에서 개인정보를 걷어낸 골격 파일을 만든다."""
    for content in iter_projects(root):
        p = Parts(content)
        if not p.ok:
            continue
        j = copy.deepcopy(content)
        # 기기 식별자 제거
        for key in ("platform", "last_modified_platform"):
            for f in ("device_id", "hard_disk_id", "mac_address"):
                if isinstance(j.get(key), dict) and f in j[key]:
                    j[key][f] = ""
        # 내용물은 형태만 남기고 비운다 (각 1개씩만)
        M = j["materials"]
        for k, v in list(M.items()):
            if isinstance(v, list) and k not in ("texts", "audios"):
                M[k] = []
        if M.get("audios"):
            a = M["audios"][0]
            a.update({"path": "", "name": "", "duration": 0})
            a.pop("wave_points", None)
            M["audios"] = [a]
        if M.get("texts"):
            t = M["texts"][0]
            t["content"] = text_content("샘플", "", 11)
            for f in ("font_path", "font_url", "font_resource_id", "font_id"):
                if f in t:
                    t[f] = ""
            M["texts"] = [t]
        for t in j.get("tracks", []):
            t["segments"] = t["segments"][:1]
        j["name"] = ""
        j["duration"] = 0
        dst.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
        print(f"[O] 골격 저장: {dst}  ({dst.stat().st_size:,} bytes)")
        return
    sys.exit("[!] 골격으로 쓸 만한 프로젝트를 찾지 못했습니다 (오디오+자막이 있는 프로젝트 필요).")


# --------------------------------------------------------------------------
# 프로젝트 조립
# --------------------------------------------------------------------------

def local_font(preferred: str) -> str:
    """
    폰트 경로는 PC 마다 다르다 (사용자명·캡컷 버전이 들어감).
    빌려온 경로가 이 PC 에 없으면 설치된 캡컷에서 다시 찾고, 그래도 없으면 비운다.
    """
    if preferred and Path(preferred).is_file():
        return preferred
    import os
    apps = Path(os.environ.get("LOCALAPPDATA", "")) / "CapCut" / "Apps"
    if apps.is_dir():
        cands = sorted(apps.glob("*/Resources/Font/SystemFont/en.ttf"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if cands:
            return str(cands[0]).replace("\\", "/")
    return ""


def text_content(text: str, font: str, size: int) -> str:
    return json.dumps({
        "text": text,
        "styles": [{
            "fill": {"content": {"render_type": "solid", "solid": {"color": [1, 1, 1]}}},
            "font": {"path": font, "id": ""},
            "size": size,
            "range": [0, len(text)],
        }],
    }, ensure_ascii=False)


def build_content(audio: Path, audio_sec: float, cues: list[Cue],
                  parts: Parts, width: int, height: int, ratio: str,
                  fps: float) -> dict:
    if not parts.ok:
        sys.exit("[!] 이 PC 의 캡컷 프로젝트에서 구성 정보를 읽지 못했습니다.\n"
                 "    캡컷에서 오디오와 자막이 있는 프로젝트를 하나 저장한 뒤 다시 시도하세요.")

    j = copy.deepcopy(parts.src)
    total = us(audio_sec)
    font = local_font(parts.font_path)

    # --- 초기화 ---
    j["id"] = uid()
    j["name"] = ""
    j["duration"] = total
    j["fps"] = float(fps)
    j["canvas_config"] = {"ratio": ratio, "width": width, "height": height, "background": None}
    j["relationships"] = []
    j["keyframe_graph_list"] = []
    j["lyrics_effects"] = []
    j["cover"] = None
    j["static_cover_image_path"] = ""
    for k in list(j.get("keyframes", {})):
        j["keyframes"][k] = []
    for k, v in list(j.get("materials", {}).items()):
        if isinstance(v, list):
            j["materials"][k] = []

    M = j["materials"]

    # --- 오디오 ---
    a_mat = copy.deepcopy(parts.audio_mat)
    a_mat.update({
        "id": uid(),
        "name": audio.name,
        "path": str(audio.resolve()).replace("\\", "/"),
        "duration": total,
        "local_material_id": uid(),
    })
    a_mat.pop("wave_points", None)
    M["audios"] = [a_mat]

    a_seg = copy.deepcopy(parts.audio_seg)
    a_seg["id"] = uid()
    a_seg["material_id"] = a_mat["id"]
    a_seg["source_timerange"] = {"start": 0, "duration": total}
    a_seg["target_timerange"] = {"start": 0, "duration": total}
    a_seg["keyframe_refs"] = []
    a_seg["common_keyframes"] = []

    # 오디오가 참조하는 부속 재료들을 새 id 로 다시 만든다
    refs, extras = [], {
        "speeds": {"type": "speed", "mode": 0, "speed": 1.0, "curve_speed": None},
        "placeholder_infos": {"type": "placeholder_info", "meta_type": "none", "res_path": "",
                              "res_text": "", "error_path": "", "error_text": ""},
        "beats": {"type": "beats", "enable_ai_beats": False, "gear": 404, "gear_count": 0,
                  "mode": 404, "user_beats": [], "user_delete_ai_beats": None,
                  "ai_beats": {"melody_url": "", "melody_path": "", "beats_url": "",
                               "beats_path": "", "melody_percents": [0.0], "beat_speed_infos": []}},
        "sound_channel_mappings": {"type": "", "audio_channel_mapping": 0, "is_config_open": False},
        "vocal_separations": {"type": "vocal_separation", "choice": 0, "removed_sounds": [],
                              "time_range": None, "production_path": "", "final_algorithm": "",
                              "enter_from": ""},
    }
    for kind, obj in extras.items():
        o = dict(obj, id=uid())
        M.setdefault(kind, []).append(o)
        refs.append(o["id"])
    a_seg["extra_material_refs"] = refs

    # --- 자막 ---
    t_segs = []
    for i, c in enumerate(cues):
        t_mat = copy.deepcopy(parts.text_mat)
        t_mat["id"] = uid()
        t_mat["content"] = text_content(c.text, font, parts.font_size)
        if "font_path" in t_mat:
            t_mat["font_path"] = font
        t_mat["base_content"] = ""
        for k in ("recognize_task_id", "words", "current_words", "relevance_segment",
                  "subtitle_keywords", "text_to_audio_ids"):
            if k in t_mat:
                t_mat[k] = [] if isinstance(t_mat[k], list) else ""
        M["texts"].append(t_mat)

        anim = {"id": uid(), "type": "sticker_animation", "animations": [],
                "multi_language_current": "none"}
        M.setdefault("material_animations", []).append(anim)

        s = copy.deepcopy(parts.text_seg)
        s["id"] = uid()
        s["material_id"] = t_mat["id"]
        s["extra_material_refs"] = [anim["id"]]
        start = us(c.start)
        s["source_timerange"] = None
        s["target_timerange"] = {"start": start, "duration": max(1, us(c.end) - start)}
        s["render_index"] = 14000 + i
        s["keyframe_refs"] = []
        s["common_keyframes"] = []
        if isinstance(s.get("clip"), dict):
            s["clip"]["transform"] = dict(parts.text_transform)
        t_segs.append(s)

    # --- 트랙 ---
    j["tracks"] = [
        {"id": uid(), "type": "video", "flag": 0, "attribute": 0,
         "name": "", "is_default_name": True, "segments": []},
        {"id": uid(), "type": "audio", "flag": 0, "attribute": 0,
         "name": "", "is_default_name": True, "segments": [a_seg]},
        {"id": uid(), "type": "text", "flag": 1, "attribute": 0,
         "name": "", "is_default_name": True, "segments": t_segs},
    ]
    return j


def build_meta(name: str, folder: Path, root: Path, audio: Path,
               srt: Path | None, total: int) -> dict:
    now = int(time.time())
    return {
        "cloud_draft_cover": False, "cloud_draft_sync": False,
        "cloud_package_completed_time": "",
        "draft_cloud_capcut_purchase_info": "", "draft_cloud_last_action_download": False,
        "draft_cloud_package_type": "", "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "", "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": "draft_cover.jpg", "draft_deeplink_url": "",
        "draft_enterprise_info": {"draft_enterprise_extra": "", "draft_enterprise_id": "",
                                  "draft_enterprise_name": "", "enterprise_material": None},
        "draft_fold_path": str(folder).replace("\\", "/"),
        "draft_id": uid(),
        "draft_is_ae_produce": False, "draft_is_ai_packaging_used": False,
        "draft_is_ai_shorts": False, "draft_is_ai_translate": False,
        "draft_is_article_video_draft": False, "draft_is_cloud_temp_draft": False,
        "draft_is_from_deeplink": "false", "draft_is_infinite_canvas_draft": False,
        "draft_is_invisible": False, "draft_is_pippit_draft": False,
        "draft_is_web_article_video": False,
        "draft_materials": [{"type": t, "value": []} for t in (0, 1, 2, 3, 6, 7, 8)],
        "draft_materials_copied_info": [],
        "draft_name": name,
        "draft_need_rename_folder": False, "draft_new_version": "",
        "draft_removable_storage_device": "",
        "draft_root_path": str(root),
        "draft_segment_extra_info": [],
        "draft_timeline_materials_size_": audio.stat().st_size if audio.exists() else 0,
        "draft_type": "", "draft_web_article_video_enter_from": "",
        "pippit_avatar_url": "", "pippit_extra_info": "", "pippit_id": "", "pippit_user_name": "",
        "tm_draft_cloud_completed": "", "tm_draft_cloud_entry_id": -1,
        "tm_draft_cloud_modified": 0, "tm_draft_cloud_parent_entry_id": -1,
        "tm_draft_cloud_space_id": -1, "tm_draft_cloud_user_id": -1,
        "tm_draft_create": now * US, "tm_draft_modified": now * US, "tm_draft_removed": 0,
        "tm_duration": total,
    }


# --------------------------------------------------------------------------

def write_project(folder: Path, content: dict, meta: dict) -> None:
    """프로젝트 폴더를 만들고 캡컷이 읽는 파일들을 쓴다."""
    folder.mkdir(parents=True)
    (folder / "draft_content.json").write_text(
        json.dumps(content, ensure_ascii=False), encoding="utf-8")
    (folder / "draft_meta_info.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (folder / "draft_virtual_store.json").write_text(
        json.dumps({"draft_materials": [], "draft_virtual_store": []}, ensure_ascii=False),
        encoding="utf-8")
    (folder / "draft_agency_config.json").write_text(json.dumps({
        "is_auto_agency_enabled": False, "is_auto_agency_popup": False,
        "is_single_agency_mode": False, "marterials": None,
        "use_converter": False, "video_resolution": 720}, ensure_ascii=False), encoding="utf-8")
    now = int(time.time())
    (folder / "draft_settings").write_text(
        f"[General]\ndraft_create_time={now}\ndraft_last_edit_time={now}\n"
        f"real_edit_seconds=0\nreal_edit_keys=0\n", encoding="utf-8")


def audio_duration(path: Path) -> float:
    exe = shutil.which("ffprobe")
    if not exe:
        sys.exit("[!] ffprobe 를 찾을 수 없습니다. ffmpeg 를 설치하세요.")
    r = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, errors="replace")
    try:
        return float(r.stdout.strip())
    except ValueError:
        sys.exit(f"[!] 오디오 길이를 읽지 못했습니다: {path}")


def safe_name(name: str) -> str:
    # BOM·제로폭·제어문자를 먼저 걷어낸다 (폴더 이름에 보이지 않는 문자가 섞이면 골치 아프다)
    n = re.sub(r"[﻿​-‏  \x00-\x1f]", "", name)
    n = re.sub(r'[<>:"/\\|?*]', "_", n).strip().strip(".").strip()
    return n or "무제"


def unique_folder(root: Path, name: str) -> Path:
    p = root / name
    if not p.exists():
        return p
    for i in range(1, 1000):
        q = root / f"{name} ({i})"
        if not q.exists():
            return q
    sys.exit("[!] 같은 이름의 프로젝트가 너무 많습니다.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="오디오와 SRT 로 캡컷 프로젝트를 새로 만듭니다.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("audio", nargs="?", help="오디오 파일 (wav/mp3)")
    ap.add_argument("srt", nargs="?", help="SRT 파일 (생략 시 오디오와 같은 이름을 찾음)")
    ap.add_argument("-n", "--name", help="캡컷에 표시될 프로젝트 이름 (생략 시 오디오 파일명)")
    ap.add_argument("-r", "--ratio", default="9:16", choices=list(RATIOS), help="화면 비율")
    ap.add_argument("--fps", type=float, default=30.0, help="프레임레이트")
    ap.add_argument("--draft-dir", help="캡컷 프로젝트 폴더를 직접 지정")
    ap.add_argument("--force", action="store_true", help="캡컷이 켜져 있어도 진행")
    ap.add_argument("--make-skeleton", action="store_true",
                    help="이 PC 의 프로젝트로 capcut_skeleton.json 을 만든다 "
                         "(캡컷이 업데이트되면 다시 실행)")
    args = ap.parse_args()

    if args.make_skeleton:
        make_skeleton(draft_root(args.draft_dir), SKELETON)
        return

    if not args.audio:
        ap.error("오디오 파일을 지정하세요.")
    audio = Path(args.audio)
    if not audio.is_file():
        sys.exit(f"[!] 오디오가 없습니다: {audio}")
    srt = Path(args.srt) if args.srt else audio.with_suffix(".srt")

    cues: list[Cue] = []
    if srt.is_file():
        cues = parse_srt(srt)
    else:
        print(f"[!] SRT 를 찾지 못했습니다 ({srt.name}) — 오디오만 넣습니다.", file=sys.stderr)

    root = draft_root(args.draft_dir)
    if capcut_running() and not args.force:
        sys.exit("[!] 캡컷이 실행 중입니다. 종료한 뒤 다시 실행하세요.\n"
                 "    (그냥 진행하려면 --force)")

    parts, origin = load_parts(root)
    dur = audio_duration(audio)
    w, h = RATIOS[args.ratio]

    name = safe_name(args.name or audio.stem)
    folder = unique_folder(root, name)

    content = build_content(audio, dur, cues, parts, w, h, args.ratio, args.fps)
    meta = build_meta(folder.name, folder, root, audio, srt if srt.is_file() else None,
                      us(dur))

    write_project(folder, content, meta)

    print(f"\n■ 캡컷 프로젝트 생성")
    print(f"  이름     : {folder.name}")
    print(f"  화면     : {w}x{h} ({args.ratio}) @ {args.fps:g}fps")
    print(f"  오디오   : {audio.name}  ({dur:.2f}초)")
    print(f"  자막     : {len(cues)}개")
    print(f"  위치     : {folder}")
    print(f"  구성정보 : {origin}")
    print(f"\n  캡컷을 실행하면 프로젝트 목록에 '{folder.name}' 이 보입니다.")
    print(f"  오디오 원본을 옮기거나 지우면 캡컷에서 파일을 찾지 못합니다.\n")


if __name__ == "__main__":
    main()
