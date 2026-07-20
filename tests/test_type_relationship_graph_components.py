# import pytest
# from petritype.core.type_relationship_graph_components import (
#     RelationshipGraphComponent, RelationshipGraphFunctionNodeData, RelationshipGraphTypeNodeData
# )
# from petritype.core.data_structures import FunctionWithAnnotations, ImportPath, TypeRelationship


# class TestRelationshipGraphTypeNodeData:

#     # def test_mapping_function_maker_exact_type_to_type(self):
#     #     type_to_node_index = {"A": 0, "B": 1, "C": 2}
#     #     mapping_function = RelationshipGraphComponent.matching_function_maker(type_to_node_index)
#     #     assert mapping_function("A", TypeContext.TYPE_NAME) == ("A", 0, TypeRelationship.ANNOTATED_AS)
#     #     expected = 0
#     #     assert result == expected

#     def test_edges_between_functions_and_types_simple_case_01(self):
#         type_names_to_node_indices = {"A": 0, "B": 1, "C": 2}
#         import pdb; pdb.set_trace()
#         function_node_inidices_to_data = {
#             3: RelationshipGraphFunctionNodeData(
#                 name="f1",
#                 function_with_annotations=FunctionWithAnnotations(
#                     function_full_name="Someclass.f1",
#                     class_name="Someclass",
#                     function_name="f1",
#                     argument_types={"a": "A"},
#                     return_type="B",
#                     import_path=ImportPath(
#                         module_path_components=("module", "file"),
#                         class_name="Someclass",
#                         function_name="f1",
#                     ),
#                     code="def f1(a: A) -> B:\n    pass",
#                 ),
#             ),
#             4: RelationshipGraphFunctionNodeData(
#                 name="f2",
#                 function_with_annotations=FunctionWithAnnotations(
#                     function_full_name="Someclass.f2",
#                     class_name="Someclass",
#                     function_name="f2",
#                     argument_types={"b": "B"},
#                     return_type="C",
#                     import_path=ImportPath(
#                         module_path_components=("module", "file"),
#                         class_name="Someclass",
#                         function_name="f2",
#                     ),
#                     code="def f2(b: B) -> C:\n    pass",
#                 ),
#             ),
#         }
#         result = RelationshipGraphComponent.edges_between_functions_and_types(
#             function_nodes=function_node_inidices_to_data, matching_function=matching_function
#         )
#         expected = [
#             (
#                 0,  # A
#                 3,  # f1
#                 TypeRelationship.ARGUMENT_TO
#             ),
#             (
#                 3,  # f1
#                 1,  # B
#                 TypeRelationship.RETURNS
#             ),
#             (
#                 1,  # B
#                 4,  # f2
#                 TypeRelationship.ARGUMENT_TO
#             ),
#             (
#                 4,  # f2
#                 2,  # C
#                 TypeRelationship.RETURNS
#             ),
#         ]
#         import pdb; pdb.set_trace()
#         assert result == expected

#     def test_edges_between_functions_and_types_simple_case_02(self):
#         type_names_to_node_indices = {"A1": 0, "B": 1, "C": 2, "A2": 5}
#         function_node_inidices_to_data = {
#             3: RelationshipGraphFunctionNodeData(
#                 name="f1",
#                 function_with_annotations=FunctionWithAnnotations(
#                     class_name="Someclass",
#                     function_name="f1",
#                     argument_types={"a": "A", "aa": "A2"},
#                     return_type="B",
#                     import_path=ImportPath(
#                         module_path_components=("module", "file"),
#                         class_name="Someclass",
#                         function_name="f1",
#                     ),
#                     code="def f1(a: A1, aa: A2) -> B:\n    pass",
#                 ),
#             ),
#             4: RelationshipGraphFunctionNodeData(
#                 name="f2",
#                 function_with_annotations=FunctionWithAnnotations(
#                     class_name="Someclass",
#                     function_name="f2",
#                     argument_types={"b": "B"},
#                     return_type="C",
#                     import_path=ImportPath(
#                         module_path_components=("module", "file"),
#                         class_name="Someclass",
#                         function_name="f2",
#                     ),
#                     code="def f2(b: B) -> C:\n    pass",
#                 ),
#             ),
#         }
#         matching_function = RelationshipGraphComponent.matching_subtypes_function_maker(
#             type_to_node_index=type_names_to_node_indices
#         )
#         result = RelationshipGraphComponent.edges_between_functions_and_types(
#             function_nodes=function_node_inidices_to_data, matching_function=matching_function
#         )
#         expected = [
#             (
#                 0,  # A
#                 3,  # f1
#                 TypeRelationship.ARGUMENT_TO
#             ),
#             (
#                 5,  # A2
#                 3,  # f1
#                 TypeRelationship.ARGUMENT_TO
#             ),
#             (
#                 3,  # f1
#                 1,  # B
#                 TypeRelationship.RETURNS
#             ),
#             (
#                 1,  # B
#                 4,  # f2
#                 TypeRelationship.ARGUMENT_TO
#             ),
#             (
#                 4,  # f2
#                 2,  # C
#                 TypeRelationship.RETURNS
#             ),
#         ]
#         assert result == expected

