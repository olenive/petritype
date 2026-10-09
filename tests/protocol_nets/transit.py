"""The shared conformance net ``transit`` (PROTOCOL.md §11, §12): the city of
``examples/transit.py``, whose vehicles hold cabins that are nets of their own.
The example is deterministic (fixed seed, sequential firing, no clock), so the
example itself is the conformance net; this module exists so that
``python -m petritype.runner --module tests.protocol_nets.transit`` carries it
alone, as the transcript harness requires, with its cabin templates registered
by the import."""

from examples.transit import transit  # noqa: F401  (the @petri_net factory; templates register on import)
