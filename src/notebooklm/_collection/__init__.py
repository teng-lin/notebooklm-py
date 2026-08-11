"""Internal helpers for NotebookLM collections (account-level notebook groups).

A *collection* is, on the wire, a source-``Label`` of type ``3`` with **no
notebook parent** — it groups whole notebooks (account-level) rather than
sources within one notebook. Collections therefore reuse the four label RPCs
verbatim (``LIST_LABELS`` / ``CREATE_LABEL`` / ``UPDATE_LABEL`` / ``DELETE_LABEL``)
with three wire differences captured in :mod:`notebooklm._collection.params`.
"""
