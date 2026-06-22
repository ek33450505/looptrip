"""looptrip — deterministic detection of multi-agent coordination pathologies.

A framework-agnostic, zero-LLM, stdlib-only detector that trips at iteration 2
(the second occurrence of a duplicate-work signature) rather than on the invoice.
It observes existing OTel GenAI spans and cast.db agent runs; it never gates.
"""

__version__ = "0.1.1"
