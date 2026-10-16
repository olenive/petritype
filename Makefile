# Everything to run before a release, in order: the unit suite, the notebook
# suite, the notebook gallery (opens in the browser: look at every net), and a
# build of the wheel and sdist into dist/. Each target also runs on its own.
.PHONY: check test notebooks gallery build

check: test notebooks gallery build

test:
	uv run --extra dev --extra examples pytest -q

notebooks:
	uv run --extra dev --extra examples pytest -m notebooks -q

gallery:
	MPLBACKEND=Agg uv run --extra examples python tests/render_notebook_gallery.py --open

build:
	uv build
