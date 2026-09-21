"""Look up CapCut assets by name in the index built by tools/build_capcut_asset_index.py
and attach them to segments of a `CapCutMacDraft`.

    from pyJianYingDraft import CapCutMacDraft, AssetIndex

    assets = AssetIndex()                                   # capcut_assets.json
    project = CapCutMacDraft.open("0920 (2) broll-test")
    project.attach_asset(assets.get("Unfold", kind="animation_in"), segment_id)
    project.attach_asset(assets.get("Pull in"), segment_id)  # transition to the next clip
    project.save()

Supported kinds: transition, animation_in/out/loop/caption, text_effect,
text_glow, text_bubble, mask, voice_filter, adjustment, color_tool, face_body.
Caption templates, video effects, filters and stickers need their own track
segments and are not attached by this helper (see the index notes).
"""

import json
import os
import re
import uuid
from copy import deepcopy
from typing import Any, Dict, List, Optional

DEFAULT_INDEX = os.environ.get(
    "CAPCUT_ASSET_INDEX", os.path.expanduser("~/Documents/Fix-Videos/capcut_asset_index/capcut_assets.json"))

_UUID_RE = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}", re.I)

# kind -> materials list the copy goes into (extra_material_refs kinds)
REF_LISTS = {
    "transition": "transitions",
    "text_effect": "effects",
    "text_glow": "effects",
    "text_bubble": "effects",
    "adjustment": "effects",
    "face_body": "effects",
    "voice_filter": "audio_effects",
}
ANIMATION_KINDS = {"animation_in", "animation_out", "animation_loop", "animation_caption"}
# kinds where a segment holds at most one material of the same `type`
REPLACE_SAME_TYPE = {"transition", "text_effect", "text_glow", "text_bubble", "mask", "adjustment", "voice_filter"}


class AssetNotFound(LookupError):
    pass


