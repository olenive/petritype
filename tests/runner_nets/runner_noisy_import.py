"""Prints at import time, through ``print`` and straight to descriptor 1; both
must land on the runner's stderr, never on the protocol stream."""

import os

from petritype import petri_net
from runner_process_nets import greeter

print("print at import")
os.write(1, b"descriptor one at import\n")


@petri_net(name="noisy")
def noisy():
    return greeter()
