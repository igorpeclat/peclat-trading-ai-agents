# Backtests Cleanup Report

## Scope

- Target path: `src/data/rbi/**/backtests_final/*.py`
- Goal: remove non-Python AI preambles/code-fence wrappers so files are executable when strategy logic is valid.

## Actions Performed

- Stripped markdown fence lines starting with ```.
- Removed leading prose before first code section (first import/class/def/docstring marker).
- Kept strategy code body unchanged after the detected code start.

## Current Status

- Total files scanned: `1277`
- Parse-valid after cleanup: `484`
- Still invalid after cleanup: `793`
  - `SyntaxError`: `737`
  - `IndentationError`: `56`

## Notes

- Remaining invalid files are not only preamble issues; they include broken code structures (unterminated strings/f-strings, unmatched brackets, incomplete blocks).
- These require per-file repair or regeneration; automatic header cleanup cannot safely infer missing logic.
