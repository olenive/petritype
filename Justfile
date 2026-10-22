# Everything to run before a release is `just check`. Each recipe also runs on its own.

# List the recipes
default:
    @{{just_executable()}} --list

# Everything to run before a release, in order: test, notebooks, gallery, build
check: test notebooks gallery build

# The unit suite
test:
    uv run --extra dev --extra examples pytest -q

# The notebook suite, the marimo example notebooks (slow)
notebooks:
    uv run --extra dev --extra examples pytest -m notebooks -q

# The notebook gallery, opened in the browser: look at every net
gallery:
    MPLBACKEND=Agg uv run --extra examples python tests/render_notebook_gallery.py --open

# A build of the wheel and sdist into dist/
build:
    uv build