#     def test_edges_between_functions_and_types_ignoring_outer_optional_01(self):
#         type_names_to_node_indices = {"A1": 0, "B": 1, "C": 2, "A2": 5}
#         function_node_inidices_to_data = {
#             3: RelationshipGraphFunctionNodeData(
#                 name="f1",
#                 function_with_annotations=FunctionWithAnnotations(
#                     class_name="Someclass",
#                     function_name="f1",
#                     argument_types={"a": "A", "aa": "Optional[A2]"},
#                     return_type="Optional[B]",
#                     import_path=ImportPath(
#                         module_path_components=("module", "file"),
#                         class_name="Someclass",
#                         function_name="f1",
#                     ),
#                     code="def f1(a: A1, aa: A2) -> B:\n    pass",
#                 ),
#             ),
#             4: RelationshipGraphFunctionNodeData(
#                 name="f2",
#                 function_with_annotations=FunctionWithAnnotations(
#                     class_name="Someclass",
#                     function_name="f2",
#                     argument_types={"b": "Optional[B]"},
#                     return_type="Optional[C]",
#                     import_path=ImportPath(
#                         module_path_components=("module", "file"),
#                         class_name="Someclass",
#                         function_name="f2",
#                     ),
#                     code="def f2(b: B) -> C:\n    pass",
#                 ),
#             ),
#         }
#         matching_function = RelationshipGraphComponent.matching_function_maker_ignoring_outer_optional(
#             type_to_node_index=type_names_to_node_indices
#         )
#         result = RelationshipGraphComponent.edges_between_functions_and_types(
#             function_nodes=function_node_inidices_to_data, matching_function=matching_function
#         )
#         expected = [
#             (
#                 0,  # A
#                 3,  # f1
#                 TypeRelationship.ARGUMENT_TO
#             ),
#             (
#                 5,  # A2
#                 3,  # f1
#                 TypeRelationship.ARGUMENT_TO
#             ),
#             (
#                 3,  # f1
#                 1,  # B
#                 TypeRelationship.RETURNS
#             ),
#             (
#                 1,  # B
#                 4,  # f2
#                 TypeRelationship.ARGUMENT_TO
#             ),
#             (
#                 4,  # f2
#                 2,  # C
#                 TypeRelationship.RETURNS
#             ),
#         ]
#         import pdb; pdb.set_trace()
#         assert result == expected

#     def test_edges_between_functions_and_types_matching_subtypes_with_optional_01(self):
#         type_names_to_node_indices = {"A1": 0, "B": 1, "C": 2, "A2": 5}
#         function_node_inidices_to_data = {
#             3: RelationshipGraphFunctionNodeData(
#                 name="f1",
#                 function_with_annotations=FunctionWithAnnotations(
#                     class_name="Someclass",
#                     function_name="f1",
#                     argument_types={"a": "A", "aa": "Optional[A2]"},
#                     return_type="Optional[B]",
#                     import_path=ImportPath(
#                         module_path_components=("module", "file"),
#                         class_name="Someclass",
#                         function_name="f1",
#                     ),
#                     code="def f1(a: A1, aa: A2) -> B:\n    pass",
#                 ),
#             ),
#             4: RelationshipGraphFunctionNodeData(
#                 name="f2",
#                 function_with_annotations=FunctionWithAnnotations(
#                     class_name="Someclass",
#                     function_name="f2",
#                     argument_types={"b": "Optional[B]"},
#                     return_type="Optional[C]",
#                     import_path=ImportPath(
#                         module_path_components=("module", "file"),
#                         class_name="Someclass",
#                         function_name="f2",
#                     ),
#                     code="def f2(b: B) -> C:\n    pass",
#                 ),
#             ),
#         }
#         matching_function = RelationshipGraphComponent.matching_subtypes_function_maker(
#             type_to_node_index=type_names_to_node_indices
#         )
#         result = RelationshipGraphComponent.edges_between_functions_and_types(
#             function_nodes=function_node_inidices_to_data, matching_function=matching_function
#         )
#         expected = [
#             (
#                 0,  # A
#                 3,  # f1
#                 TypeRelationship.ARGUMENT_TO
#             ),
#             (
#                 5,  # A2
#                 3,  # f1
#                 TypeRelationship.ARGUMENT_TO
#             ),
#             (
#                 3,  # f1
#                 1,  # B
#                 TypeRelationship.RETURNS
#             ),
#             (
#                 1,  # B
#                 4,  # f2
#                 TypeRelationship.ARGUMENT_TO
#             ),
#             (
#                 4,  # f2
#                 2,  # C
#                 TypeRelationship.RETURNS
#             ),
#         ]
#         import pdb; pdb.set_trace()
#         assert result == expected

