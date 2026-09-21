"""CapCut (macOS, international) adapter.

Recent CapCut desktop builds on macOS store each project as plain-JSON
``draft_info.json`` (plus an identical copy under ``Timelines/<timeline id>/``)
instead of Jianying's ``draft_content.json``, and use a richer schema. This
module loads such a project through the normal `ScriptFile` API and, on save:

- keeps existing tracks in their order and stacks new video tracks right
  above the main track (CapCut layers by track position), so captions and
  overlays stay on top of the b-roll;
- completes new video/photo segments and materials with the fields CapCut
  writes itself (see ``assets/capcut_mac_defaults.json``) and links the
  auxiliary materials CapCut expects on every video segment;
- converts new ids to CapCut's upper-case UUID format;
- writes both copies of ``draft_info.json`` after backing them up.

Only edit a project while CapCut is closed; CapCut overwrites the files with
its in-memory state when it saves.
"""

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from copy import deepcopy
from typing import Any, Dict, List, Optional, Set, Tuple

from . import assets
from .capcut_assets import TRACK_KINDS, TRACK_TYPES, build_caption, build_sound_segment, build_text_segment, build_track_segment
from .capcut_assets import attach as _attach_asset
from .script_file import ScriptFile

DEFAULT_DRAFTS_ROOT = os.path.expanduser("~/Movies/CapCut/User Data/Projects/com.lveditor.draft")

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_TEXT_RENDER_BASE = 14000


def capcut_is_running() -> bool:
    """Whether the CapCut desktop app is currently running"""
    return subprocess.run(["pgrep", "-f", "CapCut.app/Contents/MacOS"],
                          stdout=subprocess.DEVNULL).returncode == 0


def _ensure_closed() -> None:
    if capcut_is_running():
        raise RuntimeError("CapCut is running; close it before editing drafts (it would overwrite the changes)")


def _load_defaults() -> Dict[str, Any]:
    path = os.path.join(os.path.dirname(assets.__file__), "capcut_mac_defaults.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _fill(target: Dict[str, Any], defaults: Dict[str, Any]) -> None:
    """Recursively add keys missing from `target`, never overwriting existing values"""
    for key, value in defaults.items():
        if key not in target:
            target[key] = deepcopy(value)
        elif isinstance(target[key], dict) and isinstance(value, dict):
            _fill(target[key], value)


def _media_duration_us(path: str) -> Optional[int]:
    """Duration of a media file in microseconds, via ffprobe; None if it cannot be read"""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path],
                             capture_output=True, text=True, timeout=20)
        return int(round(float(out.stdout.strip()) * 1e6))
    except Exception:
        return None


def _new_uuid() -> str:
    return str(uuid.uuid4()).upper()


