"""``petritype.core.data_structures``: the node index types, which keep a value and
refuse a negative one, and the members of ``TypeRelationship``."""

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from petritype.core.data_structures import (
    NodeIndex,
    NodeIndicesFromTo,
    TypeRelationship,
)


class TestNodeIndex:

    def test_node_index_validator_preserves_value_and_rejects_negative(self):
        adapter = TypeAdapter(NodeIndex)
        assert adapter.validate_python(0) == 0
        assert adapter.validate_python(42) == 42
        with pytest.raises(ValidationError):
            adapter.validate_python(-1)

        class Edge(BaseModel):
            source: NodeIndex
            target: NodeIndex

        edge = Edge(source=0, target=3)
        assert edge.source == 0
        assert edge.target == 3
        with pytest.raises(ValidationError):
            Edge(source=-1, target=3)

    def test_node_indices_from_to_validates_pair(self):
        adapter = TypeAdapter(NodeIndicesFromTo)
        assert adapter.validate_python((0, 5)) == (0, 5)
        with pytest.raises(ValidationError):
            adapter.validate_python((0, -2))


class TestTypeRelationship:

    def test_contained_in_attribute_type_enum_member_exists(self):
        assert TypeRelationship.CONTAINED_IN_ATTRIBUTE_TYPE.value == "in attribute type"
