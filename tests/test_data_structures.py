# from petritype.core.data_structures import TypeRelationship, TypeRelationships, VariableTypeRelationships
# from petritype.core.type_relationship_graph_components import RelationshipGraphTypeNodeData


# class TestVariableTypeRelationships:

#     def test_instantiation_basic_example(self):
#         VariableTypeRelationships(
#             class_name="class_name",
#             variable_name="variable_name",
#             relationship=TypeRelationship.CONTAINS,
#             type_annotation="type_annotation",
#             subtypes=tuple()
#         )


# class TestRelationshipGraphTypeNodeData:

#     # def test_instantiation_with_empty_tuple_of_type_relationships(self):
#     #     type_relationships: TypeRelationships = tuple()
#     #     RelationshipGraphTypeNodeData(type_relationships=type_relationships)

#     def test_instantiation_with_type_relationships_of_length_02(self):
#         """This test fails if the model validator in VariableTypeRelationships does not return self."""
#         type_relationships: TypeRelationships = [
#             VariableTypeRelationships(
#                 class_name="class_name",
#                 variable_name="variable_name_1",
#                 relationship=TypeRelationship.CONTAINS,
#                 type_annotation="type_annotation",
#                 subtypes=tuple()
#             ),
#             VariableTypeRelationships(
#                 class_name="class_name",
#                 variable_name="variable_name_2",
#                 relationship=TypeRelationship.CONTAINS,
#                 type_annotation="type_annotation",
#                 subtypes=tuple()
#             ),
#         ]
#         result = RelationshipGraphTypeNodeData(type_relationships=type_relationships)
#         assert result.type_relationships == type_relationships
