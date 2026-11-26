import ast
import inspect
import typing

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from petritype.core.ast_extraction import (
    ASTAnnAssign,
    ASTClass,
    ASTFunction,
    ASTModule,
    ASTType,
)
from petritype.core.data_structures import (
    COMMON_TYPES,
    FunctionWithAnnotations,
    ImportPath,
    NodeIndex,
    NodeIndicesFromTo,
    TypeRelationship,
    TypeVariableWithAnnotations,
    ensure_index_is_non_negative,
)
from petritype.core.parse_modules import ExtractImportStatements, ParsedModule
from petritype.core.relationship_graph_components import (
    FunctionToTypeEdges,
    RelationshipEdges,
    TypeToFunctionEdges,
    TypeToTypeEdges,
)


def _dummy_import_path(
    class_name: str | None = None,
    function_name: str | None = None,
) -> ImportPath:
    return ImportPath(
        module_path_components=("pkg", "mod"),
        class_name=class_name,
        function_name=function_name,
    )


class TestDataStructuresFixes:

    def test_ensure_index_is_non_negative_returns_int(self):
        assert ensure_index_is_non_negative(0) == 0
        assert ensure_index_is_non_negative(7) == 7
        assert inspect.signature(ensure_index_is_non_negative).return_annotation is int
        with pytest.raises(ValueError, match="non-negative"):
            ensure_index_is_non_negative(-1)

    def test_node_index_validator_preserves_value_and_rejects_negative(self):
        adapter = TypeAdapter(NodeIndex)
        assert adapter.validate_python(0) == 0
        assert adapter.validate_python(42) == 42
        with pytest.raises(ValidationError):
            adapter.validate_python(-1)

        class Edge(BaseModel):
            source: NodeIndex
            target: NodeIndex

        edge = Edge(source=0, target=3)
        assert edge.source == 0
        assert edge.target == 3
        with pytest.raises(ValidationError):
            Edge(source=-1, target=3)

    def test_node_indices_from_to_validates_pair(self):
        adapter = TypeAdapter(NodeIndicesFromTo)
        assert adapter.validate_python((0, 5)) == (0, 5)
        with pytest.raises(ValidationError):
            adapter.validate_python((0, -2))

    def test_contained_in_attribute_type_enum_member_exists(self):
        assert TypeRelationship.CONTAINED_IN_ATTRIBUTE_TYPE.value == "in attribute type"


