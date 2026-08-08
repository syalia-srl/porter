"""The seam between `porter.types` and Task 7's `porter.spec`.

One assertion, and it exists because a comment could not make it fail. Task 4
put the dataclasses in `types.py` so Task 7 would *add* a validator rather than
move a definition `assemble` already imports, and said so in three docstrings.
Nothing failed if Task 7 wrote its own `Component` instead, and the divergence
had already begun: the brief imports `porter.spec`, the tests import
`porter.types`.
"""
import porter.spec
import porter.types


def test_spec_re_exports_the_dataclasses_assemble_consumes():
    """Identity, not equality. Two dataclasses with identical fields compare
    happily and are still two schemas: an `isinstance` check against one fails
    for the other, and the gallery has two definitions to drift between. `is`
    is the only assertion that catches a duplicate."""
    assert porter.spec.Component is porter.types.Component
    assert porter.spec.Python is porter.types.Python
