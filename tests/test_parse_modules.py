"""``petritype.core.parse_modules``: the import statements of a parsed module,
one per imported name, each valid Python."""

import ast

from petritype.core.parse_modules import ExtractImportStatements, ParsedModule


class TestExtractImportStatements:

    def test_extract_import_statements_preserves_aliases_and_relative_imports(self):
        code = (
            "import os\n"
            "import numpy as np\n"
            "import a.b as ab, sys\n"
            "from pathlib import Path\n"
            "from typing import Any as Anything, Sequence\n"
            "from . import sibling\n"
            "from . import helper as h\n"
            "from .. import parent_mod\n"
            "from .sub import thing\n"
            "from ..pkg.mod import Widget as W, Gadget\n"
            "from . import *\n"
        )
        parsed = ParsedModule(
            path_to_file="example.py",
            import_path_components=("pkg", "example"),
            code=code,
        )
        extracted = ExtractImportStatements.from_parsed_module(parsed)
        assert extracted == [
            "import os",
            "import numpy as np",
            "import a.b as ab",
            "import sys",
            "from pathlib import Path",
            "from typing import Any as Anything",
            "from typing import Sequence",
            "from . import sibling",
            "from . import helper as h",
            "from .. import parent_mod",
            "from .sub import thing",
            "from ..pkg.mod import Widget as W",
            "from ..pkg.mod import Gadget",
            "from . import *",
        ]
        # Every emitted statement must be valid Python syntax.
        for stmt in extracted:
            assert "from None " not in stmt
            ast.parse(stmt)
