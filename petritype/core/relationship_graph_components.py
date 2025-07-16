"""
The goal here is to collect all the information needed for the relationship graph without including
any graph library specific implementation details, such as node indices used by rustworkx.
As well as potentially offering interoperability with multiple graph libraries this gives us a chance to check
that there are no ID clashes. This is important because python does not require that a class or function name
be unique across all modules. However, in a graph we need to be able to uniquely identify nodes.
Working with a codebase that uses non-unique type or class.function names is not supported in this version.
"""


from typing import Sequence
from petritype.core.ast_extraction import TypeAnnotation
from petritype.core.data_structures import (
    COMMON_TYPES,
    FunctionFullName, FunctionWithAnnotations, TypeName, TypeRelationship,
    TypeVariableWithAnnotations
)


# Relationship Edges
type DataForTypeNodes = dict[TypeName, TypeVariableWithAnnotations]
type DataForFunctionNodes = dict[FunctionFullName, FunctionWithAnnotations]
type TypeToTypeEdges = dict[tuple[tuple[TypeName, TypeName], TypeRelationship]]
type TypeToFunctionEdges = dict[tuple[tuple[TypeName, FunctionFullName], TypeRelationship]]
type FunctionToTypeEdges = dict[tuple[tuple[FunctionFullName, TypeName], TypeRelationship]]


class RelationshipEdges:

    def type_to_type(
        types: Sequence[TypeVariableWithAnnotations],
        ignored_types: set[TypeName] = COMMON_TYPES,
    ) -> TypeToTypeEdges:
        out = dict()
        for type_variable in types:
            parent_type = type_variable.parent_type
            if parent_type is not None and parent_type not in ignored_types:
                from_to = (parent_type, type_variable.name)
                out[from_to] = TypeRelationship.PARENT_OF
            for _, attr_type in type_variable.attribute_types.items():
                if attr_type in ignored_types:
                    continue
                # If not ignored, then the attr_type is automatically contained as an attribute type.
                out[(attr_type, type_variable.name)] = TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE
                subtypes = TypeAnnotation.subtypes(attr_type, ignored_types=set())
                if subtypes.intersection(ignored_types) == set():
                    from_to = (attr_type, type_variable.name)
                    # Check if the type is already present as an exact attribute type.
                    if out.get(from_to) != TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE:
                        out[from_to] = TypeRelationship.CONTAINED_IN_ATTRIBUTE_TYPE
                attr_subtypes = TypeAnnotation.subtypes(attr_type, ignored_types=ignored_types)
                for subtype in attr_subtypes:
                    if subtype in ignored_types:
                        continue
                    from_to = (subtype, type_variable.name)
                    out[from_to] = TypeRelationship.CONTAINS_AS_ATTRIBUTE_SUBTYPE
            for subtype in type_variable.subtypes:
                if subtype in ignored_types:
                    continue
                from_to = (type_variable.name, subtype)
                out[from_to] = TypeRelationship.CONTAINS_AS_SUBTYPE
        return out

    def type_to_function(
        types: Sequence[TypeVariableWithAnnotations],
        functions: Sequence[FunctionWithAnnotations],
        ignored_types: set[TypeName] = COMMON_TYPES,
    ) -> TypeToFunctionEdges:
        out = dict()
        for type_variable in types:
            for function in functions:
                for _, arg_type in function.argument_types.items():
                    if (
                        arg_type is not None
                        and arg_type not in ignored_types
                        and arg_type == type_variable.name
                    ):
                        from_to = (type_variable.name, function.function_full_name)
                        out[from_to] = TypeRelationship.TAKES_AS_EXACT_TYPE_OF_ARGUMENT
                    arg_subtypes = TypeAnnotation.subtypes(arg_type, ignored_types=ignored_types)
                    for subtype in arg_subtypes:
                        if subtype not in ignored_types and subtype == type_variable.name:
                            from_to = (type_variable.name, function.function_full_name)
                            out[from_to] = TypeRelationship.TAKES_AS_EXACT_TYPE_OF_ARGUMENT
        return out

    def function_to_type(
        functions: Sequence[FunctionWithAnnotations],
        types: Sequence[TypeVariableWithAnnotations],
        ignored_types: set[TypeName] = COMMON_TYPES,
    ) -> FunctionToTypeEdges:
        out = dict()
        type_names = {type_variable.name for type_variable in types}
        for function in functions:
            if function.return_type is not None and function.return_type not in ignored_types:
                if function.return_type in type_names:
                    from_to = (function.function_full_name, function.return_type)
                    out[from_to] = TypeRelationship.RETURNS_EXACTLY_THIS_TYPE
                returned_subtypes = TypeAnnotation.subtypes(function.return_type, ignored_types=ignored_types)
                for subtype in returned_subtypes:
                    if subtype in type_names and (subtype not in ignored_types):
                        from_to = (function.function_full_name, subtype)
                        out[from_to] = TypeRelationship.RETURN_CONTAINS_THIS_AS_SUBTYPE
        return out