class TestRelationshipGraphComponentsFixes:

    def test_edge_type_aliases_are_valid_two_arg_dicts(self):
        for alias in (TypeToTypeEdges, TypeToFunctionEdges, FunctionToTypeEdges):
            resolved = alias.__value__
            assert typing.get_origin(resolved) is dict
            assert len(typing.get_args(resolved)) == 2

        type_to_type_adapter = TypeAdapter(TypeToTypeEdges)
        assert type_to_type_adapter.validate_python(
            {("A", "B"): TypeRelationship.PARENT_OF}
        ) == {("A", "B"): TypeRelationship.PARENT_OF}

        type_to_fn_adapter = TypeAdapter(TypeToFunctionEdges)
        assert type_to_fn_adapter.validate_python(
            {("A", "fn"): TypeRelationship.TAKES_AS_SUBTYPE_OF_ARGUMENT}
        ) == {("A", "fn"): TypeRelationship.TAKES_AS_SUBTYPE_OF_ARGUMENT}

        fn_to_type_adapter = TypeAdapter(FunctionToTypeEdges)
        assert fn_to_type_adapter.validate_python(
            {("fn", "A"): TypeRelationship.RETURNS_EXACTLY_THIS_TYPE}
        ) == {("fn", "A"): TypeRelationship.RETURNS_EXACTLY_THIS_TYPE}

    def test_type_to_function_distinguishes_exact_and_subtype_arguments(self):
        types = [
            TypeVariableWithAnnotations(
                name="Item",
                parent_type=None,
                subtypes=set(),
                attribute_types={},
                import_path=_dummy_import_path(class_name="Item"),
                code="class Item(BaseModel): pass",
            ),
            TypeVariableWithAnnotations(
                name="Tag",
                parent_type=None,
                subtypes=set(),
                attribute_types={},
                import_path=_dummy_import_path(class_name="Tag"),
                code="class Tag(BaseModel): pass",
            ),
        ]
        functions = [
            FunctionWithAnnotations(
                function_full_name="process_exact",
                class_name=None,
                function_name="process_exact",
                argument_types={"item": "Item"},
                return_type=None,
                import_path=_dummy_import_path(function_name="process_exact"),
                code="def process_exact(item: Item): pass",
            ),
            FunctionWithAnnotations(
                function_full_name="process_optional",
                class_name=None,
                function_name="process_optional",
                argument_types={"item": "Optional[Item]", "tags": "list[Tag]"},
                return_type=None,
                import_path=_dummy_import_path(function_name="process_optional"),
                code="def process_optional(item: Optional[Item], tags: list[Tag]): pass",
            ),
            FunctionWithAnnotations(
                function_full_name="process_union",
                class_name=None,
                function_name="process_union",
                argument_types={"item": "Item | None"},
                return_type=None,
                import_path=_dummy_import_path(function_name="process_union"),
                code="def process_union(item: Item | None): pass",
            ),
        ]
        result = RelationshipEdges.type_to_function(
            types=types, functions=functions, ignored_types=COMMON_TYPES
        )
        assert result == {
            ("Item", "process_exact"): TypeRelationship.TAKES_AS_EXACT_TYPE_OF_ARGUMENT,
            ("Item", "process_optional"): TypeRelationship.TAKES_AS_SUBTYPE_OF_ARGUMENT,
            ("Tag", "process_optional"): TypeRelationship.TAKES_AS_SUBTYPE_OF_ARGUMENT,
            ("Item", "process_union"): TypeRelationship.TAKES_AS_SUBTYPE_OF_ARGUMENT,
        }

    def test_type_to_type_handles_custom_attribute_without_attribute_error(self):
        types = [
            TypeVariableWithAnnotations(
                name="Leaf",
                parent_type=None,
                subtypes=set(),
                attribute_types={},
                import_path=_dummy_import_path(class_name="Leaf"),
                code="",
            ),
            TypeVariableWithAnnotations(
                name="Branch",
                parent_type=None,
                subtypes=set(),
                attribute_types={"leaf": "Leaf", "pair": "Pair[Leaf]"},
                import_path=_dummy_import_path(class_name="Branch"),
                code="",
            ),
        ]
        result = RelationshipEdges.type_to_type(types=types, ignored_types=COMMON_TYPES)
        assert result[("Leaf", "Branch")] == TypeRelationship.CONTAINS_AS_ATTRIBUTE_SUBTYPE
        assert result[("Pair[Leaf]", "Branch")] == TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE


