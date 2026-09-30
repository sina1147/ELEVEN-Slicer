# Known issues

## Needs verification
- Orientation-dependent island topology changes in later S1 branches.
- Interaction between island merge/unification logic and selected back-face/orientation.
- Full end-to-end regression on representative real STL files.
- Back-panel/assembly logic must not reshape visible slice geometry unless explicitly intended.
- Version strings, UI titles and ports need consistency checks across branches.
- Performance changes must be checked against contour fidelity and final physical part count.

## Regression evidence already identified
A later regression analysis identified `unify_islands_on_back`, `attach_near`, and later `outer_only/sanitize` behavior as important topology-risk areas. These changes are not assumed fixed in this initial import.
