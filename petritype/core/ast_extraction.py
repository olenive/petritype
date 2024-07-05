import ast
from ast import Module, ClassDef
from typing import Any, Optional, Sequence

from petritype.core.data_structures import (
    COMMON_TYPES, ASTNodeForType, ArgumentTypes, FunctionWithAnnotations, ImportPath,
    TypeName, TypeNameToASTNode, TypeVariableWithAnnotations
)
from petritype.helpers.structures import SafeMerge


class ASTAnnAssign:

    def extract_types(node: ast.AnnAssign, excluded_types: set[TypeName] = set()) -> set[TypeName]:
        if not isinstance(excluded_types, set):
            raise ValueError("Expected a set of excluded types but got: " + str(type(excluded_types)))
        if not isinstance(node, ast.AnnAssign):
            raise ValueError("Expected an annotation assignment node but got: " + str(node))
        subnodes = list(ast.walk(node))[
            2:  # The first returned node is the whole node and the next is the variable name.
        ]
        out = set()
        for subnode in subnodes:
            if isinstance(subnode, ast.Name):
                out.add(subnode.id)
        return out - excluded_types


class TypeAnnotation:

    def outermost_type(type_annotation: str) -> str:
        tree = ast.parse(type_annotation, mode='eval')
        expression = tree.body  # The root of the tree in 'eval' mode will be an Expression.
        if isinstance(expression, ast.Name):
            return expression.id
        elif isinstance(expression, ast.Subscript) and isinstance(expression.value, ast.Name):
            return expression.value.id
        elif isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.BitOr):
            # Check if one of the subtypes is None.
            for node in ast.walk(expression):
                if isinstance(node, ast.Constant) and node.value is None:
                    return "Optional"
            return "Union"
        else:
            raise ValueError("Expected a subscripted type annotation but got: " + type_annotation)

    def subtypes(
        annotation: Optional[str],
        ignored_types=COMMON_TYPES,
    ) -> set[str]:
        """Take an annotation such as Optional[Union[T, U, str, int]] and return the subtypes T and U."""
        out = set()
        if annotation is None:
            return out
        nodes = tuple(
            x for x in ast.walk(ast.parse(annotation).body[0])
            if (isinstance(x, ast.Name) or isinstance(x, ast.Constant))
        )
        if len(nodes) == 1:
            # return {nodes[0].id}  # The annotated type is a single type.  # TODO: what about type aliases?
            return set()
        for node in nodes:
            if isinstance(node, ast.Name) and node.id not in ignored_types:
                out.add(node.id)
            elif isinstance(node, ast.Constant) and node.value is None:
                out.add("Optional")
        # for t in ast.walk(ast.parse(annotation).body[0]):
        #     if isinstance(t, ast.Name) and t.id not in ignored_types:
        #         out.add(t.id)
        return out - ignored_types


class ASTFunction:

    def arg_annotations(func_def: ast.FunctionDef) -> dict[str, str]:
        return {arg.arg: ast.unparse(arg.annotation) if arg.annotation else None for arg in func_def.args.args}

    def return_annotation(func_def: ast.FunctionDef) -> Optional[str]:
        if func_def.returns:
            if isinstance(func_def.returns, ast.Name):
                return func_def.returns.id
            elif isinstance(func_def.returns, ast.Subscript):
                return ast.unparse(func_def.returns)
            elif isinstance(func_def.returns, ast.Constant):
                return ast.unparse(func_def.returns)
        return None

    def names_and_annotations(
        func_def: ast.FunctionDef,
        module_path_components: Sequence[str],
        class_name: Optional[str] = None,
    ) -> FunctionWithAnnotations:

        def id_from_class_and_name(class_name: Optional[str], function_name: str) -> str:
            first = class_name + "." if class_name else ""
            return first + function_name

        return FunctionWithAnnotations(
            function_full_name=id_from_class_and_name(class_name, func_def.name),
            class_name=class_name,
            function_name=func_def.name,
            argument_types=ASTFunction.arg_annotations(func_def),
            return_type=ASTFunction.return_annotation(func_def),
            import_path=ImportPath(
                module_path_components=module_path_components, class_name=class_name, function_name=func_def.name
            ),
            code=ast.unparse(func_def),
        )


