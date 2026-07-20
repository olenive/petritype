import ast
from petritype.core.ast_extraction import ASTModule, ASTType, TypeAnnotation
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