class AssetIndex:
    """The asset catalogue produced by tools/build_capcut_asset_index.py"""

    def __init__(self, path: str = DEFAULT_INDEX):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.meta: Dict[str, Any] = data["meta"]
        self.assets: List[Dict[str, Any]] = data["assets"]

    def find(self, query: str, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """Assets matching `query` (exact name / Spanish gloss / resource_id first, then substring)"""
        q = query.strip().lower()
        pool = [a for a in self.assets if kind is None or a["kind"] == kind]

        def fields(a: Dict[str, Any]) -> List[str]:
            return [str(a.get(k) or "").lower() for k in ("name", "name_es", "display_name", "resource_id")]

        exact = [a for a in pool if q in fields(a)]
        if exact:
            return sorted(exact, key=lambda a: -a["uses"])
        return sorted((a for a in pool if any(q in f for f in fields(a))), key=lambda a: -a["uses"])

    def get(self, query: str, kind: Optional[str] = None) -> Dict[str, Any]:
        """The single best match; raises if none, or if several kinds match and `kind` was not given"""
        hits = self.find(query, kind)
        if not hits:
            raise AssetNotFound(f"no CapCut asset matches {query!r}" + (f" (kind {kind})" if kind else ""))
        kinds = {h["kind"] for h in hits}
        if kind is None and len(kinds) > 1:
            raise AssetNotFound(f"{query!r} matches several kinds {sorted(kinds)}; pass kind=")
        return hits[0]


def instantiate(asset: Dict[str, Any]) -> Dict[str, Any]:
    """Deep copy of the asset's material with every UUID replaced by a fresh one"""
    text = json.dumps(asset["template"], ensure_ascii=False)
    mapping: Dict[str, str] = {}

    def fresh(m: "re.Match[str]") -> str:
        return mapping.setdefault(m.group(0).upper(), str(uuid.uuid4()).upper())

    return json.loads(_UUID_RE.sub(fresh, text))


def _materials_of(data: Dict[str, Any]) -> Dict[str, Any]:
    by_id = {}
    for mlist, items in data["materials"].items():
        if isinstance(items, list):
            for m in items:
                if isinstance(m, dict) and "id" in m:
                    by_id[m["id"]] = (mlist, m)
    return by_id


def attach(data: Dict[str, Any], segment: Dict[str, Any], asset: Dict[str, Any],
           duration_us: Optional[int] = None) -> str:
    """Attach `asset` to `segment` inside CapCut draft content `data`. Returns the new material id"""
    kind = asset["kind"]
    materials = data["materials"]
    by_id = _materials_of(data)
    refs: List[str] = segment.setdefault("extra_material_refs", [])
    seg_duration = segment["target_timerange"]["duration"]

    if kind in ANIMATION_KINDS:
        anim = deepcopy(asset["template"])
        anim_type = anim["type"]
        if duration_us is not None:
            anim["duration"] = duration_us
        if anim_type in ("caption", "loop"):
            anim["start"], anim["duration"] = 0, seg_duration
        elif anim_type == "out":
            anim["start"] = max(0, seg_duration - anim["duration"])
        else:
            anim["start"] = 0
        container = next((by_id[r][1] for r in refs if r in by_id and by_id[r][0] == "material_animations"), None)
        if container is None:
            container = {"id": str(uuid.uuid4()).upper(), "type": "sticker_animation",
                         "multi_language_current": "none", "animations": []}
            materials.setdefault("material_animations", []).append(container)
            refs.append(container["id"])
        container["animations"] = [a for a in container.get("animations", []) if a.get("type") != anim_type] + [anim]
        return container["id"]

    if kind == "mask":
        mlist = "common_mask" if asset["template"].get("constant_material_id") else "masks"
    elif kind == "color_tool":
        mlist = {"hsl": "hsl", "color_curves": "color_curves", "primary_color_wheels": "primary_color_wheels",
                 "log_color_wheels": "log_color_wheels", "smart_relight": "smart_relights"}.get(
            asset["template"].get("type"), "effects")
    elif kind in REF_LISTS:
        mlist = REF_LISTS[kind]
    else:
        raise ValueError(f"attach() does not handle kind {kind!r}; see the index notes for how to add it")

    material = instantiate(asset)
    if kind == "transition" and duration_us is not None:
        material["duration"] = duration_us
    if kind in REPLACE_SAME_TYPE:
        refs[:] = [r for r in refs if not (r in by_id and by_id[r][0] == mlist
                                           and by_id[r][1].get("type") == material.get("type"))]
    materials.setdefault(mlist, []).append(material)
    refs.append(material["id"])
    return material["id"]


# ---------------------------------------------------------------- track segments
# Captions (caption templates), video effects, filters and stickers live on their
# own tracks. These builders return (materials to add as [(list, material)], segment).

TRACK_KINDS = {"text_template": "caption", "video_effect": "video_effect", "filter": "filter", "sticker": "sticker"}
TRACK_TYPES = {"caption": "text", "video_effect": "effect", "filter": "filter", "sticker": "sticker"}
MATERIAL_LISTS = {"video_effect": "video_effects", "filter": "effects", "sticker": "stickers"}


def _remap_uuids(obj: Any) -> Any:
    """Deep copy of `obj` with every UUID consistently replaced by a fresh one"""
    text = json.dumps(obj, ensure_ascii=False)
    mapping: Dict[str, str] = {}
    return json.loads(_UUID_RE.sub(lambda m: mapping.setdefault(m.group(0).upper(), str(uuid.uuid4()).upper()), text))


def split_words(text: str, duration_us: int) -> List[Dict[str, Any]]:
    """Evenly timed words (weighted by length) covering `duration_us`, as [{text, start, end}] in µs"""
    words = text.split()
    total = sum(len(w) for w in words) or 1
    out, t = [], 0
    for w in words:
        d = duration_us * len(w) / total
        out.append({"text": w, "start": int(t), "end": int(t + d)})
        t += d
    if out:
        out[-1]["end"] = duration_us
    return out


def _words_json(words: List[Dict[str, Any]]) -> Dict[str, List[Any]]:
    """CapCut `words` block: ms offsets, with a zero-length space token between words"""
    start, end, text = [], [], []
    for i, w in enumerate(words):
        s, e = int(w["start"] / 1000), int(w["end"] / 1000)
        start.append(s), end.append(e), text.append(w["text"])
        if i < len(words) - 1:
            start.append(e), end.append(e), text.append(" ")
    return {"start_time": start, "end_time": end, "text": text}


def _set_text(text_material: Dict[str, Any], words: List[Dict[str, Any]]) -> None:
    text = " ".join(w["text"] for w in words)
    content = json.loads(text_material["content"])
    length = len(text.encode("utf-16-le")) // 2
    styles = content.get("styles") or [{}]
    first = deepcopy(styles[0])
    first["range"] = [0, length]
    content["styles"] = [first]
    content["text"] = text
    text_material["content"] = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    text_material["words"] = _words_json(words)
    text_material["current_words"] = {"start_time": [], "end_time": [], "text": []}
    text_material["recognize_text"] = text
    text_material["recognize_task_id"] = ""


def _fit_animations(container: Dict[str, Any], duration_us: int) -> None:
    for a in container.get("animations", []):
        if a.get("type") in ("caption", "loop"):
            a["start"], a["duration"] = 0, duration_us
        elif a.get("type") == "out":
            a["duration"] = min(a.get("duration", 0), duration_us)
            a["start"] = duration_us - a["duration"]
        elif a.get("type") == "in":
            a["start"], a["duration"] = 0, min(a.get("duration", 0), duration_us)


def build_caption(asset: Dict[str, Any], segment_template: Dict[str, Any], text: str, start_us: int,
                  duration_us: int, words: Optional[List[Dict[str, Any]]] = None,
                  y: Optional[float] = None, scale: Optional[float] = None):
    """Caption segment from a caption/text template asset.

    `words`: [{text, start, end}] in µs relative to the caption start (e.g. from
    speech recognition); evenly timed from `text` when omitted. Multi-line
    templates get the words split across their lines in order.
    """
    if asset["kind"] != "text_template":
        raise ValueError(f"{asset['display_name']} is a {asset['kind']}, not a caption template")
    bundle = _remap_uuids({"template": asset["template"], "deps": asset.get("dependencies", [])})
    tpl, deps = bundle["template"], bundle["deps"]
    by_id = {d["material"]["id"]: d["material"] for d in deps}
    words = words or split_words(text, duration_us)

    tirs = [r for r in tpl.get("text_info_resources", []) if r.get("text_material_id") in by_id]
    if not tirs:
        raise ValueError(f"{asset['display_name']}: template has no text material in the index")
    per_line = max(1, -(-len(words) // len(tirs)))
    refs: List[str] = []
    for i, tir in enumerate(tirs):
        chunk = words[i * per_line:(i + 1) * per_line] or words[-1:]
        _set_text(by_id[tir["text_material_id"]], chunk)
        attach = tir.setdefault("attach_info", {})
        attach["start_time"], attach["duration"] = 0, duration_us
        refs += [r for r in tir.get("extra_material_refs", []) if r not in refs]
    tpl["text_info_resources"] = tirs
    for m in by_id.values():
        if m.get("type") == "sticker_animation":
            _fit_animations(m, duration_us)

    seg = deepcopy(segment_template)
    seg["id"] = str(uuid.uuid4()).upper()
    seg["material_id"] = tpl["id"]
    seg["target_timerange"] = {"start": start_us, "duration": duration_us}
    seg["extra_material_refs"] = refs
    if y is not None:
        seg["clip"]["transform"]["y"] = y
    if scale is not None:
        seg["clip"]["scale"] = {"x": scale, "y": scale}
    materials = [("text_templates", tpl)] + [(d["list"], d["material"]) for d in deps]
    return materials, seg


def build_track_segment(asset: Dict[str, Any], segment_template: Dict[str, Any], start_us: int, duration_us: int,
                        *, x: float = 0.0, y: float = 0.0, scale: Optional[float] = None,
                        intensity: Optional[float] = None):
    """Segment + material for a video effect, filter or sticker asset"""
    kind = TRACK_KINDS.get(asset["kind"])
    if kind not in MATERIAL_LISTS:
        raise ValueError(f"{asset['display_name']} ({asset['kind']}) is not a video effect, filter or sticker")
    material = instantiate(asset)
    if intensity is not None and "value" in material:
        material["value"] = intensity
    seg = deepcopy(segment_template)
    seg["id"] = str(uuid.uuid4()).upper()
    seg["material_id"] = material["id"]
    seg["target_timerange"] = {"start": start_us, "duration": duration_us}
    if kind == "sticker" and isinstance(seg.get("clip"), dict):
        seg["clip"]["transform"] = {"x": x, "y": y}
        if scale is not None:
            seg["clip"]["scale"] = {"x": scale, "y": scale}
    return [(MATERIAL_LISTS[kind], material)], seg
