"""Repro for python-tabulate issue #354.

`maxcolwidths` adds extra padding: the rendered column is wider than the
requested max width because the wrapping width math double-counts the cell
padding. With maxcolwidths=8 the third column should wrap text into a content
field exactly 8 characters wide, but the buggy code renders it 10 wide.

Public API only: tabulate.tabulate + stdlib + pytest.
"""

from tabulate import tabulate


def test_maxcolwidths_does_not_add_extra_padding():
    headers = ["Header#1", "Header#2", "Header#3"]
    data = [
        [
            "Alpha beta gama zeta omega",
            "The weather was exceptionally good that day again",
            "The files were concatenated and archived for posterity.",
        ],
        [
            "Delta omega beta alpha nu and the rest",
            "The weather was exceptionally good that day",
            "They decided to engage in many businesses and all of them "
            "were successful.",
        ],
    ]

    max_width = 8
    out = tabulate(
        data,
        headers=headers,
        tablefmt="fancy_grid",
        maxcolwidths=[None, None, max_width],
    )

    lines = out.splitlines()

    # Collect the third-column content field from every line that has the
    # vertical box-drawing separators (header + data rows). fancy_grid uses
    # one space of cell padding on each side of the content, so stripping a
    # single leading/trailing space yields the true content-field width.
    content_widths = []
    for line in lines:
        if "│" not in line:  # skip rule/border lines (use other glyphs)
            continue
        cells = line.split("│")
        # cells[0] and cells[-1] are the empty strings outside the border;
        # the three data columns are cells[1], cells[2], cells[3].
        assert len(cells) == 5, (len(cells), repr(line))
        third = cells[3]
        # remove exactly the single-space padding on each side
        assert third.startswith(" ") and third.endswith(" "), repr(third)
        field = third[1:-1]
        content_widths.append(len(field))

    # The rendered content field for the capped column must be exactly the
    # requested max width -- not max_width + 2 as the padding-double-count bug
    # produces.
    assert content_widths, "no content rows found"
    assert max(content_widths) == max_width, (
        "maxcolwidths column rendered wider than requested: "
        f"got {max(content_widths)}, expected {max_width}; widths={content_widths}"
    )
