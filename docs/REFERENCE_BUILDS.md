# Reference builds

This document records source builds that must be preserved as behavioral references even when they are not the main production line.

## 1. Angel gap-preserving reference

**Source archive:** `ورژن اصلی با فاصله(2).zip`

**Internal lineage found in the archive:**
- README identifies the base as **BEGIN 0004**
- `CLAUDE_0001_CHANGES.md` says it is based on BEGIN_0004, which itself was built on `version-grok-11`
- `eleven/__init__.py` identifies the build as `claude_0001_fshc_gxth`

**Role in the project:**
- Golden/reference behavior for models with delicate separated geometry such as the Angel wings.
- Preserves useful spacing and visible form better than the more general back-attachment line on this class of model.
- Not considered a universal production base because it is less reliable across unrelated model classes.

**Important implementation characteristics from the archive:**
- hard panel clipping
- same-slice component consolidation
- configurable consolidation link distance
- bounded consolidation loop
- soft `NO SILHOUETTE DISTORTION` diagnostic
- local/support fallback chain before legacy flat-back closure
- tab highlighting in a dedicated DXF layer
- tab count constrained by real part size/contact opportunities

**Project classification:** `reference/angel-gap-preserving`

Status: **Reference behavior — must be regression-protected, not blindly promoted to production.**

---

## 2. General back-attachment baseline

**Source archive:** `ورژن پایه اتصال به بک(1).zip`

**Internal version:** **ELEVEN S1.1.5.0**

README description:
- four quality levels
- automatic slice count
- small islands in each slice are joined to the back rail so detached pieces are not left without attachment

**Role in the project:**
- General assembly baseline.
- Produces a mountable/resulting output for a wider range of STL files using its back-attachment logic.
- Known to contain bugs and may alter geometry more aggressively than desired.
- Performs particularly well on model classes similar to `pic of omen`.

**Project classification:** `baseline/general-back-attachment`

Status: **Broad-coverage baseline — useful for generality, but not accepted as final geometry policy.**

---

## Design rule derived from comparing these builds

The project must not choose one of these builds and discard the other.

The target architecture separates:

```
STL
  ↓
Preflight / mesh validation
  ↓
Common slicing core
  ↓
Island / topology classification
  ↓
Assembly strategy
  ├─ gap-preserving / local support
  └─ general back-attachment
  ↓
Validation
  ↓
DXF export
```

Visible slice geometry should remain primary. Assembly logic must not reshape the artistic contour unless a documented strategy explicitly requires it and validation reports the effect.

## Regression classes

### Angel-like
Protect:
- wing separation
- thin/open details
- local gaps
- no artificial full-height rear strips

### Pic-of-Omen-like
Protect:
- high contour quality
- stable general back attachment
- valid closed DXF output
- useful assembly result

### Difficult / unrelated STL
Protect:
- no crash
- no silent part loss
- deterministic topology
- graceful fallback when an attachment strategy cannot be applied
