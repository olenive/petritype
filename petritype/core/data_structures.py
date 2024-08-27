import ast
from pydantic import BaseModel, ValidationError
from pydantic.functional_validators import AfterValidator

from typing import Annotated, Any, Optional, Sequence
from enum import Enum


COMMON_TYPES = {
    # Python built-in types.
    "str", "int", "float", "bool", "None", "set", "list", "dict", "tuple",

    # Types of types.
    "Optional", "Union", "Any", "Sequence", "Iterable", "Callable", "Type",

    # Datetime types.
    "datetime", "timedelta",

    # Pydantic types.
    "BaseModel",

    # Numpy types.
    "ndarray", "np.ndarray", "numpy.ndarray"
}


type ModuleName = str
type ClassName = str  # What about classes that are types?
type VariableName = str
type FunctionName = str  # or method
type FunctionFullName = str
type TypeName = str
type ArgumentName = str
type ReturnIndex = int
type TypeAnnotation = str
type ASTNodeForType = ast.ClassDef | ast.AnnAssign | ast.TypeAlias
type TypeNameToASTNode = dict[TypeName, ASTNodeForType]
type ArgumentTypes = dict[VariableName, Optional[TypeAnnotation]]
type ReturnType = Optional[TypeAnnotation]

type PlaceNodeName = str
type TransitionNodeName = str
type KwArgs = dict[str, Any]


class ImportPath(BaseModel):
    module_path_components: Sequence[ModuleName]
    class_name: Optional[ClassName]
    function_name: Optional[FunctionName]


# This can be dataclass with fields or a pydantic model with fields or simply a type alias.
class TypeVariableWithAnnotations(BaseModel):
    name: TypeName
    # TODO: Figure out how to ensure that type_name tokens are unique.
    # TODO: Figure out how to determine type_name from type annotation.
    parent_type: Optional[TypeName]
    subtypes: set[TypeName]  # This is used for type aliases.
    attribute_types: ArgumentTypes  # This is used for classes and pydantic models.
    import_path: ImportPath
    code: str


class FunctionWithAnnotations(BaseModel):
    function_full_name: FunctionFullName  # For now this can be ClassName.function_name
    # TODO: Figure out how to ensure that function_full_name tokens are unique.
    class_name: Optional[ClassName]
    function_name: FunctionName
    argument_types: ArgumentTypes
    return_type: ReturnType
    import_path: ImportPath
    code: str


class TypeRelationship(Enum):

    # For attributes.
    CONTAINS_AS_SUBTYPE = "contains as subtype"
    CONTAINS_AS_ATTRIBUTE_TYPE = "contains as attribute type"
    CONTAINS_AS_ATTRIBUTE_SUBTYPE = "contains as attribute subtype"
    CONTAINED_IN_ATTRIBUTE_TYPE = "in attribute type"
    CONTAINED_IN_ATTRIBUTE_SUBTYPE = "in attribute subtype"
    CHILD_OF = "child of"
    PARENT_OF = "parent of"
    ANNOTATED_AS = "annotated as"

    # For functions.
    TAKES_AS_EXACT_TYPE_OF_ARGUMENT = "takes exact type as argument"
    TAKES_AS_SUBTYPE_OF_ARGUMENT = "takes subtype of argument"
    RETURNS_EXACTLY_THIS_TYPE = "returns exactly this type"
    RETURN_CONTAINS_THIS_AS_SUBTYPE = "return contains this as subtype"


# Types for mapping graph data to specific IDs used by a graph library.
def ensure_index_is_non_negative(value: int) -> ValidationError:
    if value < 0:
        raise ValueError("The index value must be non-negative.")


type NodeIndex = Annotated[int, AfterValidator(ensure_index_is_non_negative)]
type NodeIndicesFromTo = tuple[NodeIndex, NodeIndex]