#     @pytest.mark.skip("Remove this unless it actually discovers something interesting.")
#     def test_edges_between_functions_and_types_debug_01(self):
#         type_names_to_node_indices = {
#             'Document': 0, 'Collection': 1, 'SectionDocument': 2, 'SectionsCollection': 3, 'AnnotationsDocument': 4,
#             'AnnotationsCollection': 5, 'TopicDocument': 6
#         }
#         function_node_indices_to_data = {
#             7: RelationshipGraphFunctionNodeData(
#                 name="Mismatches._relevant_topic_document_fields",
#                 function_with_annotations=FunctionWithAnnotations(
#                     class_name="Mismatches",
#                     function_name="_relevant_topic_document_fields",
#                     argument_types={"doc": "TopicDocument"},
#                     return_type="Sequence[str]",
#                     import_path=ImportPath(
#                         module_path_components=("core", "data_structures"),
#                         class_name="Mismatches",
#                         function_name="_relevant_topic_document_fields",
#                     ),
#                     code="def _relevant_topic_document_fields(doc: TopicDocument) -> Sequence[str]:\n    pass",
#                 ),
#             ),
#             8: RelationshipGraphFunctionNodeData(
#                 name="Mismatches.between_two_topic_documents",
#                 function_with_annotations=FunctionWithAnnotations(
#                     class_name="Mismatches",
#                     function_name="between_two_topic_documents",
#                     argument_types={"doc1": "TopicDocument", "doc2": "TopicDocument"},
#                     return_type="dict[str, tuple[Any, Any]]",
#                     import_path=ImportPath(
#                         module_path_components=("core", "data_structures"),
#                         class_name="Mismatches",
#                         function_name="between_two_topic_documents",
#                     ),
#                     code=(
#                         'def between_two_topic_documents(doc1: TopicDocument, doc2: TopicDocument)'
#                         ' -> dict[str, tuple[Any, Any]]:\n    pass'
#                     ),
#                 ),
#             ),
#             9: RelationshipGraphFunctionNodeData(
#                 name="Mismatches.between_two_annotations_documents",
#                 function_with_annotations=FunctionWithAnnotations(
#                     class_name="Mismatches",
#                     function_name="between_two_annotations_documents",
#                     argument_types={
#                         "doc1": "AnnotationsDocument",
#                         "doc2": "AnnotationsDocument",
#                     },
#                     return_type="Optional[tuple[AnnotationsDocument, AnnotationsDocument]]",
#                     import_path=ImportPath(
#                         module_path_components=("core", "data_structures"),
#                         class_name="Mismatches",
#                         function_name="between_two_annotations_documents",
#                     ),
#                     code=(
#                         "def between_two_annotations_documents(doc1: AnnotationsDocument, doc2: AnnotationsDocument)"
#                         " -> Optional[tuple[AnnotationsDocument, AnnotationsDocument]]:\n    pass"
#                     ),
#                 ),
#             ),
#             10: RelationshipGraphFunctionNodeData(
#                 name="Mismatches.between_two_annotations_collections",
#                 function_with_annotations=FunctionWithAnnotations(
#                     class_name="Mismatches",
#                     function_name="between_two_annotations_collections",
#                     argument_types={
#                         "collection_1": "Optional[AnnotationsCollection]",
#                         "collection_2": "Optional[AnnotationsCollection]",
#                     },
#                     return_type="tuple[dict[str, AnnotationsDocument], dict[str, AnnotationsDocument]]",
#                     import_path=ImportPath(
#                         module_path_components=("core", "data_structures"),
#                         class_name="Mismatches",
#                         function_name="between_two_annotations_collections",
#                     ),
#                     code=(
#                         'def between_two_annotations_collections(collection_1: Optional[AnnotationsCollection], '
#                         'collection_2: Optional[AnnotationsCollection]) -> tuple[dict[str, AnnotationsDocument], '
#                         'dict[str, AnnotationsDocument]]:\n    pass'
#                     ),
#                 ),
#             ),
#         }
#         matching_function = RelationshipGraphComponent.matching_subtypes_function_maker(
#             type_to_node_index=type_names_to_node_indices
#         )
#         result = RelationshipGraphComponent.edges_between_functions_and_types(
#             function_nodes=function_node_indices_to_data, matching_function=matching_function
#         )
#         expected = [
#             (
#                 7,  # Mismatches._relevant_topic_document_fields
#                 6,  # TopicDocument
#                 TypeRelationship.ARGUMENT_TO,
#             ),
#         ]
#         import pdb; pdb.set_trace()
#         assert result == expected
