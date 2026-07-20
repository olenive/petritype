from typing import Sequence
from petritype.core.relationship_graph_components import RelationshipEdges
from petritype.core.data_structures import (
    COMMON_TYPES,
    ImportPath,
    TypeVariableWithAnnotations,
    TypeRelationship,
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
