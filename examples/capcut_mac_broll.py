"""Add b-roll clips to an existing CapCut (macOS) project.

    python examples/capcut_mac_broll.py "<project name>" broll.json [--copy "<new name>"]

broll.json is a list of clips:

    [
      {"file": "/path/clip.mp4", "at": "3s", "duration": "3s"},
      {"file": "/path/clip2.mp4", "at": "9s", "duration": "2s", "from": "2s",
       "scale": 0.5, "y": -0.35, "zoom_to": 1.15}
    ]

at/duration/from use the library's time strings; scale/x/y follow ClipSettings
(x/y in half-canvas units); zoom_to adds a slow push-in keyframe from 1.0.
CapCut must be closed. The b-roll lands on a new track right above the main
track, below captions and existing overlays.
"""

import argparse
import json

import pyJianYingDraft as draft


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project")
    parser.add_argument("plan")
    parser.add_argument("--copy", help="work on a duplicate with this name instead of the original")
    parser.add_argument("--track", default="broll")
    args = parser.parse_args()

    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)

    project = draft.duplicate_draft(args.project, args.copy) if args.copy else draft.CapCutMacDraft.open(args.project)
    script = project.script
    script.append_track(draft.TrackSpec(draft.TrackType.video, args.track))

    for clip in plan:
        path = project.import_media(clip["file"])
        seg = draft.VideoSegment(
            path,
            draft.trange(clip["at"], clip["duration"]),
            source_timerange=draft.trange(clip["from"], clip["duration"]) if "from" in clip else None,
            clip_settings=draft.ClipSettings(scale_x=clip.get("scale", 1.0), scale_y=clip.get("scale", 1.0),
                                             transform_x=clip.get("x", 0.0), transform_y=clip.get("y", 0.0)),
        )
        if "zoom_to" in clip:
            seg.add_keyframe(draft.KeyframeProperty.uniform_scale, "0s", clip.get("scale", 1.0))
            seg.add_keyframe(draft.KeyframeProperty.uniform_scale, clip["duration"], clip["zoom_to"])
        script.add_segment(seg, args.track)

    project.save()
    print(f"added {len(plan)} b-roll clip(s) to {project.draft_dir}")


if __name__ == "__main__":
    main()
