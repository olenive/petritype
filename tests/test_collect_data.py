import typing
from typing import Sequence

from pydantic import TypeAdapter

from petritype.core.relationship_graph_components import (
    FunctionToTypeEdges,
    RelationshipEdges,
    TypeToFunctionEdges,
    TypeToTypeEdges,
)
from petritype.core.data_structures import (
    COMMON_TYPES,
    FunctionWithAnnotations,
    ImportPath,
    TypeVariableWithAnnotations,
    TypeRelationship,
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


def example_types_01() -> Sequence[TypeVariableWithAnnotations]:
    return [
        TypeVariableWithAnnotations(
            name="Document",
            parent_type=None,
            subtypes=set(),
            attribute_types={"document_name": "str"},
            import_path=ImportPath(
                module_path_components=("core", "data_structures"),
                class_name="Document",
                function_name=None,
            ),
            code=(
                '@dataclass\n'
                'class Document:\n'
                '    """A document in Firestore."""\n'
                '    document_name: str'
            ),
        ),
        TypeVariableWithAnnotations(
            name="Collection",
            parent_type=None,
            subtypes=set(),
            attribute_types={"collection_name": "str"},
            import_path=ImportPath(
                module_path_components=("core", "data_structures"),
                class_name="Collection",
                function_name=None,
            ),
            code=(
                '@dataclass\n'
                'class Collection:\n'
                '    """A collection of documents in Firestore."""\n'
                '    collection_name: str'
            ),
        ),
        TypeVariableWithAnnotations(
            name="ScrapeDocument",
            parent_type="Document",
            subtypes=set(),
            attribute_types={
                "document_name": "str",
                "method": "str",
                "timestamp": "datetime",
                "query": "str",
                "date_since": "datetime",
                "date_until": "datetime",
                "json": "str",
                "document_type": "str",
            },
            import_path=ImportPath(
                module_path_components=("core", "data_structures"),
                class_name="ScrapeDocument",
                function_name=None,
            ),
            code=(
                '@dataclass\n'
                'class ScrapeDocument(Document):\n'
                '    """Unprocessed scrape results and metadata."""\n'
                '    document_name: str\n'
                '    method: str\n'
                '    timestamp: datetime\n'
                '    query: str\n'
                '    date_since: datetime\n'
                '    date_until: datetime\n'
                '    json: str\n'
                '    document_type: str = "ScrapeDocument"'
            ),
        ),
        TypeVariableWithAnnotations(
            name="ScrapesCollection",
            parent_type="Collection",
            subtypes=set(),
            attribute_types={"documents": "dict[str, ScrapeDocument]"},
            import_path=ImportPath(
                module_path_components=("core", "data_structures"),
                class_name="ScrapesCollection",
                function_name=None,
            ),
            code=(
                '@dataclass\n'
                'class ScrapesCollection(Collection):\n'
                '    """A collection of documents in Firestore."""\n'
                '    documents: dict[str, ScrapeDocument]'
            ),
        ),
        TypeVariableWithAnnotations(
            name="AnnotationsDocument",
            parent_type="Document",
            subtypes=set(),
            attribute_types={
                "annotations": "dict[str, Any]",
                "timestamp": "datetime",
                "document_type": "str",
            },
            import_path=ImportPath(
                module_path_components=("core", "data_structures"),
                class_name="AnnotationsDocument",
                function_name=None,
            ),
            code=(
                '@dataclass\n'
                'class AnnotationsDocument(Document):\n'
                '    """A document in Firestore.\n'
                '    """\n'
                '    annotations: dict[str, Any]\n'
                '    timestamp: datetime\n'
                '    document_type: str = "AnnotationsDocument"'
            ),
        ),
        TypeVariableWithAnnotations(
            name="AnnotationsCollection",
            parent_type="Collection",
            subtypes=set(),
            attribute_types={"documents": "dict[str, AnnotationsDocument]"},
            import_path=ImportPath(
                module_path_components=("core", "data_structures"),
                class_name="AnnotationsCollection",
                function_name=None,
            ),
            code=(
                '@dataclass\n'
                'class AnnotationsCollection(Collection):\n'
                '    """A representation of a collection of documents in Firestore.\n'
                '    """\n'
                '    documents: dict[str, AnnotationsDocument]'
            ),
        ),
        TypeVariableWithAnnotations(
            name="TeamDocument",
            parent_type="Document",
            subtypes=set(),
            attribute_types={
                "account_name": "str",
                "timestamp": "datetime",
                "text": "Optional[str]",
                "num_quotes": "Optional[int]",
                "scrapes": "ScrapesCollection",
                "document_type": "str",
                "annotations": "Optional[AnnotationsCollection]",
            },
            import_path=ImportPath(
                module_path_components=("core", "data_structures"),
                class_name="TeamDocument",
                function_name=None,
            ),
            code=(
                '@dataclass\n'
                'class TeamDocument(Document):\n'
                '    """Putting many parameters as optional because a scrape may fail to get some of them."""\n'
                '    account_name: str\n'
                '    timestamp: datetime\n'
                '    text: Optional[str]\n'
                '    scrapes: ScrapesCollection\n'
                '    document_type: str = "TeamDocument"\n'
                '    annotations: Optional[AnnotationsCollection] = None'
            ),
        ),
    ]


class TestRelationshipEdges:

    def test_type_to_type_parent_01(self):
        dummy_import_path = ImportPath(
            module_path_components=["module"],
            class_name=None,
            function_name=None,
        )
        types = (
            TypeVariableWithAnnotations(
                name="A",
                parent_type=None,
                subtypes=set(),
                attribute_types=dict(),
                import_path=dummy_import_path,
                code="",
            ),
            TypeVariableWithAnnotations(
                name="B",
                parent_type="A",
                subtypes=set(),
                attribute_types=dict(),
                import_path=dummy_import_path,
                code="",
            ),
        )
        result = RelationshipEdges.type_to_type(types=types, ignored_types=set())
        expected = {
            ("A", "B"): TypeRelationship.PARENT_OF,
            # NOTE: The reverse relationship is not present and this intentional for now.
        }
        assert result == expected

    def test_type_to_type_with_common_types_01(self):
        dummy_import_path = ImportPath(
            module_path_components=["module"],
            class_name=None,
            function_name=None,
        )
        types = (
            TypeVariableWithAnnotations(
                name="A",
                parent_type=None,
                subtypes=set(),
                attribute_types={"var_name": "str"},
                import_path=dummy_import_path,
                code="",
            ),
            TypeVariableWithAnnotations(
                name="B",
                parent_type=None,
                subtypes=set(),
                attribute_types={"maybe_var": "Optional[str]"},
                import_path=dummy_import_path,
                code="",
            ),
        )
        result = RelationshipEdges.type_to_type(types=types, ignored_types=set())
        expected = {
            ('str', 'A'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE,
            ('Optional[str]', 'B'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE,
            ('Optional', 'B'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_SUBTYPE,
            ('str', 'B'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_SUBTYPE,
        }
        assert result == expected

    def test_type_to_type_sans_common_types_01(self):
        dummy_import_path = ImportPath(
            module_path_components=["module"],
            class_name=None,
            function_name=None,
        )
        types = (
            TypeVariableWithAnnotations(
                name="A",
                parent_type=None,
                subtypes=set(),
                attribute_types={"var_name": "str"},
                import_path=dummy_import_path,
                code="",
            ),
            TypeVariableWithAnnotations(
                name="B",
                parent_type=None,
                subtypes=set(),
                attribute_types={"maybe_var": "Optional[str]"},
                import_path=dummy_import_path,
                code="",
            ),
        )
        result = RelationshipEdges.type_to_type(types=types, ignored_types=set(["str"]))
        expected = {
            # TODO: Should Optional[T] be ignored if T is ignored?
            ('Optional[str]', 'B'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE,
            ('Optional', 'B'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_SUBTYPE,
        }
        assert result == expected

    def test_type_to_type_with_bar_union(self):
        dummy_import_path = ImportPath(
            module_path_components=["module"],
            class_name=None,
            function_name=None,
        )
        types = (
            TypeVariableWithAnnotations(
                name="A",
                parent_type=None,
                subtypes=set(),
                attribute_types={"var_name": "str"},
                import_path=dummy_import_path,
                code="",
            ),
            TypeVariableWithAnnotations(
                name="B",
                parent_type=None,
                subtypes=set(),
                attribute_types={"maybe_var": "str | None"},
                import_path=dummy_import_path,
                code="",
            ),
        )
        result = RelationshipEdges.type_to_type(types=types, ignored_types=set(["str"]))
        expected = {
            # TODO: Should 'str | None' be ignored if T is ignored?
            ('str | None', 'B'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE,
            ('Optional', 'B'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_SUBTYPE,
        }
        assert result == expected

    def test_type_to_type_on_example_01_sans_common_types(self):
        types = example_types_01()
        result = RelationshipEdges.type_to_type(types=types, ignored_types=COMMON_TYPES)
        # TODO: Should various Optional and dict types be ignored if Optional and dict are in COMMON_TYPES?
        expected = {
            ('Document', 'ScrapeDocument'): TypeRelationship.PARENT_OF,
            ('Collection', 'ScrapesCollection'): TypeRelationship.PARENT_OF,
            ('dict[str, ScrapeDocument]', 'ScrapesCollection'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE,
            ('ScrapeDocument', 'ScrapesCollection'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_SUBTYPE,
            ('Document', 'AnnotationsDocument'): TypeRelationship.PARENT_OF,
            ('dict[str, Any]', 'AnnotationsDocument'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE,
            ('Collection', 'AnnotationsCollection'): TypeRelationship.PARENT_OF,
            ('dict[str, AnnotationsDocument]', 'AnnotationsCollection'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE,
            ('AnnotationsDocument', 'AnnotationsCollection'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_SUBTYPE,
            ('Document', 'TeamDocument'): TypeRelationship.PARENT_OF,
            ('Optional[str]', 'TeamDocument'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE,
            ('Optional[int]', 'TeamDocument'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE,
            ('ScrapesCollection', 'TeamDocument'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE,
            ('Optional[AnnotationsCollection]', 'TeamDocument'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE,
            ('AnnotationsCollection', 'TeamDocument'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_SUBTYPE,
        }
        # should_be_expected? = {
        #     ('Document', 'ScrapeDocument'): TypeRelationship.PARENT_OF,
        #     ('Collection', 'ScrapesCollection'): TypeRelationship.PARENT_OF,
        #     ('ScrapeDocument', 'ScrapesCollection'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_SUBTYPE,
        #     ('Document', 'AnnotationsDocument'): TypeRelationship.PARENT_OF,
        #     ('Collection', 'AnnotationsCollection'): TypeRelationship.PARENT_OF,
        #     ('AnnotationsDocument', 'AnnotationsCollection'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_SUBTYPE,
        #     ('Document', 'TeamDocument'): TypeRelationship.PARENT_OF,
        #     ('ScrapesCollection', 'TeamDocument'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE,
        #     ('AnnotationsCollection', 'TeamDocument'): TypeRelationship.CONTAINS_AS_ATTRIBUTE_SUBTYPE,
        # }
        assert result == expected

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


class TestEdgeTypeAliases:

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
