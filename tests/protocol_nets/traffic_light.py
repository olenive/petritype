"""The shared conformance net ``traffic-light`` (PROTOCOL.md §11): the net of
``examples/edges_on_empty_places/traffic_light.py``, deterministic as it stands, carried alone
by ``python -m petritype.runner --module tests.protocol_nets.traffic_light``."""

from examples.edges_on_empty_places.traffic_light import traffic_light  # noqa: F401  (the @petri_net factory)
