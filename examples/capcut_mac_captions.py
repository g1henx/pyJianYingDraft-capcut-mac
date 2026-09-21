"""Write subtitles into a CapCut project as plain text boxes, with a font pairing.

    python3 examples/capcut_mac_captions.py "<project>" [--name "<copy>"] [--in-place]

Uses CapCut's plain Text tool (not caption templates, which CapCut does not draw
when a script writes them). The pairing is Inter + Times New Roman MT: Inter Black
for the body of the line, Times New Roman Italic for the word being stressed.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pyJianYingDraft as draft  # noqa: E402

TIMES_ITALIC = "/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf"

# (start, duration, text, palabras a resaltar)
CAPTIONS = [
    (0.000, 0.633, "Nadie te lo dice", ["Nadie"]),
    (0.633, 0.700, "pero el 90% falla", ["90%"]),
    (1.333, 0.767, "por un detalle", ["detalle"]),
    (2.100, 0.767, "que dura 3 segundos", ["3 segundos"]),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--name", help='name of the copy (default "<project> subs")')
    ap.add_argument("--in-place", action="store_true")
    ap.add_argument("--keep-placeholder", action="store_true",
                    help='keep the "Default text" box the project came with')
    ap.add_argument("--root", default=draft.DEFAULT_DRAFTS_ROOT)
    args = ap.parse_args()

    assets = draft.AssetIndex()
    inter = assets.get("Inter Black", kind="font")

    project = (draft.CapCutMacDraft.open(args.project, root=args.root) if args.in_place else
               draft.duplicate_draft(args.project, args.name or f"{args.project} subs", root=args.root))

    if not args.keep_placeholder:
        placeholder = next((s for s in project.segments("text")
                            if (s.get("text") or "").strip() == "Default text"), None)
        if placeholder is not None:
            track_id = project._original["tracks"][placeholder["track_index"]]["id"]
            project.remove_track(track_id)
            print('(quitada la caja de texto "Default text" que traia el proyecto)')

    for start, duration, text, highlight in CAPTIONS:
        project.add_text(text, start, duration, y=-0.56, size=15.0,
                         font=inter, highlight=highlight, highlight_font=TIMES_ITALIC,
                         highlight_size=17.0, highlight_color=(1.0, 0.82, 0.16))
        print(f"{start:6.3f}+{duration:.3f}  {text}   (resaltado: {', '.join(highlight)})")

    project.save()
    print(f"\n{len(CAPTIONS)} subtítulos escritos en: {project.draft_dir}")


if __name__ == "__main__":
    main()
