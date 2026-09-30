# Regression test matrix

| Model class | Main purpose | Must preserve | Main failure to detect |
|---|---|---|---|
| Angel-like | Delicate separated geometry | wings, gaps, fine silhouette | artificial bridges / flattened rear profile |
| Pic-of-Omen-like | High-quality regular relief | contour fidelity + mountable result | contour simplification or bad rear attachment |
| Multi-island | Topology stability | correct island count and attachment decision | orientation-dependent fusion |
| Complex organic | Generality/performance | no crash, stable output | timeout, pathological consolidation |
| Invalid/problematic STL | Preflight | early useful diagnosis/repair | expensive late-stage failure |
| Orientation matrix | Direction invariance | equivalent topology where physically equivalent | back-face-dependent topology changes |

## Status terminology

- **PASS** — verified with actual output
- **FAIL** — reproduced defect
- **NEEDS VERIFICATION** — code/report exists but no adequate end-to-end evidence
- **REFERENCE PASS** — a historical build is intentionally retained because it produced a known-good result for that behavior class