class CapCutMacDraft:
    """A CapCut for macOS project opened for editing through `ScriptFile`"""

    draft_dir: str
    script: ScriptFile
    """Edit this as usual: append tracks, add segments, keyframes..."""

    def __init__(self, draft_dir: str):
        self.draft_dir = draft_dir
        self.info_path = os.path.join(draft_dir, "draft_info.json")
        if not os.path.isfile(self.info_path):
            raise FileNotFoundError(f"{self.info_path} not found; is this a CapCut for macOS project?")
        with open(self.info_path, encoding="utf-8") as f:
            self._original = json.load(f)
        self.script = ScriptFile._load_template(self.info_path)
        self._defaults = _load_defaults()
        self._pending_assets: List[Tuple[str, Dict[str, Any], Optional[int]]] = []
        self._pending_tracks: List[Dict[str, Any]] = []
        self._removed_tracks: Set[str] = set()
        self._new_media: List[Tuple[str, str]] = []

    @classmethod
    def open(cls, draft_name: str, root: str = DEFAULT_DRAFTS_ROOT) -> "CapCutMacDraft":
        return cls(os.path.join(root, draft_name))

    def import_media(self, path: str, subdir: str = "broll") -> str:
        """Copy a media file into the project folder and return the new path.

        CapCut is sandboxed: it only reads files in places the user granted it
        (media imported through its UI, its own project folders). Files under
        e.g. ~/Documents show up as "media not found", so copy them in first.
        """
        dst_dir = os.path.join(self.draft_dir, "Resources", subdir)
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, os.path.basename(path))
        if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(path):
            shutil.copy2(path, dst)
        return dst

    def relink_media(self, old_path: str, new_path: str) -> int:
        """Point existing materials using `old_path` to `new_path`. Returns how many changed"""
        changed = 0
        for items in self.script.imported_materials.values():
            if not isinstance(items, list):
                continue
            for m in items:
                if isinstance(m, dict) and m.get("path") == old_path:
                    m["path"] = new_path
                    changed += 1
        return changed

    def segments(self, track_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Existing segments as {id, track_index, track_type, start, end, text} (seconds), for picking targets"""
        by_id = {m["id"]: m for items in self._original["materials"].values() if isinstance(items, list)
                 for m in items if isinstance(m, dict) and "id" in m}
        out = []
        for idx, track in enumerate(self._original["tracks"]):
            if (track_type and track["type"] != track_type) or track["id"] in self._removed_tracks:
                continue
            for seg in track["segments"]:
                tr = seg["target_timerange"]
                text = ""
                mat = by_id.get(seg.get("material_id"), {})
                if isinstance(mat.get("content"), str):
                    try:
                        text = json.loads(mat["content"]).get("text", "")
                    except ValueError:
                        pass
                source = seg.get("source_timerange") or {}
                out.append({"id": seg["id"], "track_index": idx, "track_type": track["type"],
                            "start": tr["start"] / 1e6, "end": (tr["start"] + tr["duration"]) / 1e6,
                            "source_start": source.get("start", 0) / 1e6 if source else None,
                            "material": mat.get("name") or os.path.basename(mat.get("path", "")),
                            "path": mat.get("path", ""), "text": text})

        # segments added in this session (they only exist in the draft after save())
        for offset, track in enumerate(self.script.tracks.values()):
            if track_type and track.track_type.name != track_type:
                continue
            for seg in track.segments:
                out.append({"id": seg.segment_id, "track_index": len(self._original["tracks"]) + offset,
                            "track_type": track.track_type.name, "name": track.name,
                            "start": seg.target_timerange.start / 1e6,
                            "end": seg.target_timerange.end / 1e6,
                            "source_start": None, "material": "", "path": "", "text": "", "new": True})
        return out

    def segment_at(self, seconds: float, track_type: str = "video", track_index: Optional[int] = None) -> str:
        """Id of the segment covering `seconds` (main video track by default)"""
        hits = [s for s in self.segments(track_type) if s["start"] <= seconds < s["end"]
                and (track_index is None or s["track_index"] == track_index)]
        if not hits:
            raise LookupError(f"no {track_type} segment at {seconds}s")
        return min(hits, key=lambda s: s["track_index"])["id"]

    def main_track_id(self) -> str:
        """Id of the main video track: the bottom video layer, not an overlay (flag 2)"""
        track = next((t for t in self._original["tracks"] if t["type"] == "video"
                      and t.get("flag") != 2 and t["id"] not in self._removed_tracks), None)
        if track is None:
            raise LookupError("this draft has no main video track")
        return track["id"]

    def remove_track(self, track_id: str) -> int:
        """Drop an existing track (e.g. the raw main track before rebuilding it with cuts).

        Returns how many segments were removed. Its materials stay in the draft;
        CapCut prunes the unused ones the next time it saves.
        """
        self._removed_tracks.add(track_id)
        keep = [t for t in self.script.imported_tracks if t.track_id != track_id]
        removed = len(self.script.imported_tracks) - len(keep)
        if not removed:
            raise KeyError(f"track {track_id} not found among the draft's own tracks")
        segments = sum(len(t["segments"]) for t in self._original["tracks"] if t["id"] == track_id)
        self.script.imported_tracks = keep
        return segments

    def attach_asset(self, asset: Dict[str, Any], segment_id: str, duration: Optional[float] = None) -> None:
        """Attach an asset from `AssetIndex` (transition, animation, text effect, mask...) to a segment.

        `segment_id` is an existing segment id (see `segments()`/`segment_at()`) or
        the `segment_id` of a segment added through `script`. Transitions go on the
        clip BEFORE the cut. `duration` (seconds) overrides the asset's default.
        Applied when saving.
        """
        self._pending_assets.append((segment_id, asset, None if duration is None else int(round(duration * 1e6))))

    def add_caption(self, template: Dict[str, Any], text: str, start: float, duration: float, *,
                    words: Optional[List[Tuple[str, float, float]]] = None, y: Optional[float] = None,
                    scale: Optional[float] = None) -> str:
        """Add a caption using a caption/text template from `AssetIndex` (e.g. "Rebote de colores 2").

        `start`/`duration` in seconds. `words`: optional [(word, start, end)] in
        seconds relative to the caption start (e.g. from speech recognition) so
        word-by-word highlights follow the voice; evenly timed otherwise.
        `y` moves it vertically (half-canvas units, up is positive). Returns the segment id.
        """
        duration_us = int(round(duration * 1e6))
        word_list = None if words is None else [
            {"text": w, "start": int(round(a * 1e6)), "end": int(round(b * 1e6))} for w, a, b in words]
        materials, seg = build_caption(template, self._defaults["segments"]["caption"], text,
                                       int(round(start * 1e6)), duration_us, word_list, y, scale)
        return self._queue_segment("caption", materials, seg)

    def add_track_asset(self, asset: Dict[str, Any], start: float, duration: float, *, x: float = 0.0,
                        y: float = 0.0, scale: Optional[float] = None, intensity: Optional[float] = None) -> str:
        """Add a video effect, filter or sticker from `AssetIndex` on its own track. Returns the segment id.

        Video effects and filters go right above the main video (and new b-roll)
        tracks, so they apply to the footage but not to captions or overlays;
        stickers go on top. `x`/`y`/`scale` position stickers; `intensity` (0-1) sets filter strength.
        """
        kind = TRACK_KINDS.get(asset["kind"])
        if kind not in ("video_effect", "filter", "sticker"):
            raise ValueError(f"{asset['display_name']} ({asset['kind']}): use add_caption/attach_asset instead")
        materials, seg = build_track_segment(asset, self._defaults["segments"][kind], int(round(start * 1e6)),
                                             int(round(duration * 1e6)), x=x, y=y, scale=scale, intensity=intensity)
        return self._queue_segment(kind, materials, seg)

    def add_text(self, text: str, start: float, duration: float, *, y: float = -0.56, x: float = 0.0,
                 size: float = 15.0, font: Any = None, highlight: Optional[List[str]] = None,
                 highlight_font: Any = None, highlight_size: Optional[float] = None,
                 color: Tuple[float, float, float] = (1.0, 1.0, 1.0),
                 highlight_color: Tuple[float, float, float] = (1.0, 0.85, 0.2),
                 stroke: float = 0.06, scale: float = 1.0) -> str:
        """Add a plain CapCut text box (the "Text" tool, not a caption template). Returns the segment id.

        This is the path that works today: caption templates written by script are
        not drawn by CapCut. `font` and `highlight_font` take a font asset from
        `AssetIndex` or a path to a .ttf/.otf; the words listed in `highlight` are
        drawn with the second font, colour and size. `y` = -0.56 is chest height.
        """
        materials, seg = build_text_segment(
            self._defaults["segments"]["text"], self._defaults["materials"]["text"], text,
            int(round(start * 1e6)), int(round(duration * 1e6)), font=font, highlight=highlight,
            highlight_font=highlight_font, color=color, highlight_color=highlight_color, size=size,
            highlight_size=highlight_size, stroke=stroke, y=y, x=x, scale=scale)
        return self._queue_segment("text", materials, seg)

    def add_sound_effect(self, path: str, start: float, duration: float, *, source_start: float = 0.0,
                         volume: float = 1.0) -> str:
        """Add a local audio file (sound effect) on its own audio track. Returns the segment id.

        `start` is where it sounds in the timeline, `duration` how long, and
        `source_start` where the cut starts inside the file (all in seconds).
        `volume` 1.0 is the file's own level. Overlapping effects open a new
        audio track automatically; existing audio is left untouched.
        """
        materials, seg = build_sound_segment(
            path, self._defaults["segments"]["sound"], self._defaults["materials"]["audio"],
            self._defaults["audio_aux"], int(round(start * 1e6)), int(round(duration * 1e6)),
            source_start_us=int(round(source_start * 1e6)), volume=volume,
            file_duration_us=_media_duration_us(path))
        self._new_media.append(("music", path))
        return self._queue_segment("sound", materials, seg)

    def add_asset(self, asset: Dict[str, Any], start: float, duration: float, **kwargs: Any) -> str:
        """Dispatch to `add_caption` (needs text=...) or `add_track_asset` by asset kind"""
        if asset["kind"] == "text_template":
            return self.add_caption(asset, kwargs.pop("text"), start, duration, **kwargs)
        return self.add_track_asset(asset, start, duration, **kwargs)

    def _queue_segment(self, kind: str, materials: List[Tuple[str, Dict[str, Any]]], seg: Dict[str, Any]) -> str:
        tr = seg["target_timerange"]
        begin, end = tr["start"], tr["start"] + tr["duration"]
        track = next((t for t in self._pending_tracks if t["kind"] == kind and all(
            end <= o["target_timerange"]["start"] or begin >= o["target_timerange"]["start"] + o["target_timerange"]["duration"]
            for o in t["segments"])), None)
        if track is None:
            track = {"kind": kind, "segments": [], "materials": []}
            self._pending_tracks.append(track)
        track["segments"].append(seg)
        track["materials"] += materials
        return seg["id"]

    def _insert_pending_tracks(self, data: Dict[str, Any], above_video_idx: int) -> None:
        tracks = data["tracks"]
        ri = {"caption": max([s["render_index"] for t in tracks if t["type"] in ("text", "sticker")
                              for s in t["segments"]] + [_TEXT_RENDER_BASE - 1]) + 1}
        ri["sticker"] = ri["text"] = ri["caption"]
        low, top = [], []
        for pending in self._pending_tracks:
            kind = pending["kind"]
            for mlist, m in pending["materials"]:
                data["materials"].setdefault(mlist, []).append(m)
            segs = sorted(pending["segments"], key=lambda sg: sg["target_timerange"]["start"])
            if kind in ri:
                for sg in segs:
                    sg["render_index"] = ri[kind]
                ri["caption"] = ri["sticker"] = ri["text"] = ri[kind] + 1
            track = {**deepcopy(self._defaults["tracks"][kind]), "id": _new_uuid(), "segments": segs}
            track["type"] = TRACK_TYPES[kind]
            (low if kind in ("video_effect", "filter") else top).append(track)
            data["duration"] = max([data["duration"]] + [sg["target_timerange"]["start"] + sg["target_timerange"]["duration"]
                                                         for sg in segs])
        tracks[above_video_idx:above_video_idx] = low
        # CapCut keeps the audio tracks last; new visual tracks go before them
        visual = [t for t in top if t["type"] != "audio"]
        audio = [t for t in top if t["type"] == "audio"]
        first_audio = next((i for i, t in enumerate(tracks) if t["type"] == "audio"), len(tracks))
        tracks[first_audio:first_audio] = visual
        tracks.extend(audio)
        for idx, t in enumerate(tracks):
            for sg in t["segments"]:
                sg["track_render_index"] = idx

    # ------------------------------------------------------------------ saving

    def export_json(self, new_video_tracks: str = "above_main") -> Dict[str, Any]:
        """Return the CapCut-ready draft content without writing it

        CapCut for macOS stacks visual layers by the position of the track in
        the ``tracks`` list, not by ``render_index``.

        Args:
            new_video_tracks: ``"above_main"`` (default) places new video tracks
                right above the main track, so captions, stickers and existing
                overlays stay on top of the b-roll; ``"top"`` puts them above everything.
        """
        data = json.loads(self.script.dumps())
        orig = self._original

        data["canvas_config"] = {**orig["canvas_config"],
                                 "width": data["canvas_config"]["width"],
                                 "height": data["canvas_config"]["height"]}

        orig_render_index = {s["id"]: s.get("render_index", 0) for t in orig["tracks"] for s in t["segments"]}
        orig_track_ids = {t["id"] for t in orig["tracks"]}
        orig_material_ids = {m["id"] for items in orig["materials"].values() if isinstance(items, list)
                             for m in items if isinstance(m, dict) and "id" in m}

        tracks = data["tracks"]
        if new_video_tracks == "above_main":
            new_video = [t for t in tracks if t["type"] == "video" and t["id"] not in orig_track_ids]
            new_video_ids = {t["id"] for t in new_video}
            rest = [t for t in tracks if t["id"] not in new_video_ids]
            # the main track is the first video track that is not an overlay (flag 2);
            # if the draft has none (it was rebuilt with cuts), the new tracks are the base layer
            main_idx = next((i for i, t in enumerate(rest) if t["type"] == "video" and t.get("flag") != 2), None)
            at = 0 if main_idx is None else main_idx + 1
            tracks = rest[:at] + new_video + rest[at:]
            data["tracks"] = tracks
        elif new_video_tracks != "top":
            raise ValueError(f"unknown new_video_tracks mode: {new_video_tracks}")

        new_ids: Set[str] = set()
        text_ri = [s.get("render_index", 0) for t in orig["tracks"] if t["type"] == "text" for s in t["segments"]]
        next_text_ri = max(text_ri + [_TEXT_RENDER_BASE - 1]) + 1
        overlay_track = next((t for t in orig["tracks"] if t["type"] == "video" and t.get("flag") == 2), None)
        video_layer = -1  # 0 for the main track, then 1, 2... bottom to top like CapCut

        for track_idx, track in enumerate(tracks):
            is_new_track = track["id"] not in orig_track_ids
            if is_new_track:
                new_ids.add(track["id"])
                if track["type"] == "video" and overlay_track is not None:
                    _fill(track, {k: v for k, v in overlay_track.items() if k not in ("id", "segments", "name")})
                    # bottom-most video track is the main one, the rest are overlays (flag 2)
                    is_base = track_idx == next(i for i, t in enumerate(tracks) if t["type"] == "video")
                    track["flag"] = 0 if is_base else overlay_track.get("flag", 2)
            if track["type"] == "video":
                video_layer += 1
                track_ri = video_layer
            elif track["type"] == "text" and is_new_track:
                track_ri, next_text_ri = next_text_ri, next_text_ri + 1
            else:
                track_ri = 0

            for seg in track["segments"]:
                seg["track_render_index"] = track_idx
                if seg["id"] in orig_render_index:
                    seg["render_index"] = track_ri if track["type"] == "video" else orig_render_index[seg["id"]]
                    continue
                new_ids.add(seg["id"])
                seg["render_index"] = track_ri
                if track["type"] == "video":
                    self._complete_video_segment(seg, data["materials"], new_ids)

        base_idx = next((i for i, t in enumerate(tracks) if t["type"] == "video" and t.get("flag") != 2), -1)
        above_video_idx = base_idx + 1
        # skip the b-roll stacked straight on the main track, but stay below the overlays
        while above_video_idx < len(tracks) and tracks[above_video_idx]["type"] == "video" \
                and (tracks[above_video_idx]["id"] not in orig_track_ids or tracks[above_video_idx].get("flag") != 2):
            above_video_idx += 1
        self._insert_pending_tracks(data, above_video_idx)
        tracks = data["tracks"]

        segments_by_id = {seg["id"]: seg for t in tracks for seg in t["segments"]}
        for seg_id, asset, duration_us in self._pending_assets:
            seg = segments_by_id.get(seg_id) or segments_by_id.get(seg_id.replace("-", "").lower())
            if seg is None:
                raise KeyError(f"segment {seg_id} not found in draft")
            _attach_asset(data, seg, asset, duration_us)

        for items in data["materials"].values():
            if not isinstance(items, list):
                continue
            for m in items:
                if isinstance(m, dict) and m.get("id") and m["id"] not in orig_material_ids:
                    new_ids.add(m["id"])

        for m in data["materials"].get("videos", []):
            if m["id"] in new_ids and m.get("type") in self._defaults["materials"]:
                _fill(m, self._defaults["materials"][m["type"]])

        self._warn_catalogue_resources(data, new_ids)

        text = json.dumps(data, ensure_ascii=False)
        for old in new_ids:
            if _HEX32.match(old):
                text = text.replace(f'"{old}"', f'"{str(uuid.UUID(old)).upper()}"')
        return json.loads(text)

    def _complete_video_segment(self, seg: Dict[str, Any], materials: Dict[str, List[Any]], new_ids: Set[str]) -> None:
        _fill(seg, self._defaults["video_segment"])
        for kf_list in seg.get("common_keyframes", []):
            new_ids.add(kf_list["id"])
            for kf in kf_list["keyframe_list"]:
                new_ids.add(kf["id"])
                _fill(kf, self._defaults["keyframe"])

        linked_types = {mtype for mtype, items in materials.items() if isinstance(items, list)
                        for m in items if isinstance(m, dict) and m.get("id") in seg["extra_material_refs"]}
        for mtype, template in self._defaults["aux"].items():
            if mtype in linked_types:
                continue
            aux = deepcopy(template)
            aux["id"] = _new_uuid()
            materials.setdefault(mtype, []).append(aux)
            seg["extra_material_refs"].append(aux["id"])

    @staticmethod
    def _warn_catalogue_resources(data: Dict[str, Any], new_ids: Set[str]) -> None:
        # materials copied from CapCut projects (asset index) carry a CapCut cache path; library ones do not
        kinds = ["material_animations", "transitions", "video_effects", "effects", "filters"]
        used = [k for k in kinds for m in data["materials"].get(k, []) if m.get("id") in new_ids
                and not all(a.get("path") for a in m.get("animations", [m]))]
        if used:
            print(f"[capcut_mac] warning: new {sorted(set(used))} use Jianying resource ids; "
                  "CapCut international may not find them. Keyframes are safe.")

    def save(self, backup: bool = True, new_video_tracks: str = "above_main") -> str:
        """Write the draft back into the CapCut project. Returns the backup suffix (if any)"""
        _ensure_closed()
        data = self.export_json(new_video_tracks)
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

        targets = [self.info_path]
        timeline_copy = os.path.join(self.draft_dir, "Timelines", data["id"], "draft_info.json")
        if os.path.isfile(timeline_copy):
            targets.append(timeline_copy)

        suffix = time.strftime(".pyjy-%Y%m%d-%H%M%S.bak")
        for path in targets:
            if backup:
                shutil.copy2(path, path + suffix)
            for p in (path, path + ".bak"):
                if p == path or os.path.isfile(p):
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(text)

        meta_path = os.path.join(self.draft_dir, "draft_meta_info.json")
        if os.path.isfile(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            meta["tm_duration"] = data["duration"]
            meta["tm_draft_modified"] = int(time.time() * 1e6)
            self._register_media(meta)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))

        self._original = data
        self._pending_assets = []
        self._pending_tracks = []
        self._new_media = []
        return suffix if backup else ""

    def _register_media(self, meta: Dict[str, Any]) -> None:
        """List new media files in draft_meta_info.json, the way CapCut does when importing"""
        bucket = next((b for b in meta.get("draft_materials", []) if b.get("type") == 0), None)
        if bucket is None:
            return
        known = {v.get("file_Path") for v in bucket.setdefault("value", [])}
        now = int(time.time())
        for metetype, path in self._new_media:
            if path in known:
                continue
            known.add(path)
            duration = _media_duration_us(path) or 0
            bucket["value"].append({
                "ai_group_type": "", "create_time": now, "duration": duration, "enter_from": 0,
                "extra_info": os.path.basename(path), "file_Path": path, "height": 0,
                "id": str(uuid.uuid4()).lower(), "import_time": now, "import_time_ms": now * 1000000,
                "item_source": 1, "material_color_tag": "", "md5": "", "metetype": metetype,
                "roughcut_time_range": {"duration": duration, "start": 0},
                "sub_time_range": {"duration": -1, "start": -1}, "type": 0, "width": 0})


def list_drafts(root: str = DEFAULT_DRAFTS_ROOT) -> List[str]:
    """Project folder names, most recently modified first"""
    names = [n for n in os.listdir(root) if os.path.isfile(os.path.join(root, n, "draft_info.json"))]
    return sorted(names, key=lambda n: os.path.getmtime(os.path.join(root, n, "draft_info.json")), reverse=True)


def duplicate_draft(src_name: str, new_name: str, root: str = DEFAULT_DRAFTS_ROOT) -> CapCutMacDraft:
    """Copy a project to a new one CapCut lists as a separate draft, and open it"""
    _ensure_closed()
    src, dst = os.path.join(root, src_name), os.path.join(root, new_name)
    if os.path.exists(dst):
        raise FileExistsError(dst)
    shutil.copytree(src, dst)

    new_id = _new_uuid()
    now = int(time.time() * 1e6)

    def retarget(obj: Any, old_id: str) -> Any:
        if isinstance(obj, dict):
            return {k: retarget(v, old_id) for k, v in obj.items()}
        if isinstance(obj, list):
            return [retarget(v, old_id) for v in obj]
        if isinstance(obj, str):
            if obj == old_id:
                return new_id
            if obj == src or obj.startswith(src + os.sep):
                return dst + obj[len(src):]
        return obj

    meta_path = os.path.join(dst, "draft_meta_info.json")
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    old_id = meta["draft_id"]
    meta = retarget(meta, old_id)
    meta.update(draft_name=new_name, tm_draft_create=now, tm_draft_modified=now)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))

    root_meta_path = os.path.join(root, "root_meta_info.json")
    if os.path.isfile(root_meta_path):
        with open(root_meta_path, encoding="utf-8") as f:
            root_meta = json.load(f)
        shutil.copy2(root_meta_path, root_meta_path + time.strftime(".pyjy-%Y%m%d-%H%M%S.bak"))
        entry = next((e for e in root_meta["all_draft_store"] if e.get("draft_fold_path") == src), None)
        if entry is not None:
            entry = retarget(entry, old_id)
            entry.update(draft_name=new_name, tm_draft_create=now, tm_draft_modified=now)
            root_meta["all_draft_store"].insert(0, entry)
            root_meta["draft_ids"] = root_meta.get("draft_ids", 0) + 1
            with open(root_meta_path, "w", encoding="utf-8") as f:
                json.dump(root_meta, f, ensure_ascii=False, separators=(",", ":"))

    return CapCutMacDraft(dst)
