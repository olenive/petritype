"""Render the example notebooks into a single self-contained HTML gallery.

This is the "look at every net without clicking" companion to ``tests/test_notebooks.py``.
The live-firing notebooks only advance on button clicks, so a normal ``marimo export`` shows
just the initial frame. Here we instead load each notebook, pull its own ``session`` /
``fire_one`` out of ``app.run()`` (exactly as the execution-tier test does), drive the real
graph from its initial state to its terminal state, and assemble **every** recorded frame into a
looping animated GIF — one per net — embedded in a single HTML page. Open the page and watch
every net run start to finish.

Usage (needs the ``examples`` extra, NOT the ``dev`` extra — this is not a pytest run)::

    uv run --extra examples python tests/render_notebook_gallery.py
    uv run --extra examples python tests/render_notebook_gallery.py --open
    uv run --extra examples python tests/render_notebook_gallery.py -o /tmp/nets.html

A notebook is driven to completion automatically if its cells define ``session`` and
``fire_one`` (the live-firing template). Perpetual / user-driven notebooks (parcel, rooms) have
no terminal state to enumerate, so they are listed but not auto-played.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import html
import importlib.util
import io
import sys
import webbrowser
from pathlib import Path

from PIL import Image

# Frame timing for the per-net GIFs (milliseconds).
_FRAME_MS = 800
_HOLD_LAST_MS = 2200  # linger on the terminal state before the loop restarts

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"

# Pre-existing experiments that are not part of the Jupyter -> marimo migration.
_EXCLUDED_NAMES = {
    "interactive_traffic_monitor.py",
    "interactive_traffic_monitor_notebook.py",
}

_MAX_STEPS = 1000


def _marimo_notebooks() -> list[Path]:
    """Every marimo notebook under examples/ (auto-discovers ones added later)."""
    notebooks = []
    for path in sorted(EXAMPLES_DIR.rglob("*.py")):
        if path.name in _EXCLUDED_NAMES:
            continue
        if "marimo.App(" in path.read_text(encoding="utf-8"):
            notebooks.append(path)
    return notebooks


def _load_notebook(path: Path):
    """Import a notebook by file path (names start with digits, so not importable by name).

    The notebook's own directory is placed on ``sys.path`` first, so bare sibling imports (e.g.
    fine_tune's ``from hypothetical_training_steps import ...``) resolve the way they do when
    marimo runs the notebook from its own directory.
    """
    parent = str(path.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec = importlib.util.spec_from_file_location(f"_nb_{abs(hash(str(path)))}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _drive_to_completion(session, fire_one) -> list[tuple[str, bytes, int]]:
    """Fire the notebook's graph from its initial state to its terminal state.

    Returns the full history of ``(caption, png_bytes, width)`` frames, including the initial
    one that the notebook records before any transition fires.
    """
    for _ in range(_MAX_STEPS):
        if not asyncio.run(fire_one(session)):
            break
    return list(session["history"])


def _frames_to_gif(history: list[tuple[str, bytes, int]]) -> tuple[bytes, int]:
    """Assemble a notebook's recorded frames into one looping animated GIF.

    The net's topology is fixed but node labels (token contents) can change between steps, so
    frame images may differ in size; each is pasted onto a common white canvas to keep the GIF
    valid. Returns ``(gif_bytes, display_width_px)``.
    """
    images = [Image.open(io.BytesIO(png_bytes)).convert("RGBA") for _c, png_bytes, _w in history]
    canvas_w = max(im.width for im in images)
    canvas_h = max(im.height for im in images)

    frames = []
    for im in images:
        canvas = Image.new("RGBA", (canvas_w, canvas_h), (255, 255, 255, 255))
        canvas.paste(im, (0, 0), im)
        frames.append(canvas.convert("RGB"))

    durations = [_FRAME_MS] * len(frames)
    durations[-1] = _HOLD_LAST_MS

    buffer = io.BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
        optimize=True,
    )
    return buffer.getvalue(), max(width for _c, _p, width in history)


def _gif_html(caption: str, gif_bytes: bytes, width: int) -> str:
    b64 = base64.b64encode(gif_bytes).decode("ascii")
    return (
        '<figure class="frame">'
        f'<img src="data:image/gif;base64,{b64}" width="{width}" alt="{html.escape(caption)}">'
        f"<figcaption>{html.escape(caption)}</figcaption>"
        "</figure>"
    )


def _section_html(title: str, body: str) -> str:
    return f'<section class="notebook"><h2>{html.escape(title)}</h2>{body}</section>'


def build_gallery() -> str:
    """Run every notebook and return the gallery as one self-contained HTML string."""
    fired_sections: list[str] = []
    skipped: list[str] = []

    for notebook in _marimo_notebooks():
        rel = str(notebook.relative_to(EXAMPLES_DIR))
        print(f"  loading {rel} ...", flush=True)
        module = _load_notebook(notebook)
        _outputs, defs = module.app.run()

        if "session" in defs and "fire_one" in defs:
            history = _drive_to_completion(defs["session"], defs["fire_one"])
            steps = len(history) - 1
            gif_bytes, width = _frames_to_gif(history)
            caption = f"{steps} step{'s' if steps != 1 else ''} — animation loops continuously"
            heading = f"{rel} — {steps} step{'s' if steps != 1 else ''}"
            fired_sections.append(_section_html(heading, _gif_html(caption, gif_bytes, width)))
            print(f"    -> {steps} steps animated", flush=True)
        else:
            skipped.append(rel)
            print("    -> not auto-played (static / perpetual / user-driven)", flush=True)

    skipped_html = ""
    if skipped:
        items = "".join(f"<li>{html.escape(name)}</li>" for name in skipped)
        skipped_html = (
            '<section class="notebook skipped"><h2>Not auto-played</h2>'
            "<p>These don't expose a finite <code>fire_one</code> loop to drive — they are "
            "static, perpetual, or user-driven. Open them with "
            "<code>uv run --extra examples marimo edit &lt;notebook&gt;</code>.</p>"
            f"<ul>{items}</ul></section>"
        )

    return _PAGE_TEMPLATE.format(body="".join(fired_sections) + skipped_html)


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Petritype notebook gallery</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 1100px; color: #1a1a1a; }}
  h1 {{ border-bottom: 2px solid #ddd; padding-bottom: .4rem; }}
  section.notebook {{ margin: 2.5rem 0; }}
  section.notebook h2 {{ background: #f4f4f6; padding: .5rem .8rem; border-left: 4px solid #807dbd; }}
  .frame {{ margin: 1.2rem 0; padding: 1rem; border: 1px solid #e6e6e6; border-radius: 8px; }}
  .frame img {{ display: block; max-width: 100%; height: auto; }}
  figcaption {{ margin-top: .6rem; font-weight: 600; color: #444; }}
  .skipped ul {{ line-height: 1.8; }}
  code {{ background: #f0f0f3; padding: .1rem .3rem; border-radius: 4px; }}
</style>
</head>
<body>
<h1>Petritype notebook gallery</h1>
<p>Every live-firing example net, driven from its initial state to its terminal state and
replayed as a looping animation. No clicking required.</p>
{body}
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=REPO_ROOT / "notebook_gallery.html",
        help="where to write the gallery HTML (default: ./notebook_gallery.html)",
    )
    parser.add_argument(
        "--open", action="store_true", help="open the gallery in a browser when done"
    )
    args = parser.parse_args()

    print("Building notebook gallery...")
    page = build_gallery()
    args.output.write_text(page, encoding="utf-8")
    print(f"\nWrote gallery to {args.output}")
    if args.open:
        webbrowser.open(args.output.resolve().as_uri())


if __name__ == "__main__":
    main()
