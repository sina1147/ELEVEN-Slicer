# ELEVEN Slicer migration report

Initial repository reconstruction source: connected Google Drive.

## Imported source line
The imported tree is the Drive source whose README identifies it as **ELEVEN Version S1.1.10.0**, built on **S1.1.9.0** and adding optional parametric wave panels.

This repository was not reconstructed by inventing historical Git commits from ZIP timestamps.

## Important caution
S1.1.9.0 is the documented baseline in the project's baseline-analysis material, but the exact Drive tree imported here is S1.1.10.0. Therefore this initial repository state should be treated as a recovered working source snapshot, not proof that every behavior is production-verified.

## Migration rules
- Original Drive files were not deleted or overwritten.
- __pycache__, generated DXFs, STL assets, ZIP archives, caches and temporary files were not imported.
- Historical branches such as C1, BAC, NOPANEL and other S1 variants should be imported only after content comparison and regression review.
