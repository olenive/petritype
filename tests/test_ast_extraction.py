import ast
import inspect

import pytest

from petritype.core.ast_extraction import (
    ASTAnnAssign,
    ASTClass,
    ASTFunction,
    ASTModule,
    ASTType,
    TypeAnnotation,
)
from petritype.core.data_structures import COMMON_TYPES, ImportPath, TypeVariableWithAnnotations


class TestTypeAnnotation:

    def test_outermost_type_case_with_str(self):
        annotation = "str"
        result = TypeAnnotation.outermost_type(annotation)
        assert result == "str"

    def test_outermost_type_case_with_typed_dict(self):
        annotation = "dict[str, ScrapeDocument]"
        result = TypeAnnotation.outermost_type(annotation)
        assert result == "dict"

    def test_outermost_type_case_with_optional(self):
        annotation = "Optional[Union[str, int]]"
        result = TypeAnnotation.outermost_type(annotation)
        assert result == "Optional"

    def test_outermost_type_case_with_union(self):
        annotation = "Union[str, int]"
        result = TypeAnnotation.outermost_type(annotation)
        assert result == "Union"

    def test_outermost_type_case_with_bar_union(self):
        annotation = "str | int"
        result = TypeAnnotation.outermost_type(annotation)
        assert result == "Union"

    def test_outermost_type_case_with_bar_optional_left(self):
        annotation = "None | str"
        result = TypeAnnotation.outermost_type(annotation)
        assert result == "Optional"

    def test_outermost_type_case_with_bar_optional_right(self):
        annotation = "str | None"
        result = TypeAnnotation.outermost_type(annotation)
        assert result == "Optional"

    def test_outermost_type_case_with_bar_optional_both(self):
        annotation = "None | str | None"
        result = TypeAnnotation.outermost_type(annotation)
        assert result == "Optional"

    def test_outermost_type_case_with_bar_optional_both_01(self):
        annotation = "None | str | bool | int"
        result = TypeAnnotation.outermost_type(annotation)
        assert result == "Optional"

    def test_subtypes_optional_str(self):
        annotation = "Optional[str]"
        out = TypeAnnotation.subtypes(annotation, ignored_types=set())
        assert out == {"Optional", "str"}

    def test_subtypes_bar_optional_str_01(self):
        annotation = "None | str"
        out = TypeAnnotation.subtypes(annotation, ignored_types=set())
        assert out == {"Optional", "str"}

    def test_subtypes_bar_optional_str_02(self):
        annotation = "str | None"
        out = TypeAnnotation.subtypes(annotation, ignored_types=set())
        assert out == {"Optional", "str"}

    def test_subtypes_optional_str_ignoring_str(self):
        annotation = "Optional[str]"
        out = TypeAnnotation.subtypes(annotation, ignored_types=set(["str"]))
        assert out == {"Optional"}

    def test_subtypes_documented_case(self):
        annotation = "Optional[Union[T, U, str, int]]"
        out = TypeAnnotation.subtypes(annotation, ignored_types=COMMON_TYPES)
        assert out == {"T", "U"}

    # TODO: What happens when there is no type annotation?

    def test_subtypes_returns_empty_set_when_annotation_is_a_single_type(self):
        annotation = "MyType"
        out = TypeAnnotation.subtypes(annotation, ignored_types=COMMON_TYPES)
        assert out == set()

    def test_subtypes_case_with_none_00(self):
        """TODO: Figure out if this should be the way to handle None type annotations."""
        annotation = "None"
        out = TypeAnnotation.subtypes(annotation, ignored_types=set())
        assert out == set()

    def test_subtypes_case_with_none_01(self):
        annotation = "Optional[Union[T, U, str, None, int]]"
        out = TypeAnnotation.subtypes(annotation, ignored_types=COMMON_TYPES)
        assert out == {"T", "U"}

    def test_subtypes_with_no_ignored_types(self):
        annotation = "Optional[Union[Type, str, int]]"
        out = TypeAnnotation.subtypes(annotation, ignored_types=set())
        assert out == {"Optional", "Union", "Type", "str", "int"}


class TestASTModule:

    def test_type_names_from_type_aliases_simple_case_01(self):
        code = "type MyType = Union[str, int]"
        tree = ast.parse(code)
        my_type_node = tree.body[0]
        result = ASTModule.type_aliases_to_nodes(tree)
        assert result == {"MyType": my_type_node}

    def test_type_names_from_type_aliases_does_not_extract_attributes_from_pydantic_models(self):
        code = (
            "from pydantic import BaseModel\n"
            "class MyModel(BaseModel):\n"
            "    name: str\n"
            "    age: int\n"
        )
        tree = ast.parse(code)
        result = ASTModule.type_aliases_to_nodes(tree)
        assert result == dict()

    def test_relevant_types_and_conversion_end_to_end(self):
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


class TestASTAnnAssign:

    def test_extract_types_has_non_mutable_default(self):
        sig = inspect.signature(ASTAnnAssign.extract_types)
        assert sig.parameters["excluded_types"].default is None

        node = ast.parse("x: dict[str, MyType]").body[0]
        assert isinstance(node, ast.AnnAssign)
        assert ASTAnnAssign.extract_types(node) == {"dict", "str", "MyType"}
        assert ASTAnnAssign.extract_types(node, excluded_types=None) == {"dict", "str", "MyType"}
        assert ASTAnnAssign.extract_types(node, excluded_types={"dict", "str"}) == {"MyType"}
        with pytest.raises(ValueError, match="Expected a set"):
            ASTAnnAssign.extract_types(node, excluded_types=["str"])  # type: ignore[arg-type]


class TestASTClass:

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


class TestASTFunction:

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


class TestASTType:

    def test_to_type_variables_with_annotations_with_type_alias_for_str(self):
        module_path_components = ("some", "module")
        code = "type MyType = str"
        tree = ast.parse(code)
        type_alias_node = tree.body[0]
        expected = TypeVariableWithAnnotations(
            name="MyType",
            parent_type=None,
            subtypes=set(),
            attribute_types=dict(),
            import_path=ImportPath(
                module_path_components=module_path_components,
                class_name=None,
                function_name=None
            ),
            code=code
        )
        result: TypeVariableWithAnnotations = ASTType.to_type_variables_with_annotations(
            node=type_alias_node, module_path_components=module_path_components
        )
        assert result == expected