class ASTClass:

    def is_decorated_as_dataclass(class_node: Any) -> bool:
        if not isinstance(class_node, ast.ClassDef):
            return False
        return any(decorator.id == "dataclass" for decorator in class_node.decorator_list)

    def is_dataclass(class_node: ClassDef) -> bool:
        # TODO: What about inheriting from dataclass rather than using a decorator?
        return ASTClass.is_decorated_as_dataclass(class_node)

    def is_a_relevant_type(class_node: ClassDef) -> bool:
        if ASTClass.is_dataclass(class_node):
            return True
        # Check if the class inherits from a pydantic BaseModel.
        if class_node.bases:
            for base in class_node.bases:
                if base.id == "BaseModel":
                    return True
        return False

    def functions(class_node: ClassDef) -> tuple[ast.FunctionDef, ...]:
        return tuple(
            node for node in ast.walk(class_node)
            if (isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef))
        )

    def functions_with_annotations(
        class_node: ClassDef, module_path_components: Sequence[str], include_private: bool = True
    ) -> list[FunctionWithAnnotations]:
        out = []
        for func in ASTClass.functions(class_node):
            if not include_private and func.name.startswith("_"):
                continue
            out.append(ASTFunction.names_and_annotations(
                func, module_path_components=module_path_components, class_name=class_node.name
            ))
        return out

    def parent_type(class_def: ClassDef) -> Optional[TypeName]:
        if isinstance(class_def, ast.ClassDef) and class_def.bases:
            return class_def.bases[0].id
        return None

    def attribute_types(class_def: ClassDef) -> ArgumentTypes:
        out = {}
        if not isinstance(class_def, ast.ClassDef):
            return out
        for node in class_def.body:
            if isinstance(node, ast.AnnAssign):
                out[node.target.id] = ast.unparse(node.annotation)
        return out


class ASTType:

    def alias_subtypes(node: ast.TypeAlias | ast.ClassDef) -> set[TypeName]:
        if isinstance(node, ast.TypeAlias):
            # Use TypeAnnotation.subtypes to extract the subtypes of the alias including the alias itself.
            # Subtract the alias itself from the set of subtypes because we only want the subtypes.
            # There should be a more elegant way to do this but it's not obvious to me at the time of writing.
            all_types = TypeAnnotation.subtypes(ast.unparse(node))
            return all_types - {node.name.id}
        return set()

    def to_type_variables_with_annotations(
        node: ASTNodeForType, module_path_components: Sequence[str]
    ) -> TypeVariableWithAnnotations:
        """Not every class should be considered a type (e.g. some are just name spaces for functions).

        Therefore, classes should be selected before being passed to this function.
        """

        def type_name_from_ast_node() -> str:
            if isinstance(node, ast.ClassDef):
                return node.name
            elif isinstance(node, ast.AnnAssign):
                return node.target.id
            elif isinstance(node, ast.TypeAlias):
                return node.name.id
            else:
                raise ValueError("Unexpected node type: " + str(type(node)))

        def class_name_from_ast_node() -> Optional[str]:
            if isinstance(node, ast.ClassDef):
                return node.name
            return None

        type_name = type_name_from_ast_node()
        return TypeVariableWithAnnotations(
            name=type_name,
            parent_type=ASTClass.parent_type(node),
            subtypes=ASTType.alias_subtypes(node),
            attribute_types=ASTClass.attribute_types(node),
            import_path=ImportPath(
                module_path_components=module_path_components,
                class_name=class_name_from_ast_node(),
                function_name=None,
            ),
            code=ast.unparse(node),
        )


class ASTModule:

    def class_declarations(module_tree: Module) -> dict[str, ClassDef]:
        return {node.name: node for node in ast.walk(module_tree) if isinstance(node, ast.ClassDef)}

    def type_aliases_to_nodes(module_tree: Module) -> TypeNameToASTNode:
        return {
            node.name.id: node for node in ast.walk(module_tree) if isinstance(node, ast.TypeAlias)
        }

    def relevant_types(module_tree: Module) -> TypeNameToASTNode:
        types_from_classes = {
            name: node for name, node in ASTModule.class_declarations(module_tree).items()
            if ASTClass.is_a_relevant_type(node)
        }
        # Annotations such as type A = Tuple[int, str] are represented using ast.AnnAssign nodes,
        # while general assignments like VAL = "foo" are represented using ast.Assign nodes
        types_from_type_aliases = ASTModule.type_aliases_to_nodes(module_tree)
        return SafeMerge.dictionaries(types_from_classes, types_from_type_aliases)
