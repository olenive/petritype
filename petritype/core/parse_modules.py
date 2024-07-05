import ast
from typing import Iterable, Sequence
from pydantic import BaseModel

from petritype.core.ast_extraction import ASTClass, ASTModule, ASTType
from petritype.core.data_structures import FunctionWithAnnotations, TypeNameToASTNode, TypeVariableWithAnnotations


class ParsedModule(BaseModel):
    path_to_file: str
    import_path_components: Sequence[str]
    code: str


class ParseModule:

    def from_file(path_to_file: str, import_path_components: Sequence[str]) -> ParsedModule:
        with open(path_to_file, "r") as file:
            code = file.read()
        return ParsedModule(
            path_to_file=path_to_file,
            import_path_components=import_path_components,
            code=code,
        )


class ExtractFunctions:

    def from_selected_classes_in_parsed_module(
        *,
        parsed_module: ParsedModule,
        selected_classes: set[str],
        include_private: bool = True,
    ) -> Sequence[FunctionWithAnnotations]:
        module_ast = ast.parse(parsed_module.code)
        class_declarations = ASTModule.class_declarations(module_ast)
        functions = []
        for name, class_def in class_declarations.items():
            if name in selected_classes:
                functions.extend(
                    ASTClass.functions_with_annotations(
                        class_def,
                        parsed_module.import_path_components,
                        include_private=include_private,
                    )
                )
        return functions

    def from_selected_classes_in_parsed_modules(
        *,
        parsed_modules: Sequence[ParsedModule],
        selected_classes: set[str],
        include_private: bool = True,
    ) -> Sequence[FunctionWithAnnotations]:
        functions = []
        for parsed_module in parsed_modules:
            functions.extend(
                ExtractFunctions.from_selected_classes_in_parsed_module(
                    parsed_module=parsed_module,
                    selected_classes=selected_classes,
                    include_private=include_private,
                )
            )
        return functions


class ExtractTypes:

    def from_parsed_module(
        parsed_module: ParsedModule,
    ) -> Sequence[TypeVariableWithAnnotations]:
        module_ast = ast.parse(parsed_module.code)
        relevant_types: TypeNameToASTNode = ASTModule.relevant_types(module_ast)
        types: Iterable[TypeVariableWithAnnotations] = [
            ASTType.to_type_variables_with_annotations(type, parsed_module.import_path_components)
            for type in relevant_types.values()
        ]
        return types

    def from_parsed_modules(
        parsed_modules: Sequence[ParsedModule],
    ) -> Sequence[TypeVariableWithAnnotations]:
        types = []
        for parsed_module in parsed_modules:
            types.extend(ExtractTypes.from_parsed_module(parsed_module))
        return types


class ExtractImportStatements:

    def from_parsed_module(parsed_module: ParsedModule) -> Sequence[str]:
        module_ast = ast.parse(parsed_module.code)
        import_statements = []
        for node in ast.walk(module_ast):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    import_statements.append(f"import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module
                for alias in node.names:
                    # TODO: Test this, does it work for "from x import y as z"
                    # Does it work for "from x import a, b, c"?
                    import_statements.append(f"from {module} import {alias.name}")
        return import_statements


class ExtractClassCode:

    def from_parsed_module(
        parsed_module: ParsedModule,
        class_name: str,
    ) -> str:
        module_ast = ast.parse(parsed_module.code)
        class_declarations = ASTModule.class_declarations(module_ast)
        class_def = class_declarations[class_name]
        return ast.unparse(class_def)