class TestASTExtractionFixes:

    def test_ann_assign_extract_types_has_non_mutable_default(self):
        sig = inspect.signature(ASTAnnAssign.extract_types)
        assert sig.parameters["excluded_types"].default is None

        node = ast.parse("x: dict[str, MyType]").body[0]
        assert isinstance(node, ast.AnnAssign)
        assert ASTAnnAssign.extract_types(node) == {"dict", "str", "MyType"}
        assert ASTAnnAssign.extract_types(node, excluded_types=None) == {"dict", "str", "MyType"}
        assert ASTAnnAssign.extract_types(node, excluded_types={"dict", "str"}) == {"MyType"}
        with pytest.raises(ValueError, match="Expected a set"):
            ASTAnnAssign.extract_types(node, excluded_types=["str"])  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("@dataclass\nclass A:\n    x: int", True),
            ("@dataclass(frozen=True)\nclass A:\n    x: int", True),
            ("@dataclasses.dataclass\nclass A:\n    x: int", True),
            ("@dataclasses.dataclass(frozen=True, slots=True)\nclass A:\n    x: int", True),
            ("@pydantic.dataclasses.dataclass()\nclass A:\n    x: int", True),
            ("@other\nclass A:\n    x: int", False),
            ("@other(flag=True)\nclass A:\n    x: int", False),
            ("@pkg.other\nclass A:\n    x: int", False),
            ("@decorators[0]\nclass A:\n    x: int", False),
            ("@factory()(arg)\nclass A:\n    x: int", False),
            ("class A:\n    x: int", False),
        ],
    )
    def test_is_decorated_as_dataclass_handles_all_decorator_forms(self, code, expected):
        class_node = ast.parse(code).body[0]
        assert ASTClass.is_decorated_as_dataclass(class_node) is expected
        assert ASTClass.is_dataclass(class_node) is expected

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("class A(BaseModel):\n    x: int", True),
            ("class A(pydantic.BaseModel):\n    x: int", True),
            ("class A(Generic[T]):\n    x: int", False),
            ("class A(Generic[T], BaseModel):\n    x: int", True),
            ("class A(Generic[T], pydantic.BaseModel):\n    x: int", True),
            ("class A(BaseModel, Generic[T]):\n    x: int", True),
            ("class A(pydantic.BaseModel[T]):\n    x: int", True),
            ("class A(get_base()):\n    x: int", False),
            ("@dataclass(frozen=True)\nclass A:\n    x: int", True),
            ("@dataclasses.dataclass\nclass A(Generic[T]):\n    x: int", True),
        ],
    )
    def test_is_a_relevant_type_handles_attribute_and_subscript_bases(self, code, expected):
        class_node = ast.parse(code).body[0]
        assert ASTClass.is_a_relevant_type(class_node) is expected

    @pytest.mark.parametrize(
        ("code", "expected_parent"),
        [
            ("class A(Parent):\n    pass", "Parent"),
            ("class A(pydantic.BaseModel):\n    pass", "pydantic.BaseModel"),
            ("class A(Generic[T]):\n    pass", "Generic[T]"),
            ("class A(typing.Generic[T]):\n    pass", "typing.Generic[T]"),
            ("class A:\n    pass", None),
        ],
    )
    def test_parent_type_handles_attribute_and_subscript_bases(self, code, expected_parent):
        class_node = ast.parse(code).body[0]
        assert ASTClass.parent_type(class_node) == expected_parent

    def test_module_relevant_types_and_conversion_end_to_end(self):
        code = (
            "import dataclasses\n"
            "import pydantic\n"
            "from typing import Generic, TypeVar\n\n"
            "T = TypeVar('T')\n\n"
            "@dataclasses.dataclass(frozen=True)\n"
            "class FrozenDC:\n"
            "    value: int\n\n"
            "class QualifiedModel(pydantic.BaseModel):\n"
            "    name: str\n\n"
            "class Container(Generic[T]):\n"
            "    item: T\n"
        )
        tree = ast.parse(code)
        relevant = ASTModule.relevant_types(tree)
        assert set(relevant.keys()) == {"FrozenDC", "QualifiedModel"}
        model_var = ASTType.to_type_variables_with_annotations(
            relevant["QualifiedModel"], module_path_components=("m",)
        )
        assert model_var.parent_type == "pydantic.BaseModel"
        assert model_var.attribute_types == {"name": "str"}

    @pytest.mark.parametrize(
        ("code", "expected_return"),
        [
            ("def f() -> int:\n    pass", "int"),
            ("def f() -> list[int]:\n    pass", "list[int]"),
            ("def f() -> None:\n    pass", "None"),
            ("def f() -> int | str:\n    pass", "int | str"),
            ("def f() -> Foo | None:\n    pass", "Foo | None"),
            ("def f() -> typing.Any:\n    pass", "typing.Any"),
            ("def f() -> pkg.sub.MyType:\n    pass", "pkg.sub.MyType"),
            ("def f() -> pkg.MyType | None:\n    pass", "pkg.MyType | None"),
            ("def f():\n    pass", None),
        ],
    )
    def test_return_annotation_handles_binop_and_attribute(self, code, expected_return):
        func_node = ast.parse(code).body[0]
        assert ASTFunction.return_annotation(func_node) == expected_return


class TestParseModulesFixes:

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
