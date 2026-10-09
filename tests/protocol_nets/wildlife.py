"""The shared conformance net ``wildlife`` (PROTOCOL.md §11, §13): the net of
``examples/wildlife.py``, deterministic as it stands, carried alone by
``python -m petritype.runner --module tests.protocol_nets.wildlife``."""

from examples.wildlife import wildlife  # noqa: F401  (the @petri_net factory)
