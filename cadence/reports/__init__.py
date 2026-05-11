"""CADENCE report rendering (WP-11).

Three entry points exported here:

* :func:`cadence.reports.summary.render_summary` — Rich-formatted CLI report
  covering all three gaps, inter-build intervals, and the major slicing
  dimensions.
* :func:`cadence.reports.markdown.render_markdown` — comprehensive
  GitHub-renderable Markdown report that references the PNG charts.
* :func:`cadence.reports.charts.render_all` — emits the eight publication
  charts the spec calls out, each as a 300-DPI PNG (matplotlib) and an
  interactive HTML (plotly).
"""
