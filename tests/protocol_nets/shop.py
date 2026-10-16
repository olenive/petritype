"""The shared conformance net ``shop`` (PROTOCOL.md §11): the net of
``examples/edges_on_empty_places/shop.py``, deterministic as it stands, carried alone by
``python -m petritype.runner --module tests.protocol_nets.shop``."""

from examples.edges_on_empty_places.shop import shop  # noqa: F401  (the @petri_net factory)
