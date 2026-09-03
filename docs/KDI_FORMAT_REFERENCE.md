# SQEX_KineDriverData (KDI) Format Reference

Consolidated reference on Square Enix's KineDriver procedural-rig format, as it
appears in FModel JSON exports of Final Fantasy VII Rebirth character assets
(`*_KDI.json`, class `SQEX_KineDriverData`). Compiled from direct analysis of
one specimen file in depth (`PC0004_00_KDI.json`, Red XIII's Standard
costume) cross-checked against a census of 58 exported `*_KDI.json` assets
across multiple characters, and verified against Square Enix's own design
talk: Ryûsuke Sasaki & Keita Takagi, **"次世代を見据えた新しい補助骨システムの開発"**
("Developing a New Helper-Bone System with the Next Generation in Mind"),
CEDEC 2019, <https://www.jp.square-enix.com/tech/library/pdf/CEDEC2019_KineDriver.pdf>
(90 slides, cited below as "CEDEC 2019" with slide numbers).

Everything marked **[verified]** was checked either against the CEDEC talk
directly or by independent numerical round-trip testing (shown inline).
Everything marked **[inferred]** is a confident reading of the data that has
not been independently confirmed. Everything marked **[open]** is a real,
unresolved gap.

---

## 1. What KineDriver is

KineDriver is Square Enix's in-house "helper bone" system: a node graph that
reads the current pose of one or more animated ("main") bones, runs that
through a small set of math/curve nodes, and writes the result into other
("helper"/"补助") bones' transforms — driving jiggle-free secondary motion
(hair sway, shoulder/scapula correction, hip squash on leg bend, etc.)
without requiring hand-keyed animation. It has shipped since 2009 (Softimage
v1) through a 2019 "v3" rewrite that introduced the free node-graph
architecture documented here. It runs both in the DCC tool (Maya) for
authoring and in-engine at runtime (CEDEC 2019 slides 8–13, 40–50).

A `*_KDI.json` file, as exported by FModel from a cooked `SQEX_KineDriverData`
UAsset, is the **compiled runtime graph**: nodes in guaranteed evaluation
order, with all inter-node wiring resolved to explicit connections (CEDEC
2019 slide 38: "評価順にデータ出力 / ランタイムはこの順番に評価" — "output in
evaluation order; the runtime evaluates in this order"). It is not the Maya
authoring graph (which uses named DependencyNodes); it is the baked/flattened
form.

A character's KDI asset is referenced from the SkeletalMesh's `AssetUserData`
block, alongside its Bonamik (jiggle-physics) asset:

```json
"SQEX_KBD_AssetUserData": {
  "BonamikDataList":     [ { "ObjectPath": ".../PC0004_00_BNM.0" } ],
  "KineDriverDataList":  [ { "ObjectPath": ".../PC0004_00_KDI.0" } ]
}
```

**Runtime placement** [verified, CEDEC 2019 slides 44, 50]: in the UE4/5
plugin, a `KineDriverComponent` sits as a child of the `SkeletalMeshComponent`
and rewrites the mesh's bone transforms every Tick, strictly **after** the
base animation pose is evaluated. Execution order relative to Bonamik is
explicit — either via UE's `AddTickPrerequisiteComponent`, or via a
project-specific merged Kine+Bona component built for performance. Confirmed
independently in the data: Bonamik's kinematic collision anchor
`Bdy_C_NeckAKdi_0_1` is parented directly to the KDI-driven bone
`C_NeckAKdi` — Bonamik's hair/cloth simulation is reading a bone that only
exists post-KineDriver, proving the ordering rather than merely asserting it.

---

## 2. Top-level file structure

```json
[
  {
    "Type": "SQEX_KineDriverData",
    "Name": "PC0004_00_KDI",
    "Properties": {
      "WorkNum": 29,
      "Operators": [ ... ],
      "SourceTranslateBody": [ ... ],
      "SourceRotateBody": [ ... ],
      "TargetTranslateBody": [ ... ],
      "TargetScaleBody": [ ... ],
      "TargetBendSTRollBody": [ ... ],
      "TargetPoscnsBody": [ ... ],
      "TargetOricnsBody": [ ... ],
      "EffectorEZParamLinkBody": [ ... ],
      "EffectorEZParamLinkLinearBody": [ ... ],
      "ConnectionBody": [ ... ]
    }
  }
]
```

The file is always a one-element array wrapping a single
`SQEX_KineDriverData` object. Not every body array appears in every file —
only the ones actually used. The corpus (§8) shows several more body arrays
that exist in the schema but weren't present in the specimen file:
`TargetBendRollBody`, `TargetDircnsBody`, `TargetRotateBody`,
`EffectorExprBody`, `EffectorInverseBody`.

### 2.1 `Operators` — the instruction list

A flat array of `{WorkIndex, OpType, OperatorBody, Label}` entries. Each
entry is one node in the graph:

```json
{ "WorkIndex": 7, "OpType": "ESQEX_KD_OperatorType_SourceRotate", "OperatorBody": 0, "Label": "None" }
```

- **`OpType`** — the node's kind, prefixed `ESQEX_KD_OperatorType_`. See §3
  for the full type catalog.
- **`OperatorBody`** — index into the *body array matching this OpType*
  (e.g. `OpType=SourceRotate` → look up `SourceRotateBody[OperatorBody]`).
  Body arrays are indexed independently per type, not globally.
- **`WorkIndex`** — an integer ≥0 for "real" output-producing nodes
  (Source/Target nodes), or **`-1`** for pure plumbing nodes (Connection,
  Effector). `WorkIndex` values are dense and increasing but are **not** the
  same as the node's position in the `Operators` array — they index into a
  separate `WorkNum`-sized scratch buffer the runtime uses to cache
  intermediate Source/Target values between passes. `WorkNum` at the top
  level is simply `max(WorkIndex) + 1`.
- **`Label`** — always seen as `"None"` in every sampled file; presumably an
  authoring-time display label that never got populated, or is stripped on
  cook. Not useful.

**Evaluation order** is simply array order — walk `Operators` front to back;
each node's inputs are guaranteed to already be resolved (CEDEC 2019 slide
38). Nodes are typically emitted in tight source→effector→target runs, but
a target's needed value can be produced by operators emitted many entries
earlier (e.g. constraint nodes referencing a source bone whose own
Source-type operator appears near the start of the file). Don't assume
locality; assume only that array order is a valid topological order.

### 2.2 `ConnectionBody` — the wiring, with a duplication quirk

Each entry describes one point-to-point wire:

```json
{
  "ConnectionType": "ESQEX_KD_ConnectionType::ESQEX_KD_ConnectionType_Float",
  "InPortInfo":  { "NodeName": "C_HeadAKdi.C_HeadAKdi_KDSrcR", "OperatorIndex": 7,  "ParameterType": "ESQEX_KD_ParameterType_BendS", "MultiIndex": 0 },
  "OutPortInfo": { "NodeName": "op_eff_....", "OperatorIndex": 9, "ParameterType": "ESQEX_KD_ParameterType_Input", "MultiIndex": 0 },
  "OtherSourceParamIndex": 0, "OtherTargetParamIndex": 0,
  "Coef": 1.0
}
```

- `InPortInfo`/`OutPortInfo.OperatorIndex` are indices into the **`Operators`
  array** (not into a body array) — i.e. "which node".
- `ParameterType` names the specific channel on that node
  (`BendS`/`BendT`/`Roll`/`TranslateX..Z`/`ScaleX..Z`/`Input`/`Output`/
  `RotateQuat`, etc.).
- **`Coef`** — a scalar multiplier applied on that specific wire. See §5.2:
  this is Maya's automatically-inserted `UnitConversion` node, baked into the
  edge. In the specimen file every `Coef` is `1.0`; across the corpus, 53/58
  files use a non-unit `Coef` on at least one connection, and `57.29578`
  (=180/π) / `0.0174533` (=π/180) dominate — i.e. **most connections in most
  files are not `Coef=1.0`**; don't hard-code that assumption.
- **Duplication quirk** [verified]: `ConnectionBody` in the specimen file has
  156 entries, but only the ones **actually referenced by a `Connection`-type
  `Operators` entry** (via `Operators[i].OperatorBody`) are "live" — 60 of
  them, each appearing 2–3× at different array indices with byte-identical
  content. The remaining 96 are dead duplicates. **Always resolve live
  connections through `Operators`, never by iterating `ConnectionBody`
  directly** — that also naturally gives you evaluation order for free.

### 2.3 How to walk the graph (reference algorithm)

```python
# 1. Only Connection-type Operators entries point at *live* ConnectionBody rows.
live = [ConnectionBody[op.OperatorBody] for op in Operators if op.OpType.endswith('_Connection')]

# 2. Build effector-centric adjacency from those live connections.
incoming = {}  # effector_operator_index -> (producer_operator_index, producer_param, coef)
outgoing = {}  # effector_operator_index -> [(consumer_operator_index, consumer_param, coef), ...]
for c in live:
    if c.OutPortInfo.ParameterType == 'Input':
        incoming[c.OutPortInfo.OperatorIndex] = (c.InPortInfo.OperatorIndex, c.InPortInfo.ParameterType, c.Coef)
    if c.InPortInfo.ParameterType == 'Output':
        outgoing.setdefault(c.InPortInfo.OperatorIndex, []).append(
            (c.OutPortInfo.OperatorIndex, c.OutPortInfo.ParameterType, c.Coef))

# 3. Every Effector-type operator with both an incoming producer and >=1 outgoing
#    consumer is one "driver link": producer_channel -> curve -> consumer_channel.
```

This reconstructs the full source→effector→target chain generically, for
any KDI file, regardless of graph size or shape. Verified against the
specimen file: reproduces all 30 driver links with zero unresolved nodes.

---

## 3. Node type catalog

Every `OpType` seen (specimen file marked ✅; corpus-only marked with
occurrence stats from §8; CEDEC-only — mentioned in the design talk but never
observed in the exported corpus — marked 📖).

| OpType | Role | Seen |
|---|---|---|
| `SourceTranslate` | Read a bone's local translation as scalar channels | ✅ |
| `SourceRotate` | Read a bone's local rotation, decomposed via Bend/Roll or Expmap | ✅ |
| `TargetTranslate` | Write a bone's local translation | ✅ |
| `TargetScale` | Write a bone's local scale | ✅ |
| `TargetBendSTRoll` | Write a bone's local rotation via Bend/Roll recomposition | ✅ |
| `TargetBendRoll` | A related but distinct 2-channel rotation write (`SourceQuat`/`QuatWeight`/`AsQuatAngle` fields) — different decomposition path | corpus: 50/58 files, 421 uses |
| `TargetPoscns` | Position **constraint** — "maintain offset" copy from 1..n weighted sources | ✅ |
| `TargetOricns` | Orientation **constraint** — same, for rotation | ✅ |
| `TargetDircns` | Aim/"Direction" constraint — 2-point (aim only) or 3-point (aim+up, adds twist) | corpus: 22/58, 66 uses |
| `TargetRotate` | Direct rotation write, bypassing Bend/Roll entirely | corpus: 29/58, 57 uses |
| `EffectorEZParamLink` | Two-segment cubic Bézier curve ("簡易ベジェ補間" / simple Bézier interpolation) | ✅ |
| `EffectorEZParamLinkLinear` | Linear scale+offset with optional clamp | ✅ |
| `EffectorExpr` | Stack-VM expression (arbitrary math) | corpus: 23/58, 308 uses |
| `EffectorInverse` | Unknown — body is `{}` (empty object) in the one instance seen | corpus: 1/58, 2 uses — **[open]** |
| `EffectorLinkWith` | "Driven key" (single-key interpolation) | 📖 CEDEC slide 28 only — never seen in any exported file |
| `EffectorRBFInterp` | Multi-dimensional driven key (Radial Basis Function interpolation) | 📖 CEDEC slides 28, 56–66 only — never seen exported |
| `Connection` | Pure wiring node (see §2.2) | ✅ |

**Why LinkWith/RBFInterp never appear exported** [open]: either they get
baked down into `EZParamLink`/`Expr` at cook time, or none of the 58 sampled
character files happen to use them. Not resolved.

### 3.1 `SourceTranslateBody` / `SourceRotateBody` — reading a driver bone

```json
// SourceTranslateBody[0]
{
  "SourceBoneNameArray": ["L_UpperArmKdi"], "WeightArray": [1.0],
  "BaseSpaceInfo": { "BaseSpaceType": "...PARENT", "BoneName": "None" },
  "NeutralTranslate": { "X": 11.00766, "Y": -1.7e-13, "Z": 9.2e-14 },
  "NeutralRotate": { "X": -0.0, "Y": 0.0, "Z": 0.0, "W": 1.0, ... }
}
```

- **`SourceBoneNameArray` + `WeightArray`** — supports blending multiple
  source bones (weighted average); corpus shows 42/58 files with genuine
  multi-source use (769 occurrences) via this exact mechanism, most likely
  from the auto helper-bone generator (§5.6) or from **mesh-attach**
  constraints (§5.5). The specimen file uses single-source everywhere
  (weight 1.0).
- **`BaseSpaceInfo`** — normally `PARENT` (evaluate relative to the bone's
  own parent, Blender's native convention). Corpus shows 47/58 files also
  use `NODE` (base space is an arbitrary *named* bone, not the parent) —
  344 occurrences. Changes what "neutral" and offsets are expressed
  relative to.
- **`NeutralTranslate`/`NeutralRotate`** — the neutral transform removed from
  the Source node's sampled transform. For a single PARENT-space source this
  is normally redundant with Blender's already-rest-relative pose channels.
  For a multi-source node it is **not redundant**: the sources are blended as
  current transforms first, then the node's one authored neutral is applied to
  that combined transform. `PC0012_00` demonstrates this directly: the inverse
  of the three spine bones' averaged bind orientation relative to `C_Hip_a`
  matches its SourceRotate `NeutralRotate` after the KDI intermediate-frame
  conversion described in §5.4.
- `SourceRotateBody` additionally carries `AimVector`/`UpVector`/
  `CrossVector` (always `(1,0,0)`/`(0,1,0)`/`(0,0,1)` in the specimen file —
  the reference axes for the Bend/Roll decomposition, §5.1),
  `SegmentScaleCompensate` (Maya-vs-UE4 scale semantics, §5.3),
  `ReverseOrder` (bend-then-twist vs twist-then-bend order, always `false`
  in the specimen), `MirrorParams` (unused, all-zero — corpus shows 2/58
  files use `EnableMirroring`, 32 occurrences, semantics unverified), and
  `BoundExpmapAngles` (**[open]** — likely relates to how the stereographic
  swing singularity at 180° is handled, but not confirmed).

### 3.2 `TargetTranslateBody` / `TargetScaleBody` / `TargetBendSTRollBody` — writing a driven bone

Same `NeutralTranslate`/`NeutralRotate` scaffolding as above (redundant for a
Blender target for the same reason). `TargetScaleBody`'s `ScaleX/Y/Z`
default to `1.0` — this is Blender's own multiplicative-scale-from-1.0
convention already, so a driven `ScaleX` value writes straight into
`pose_bone.scale.x` with no transformation needed.

`TargetBendSTRollBody` is one **node per bone**, with up to three
independently-drivable scalar inputs — `BendS`, `BendT`, `Roll` — that get
jointly recomposed into one final local rotation (§5.1). In the specimen
file, a given bone's node is never driven on all three unless it's a hip
(`L_Hip_Spo`/`R_Hip_Spo` get all three; most hair-tuft bones get only one or
two, with the rest implicitly `0.0`).

**Cloud rotation targets [verified in Blender]**: `TargetBendRoll` optionally
receives a `SourceRotate.BendingQuat`, interpolates identity-to-bend by
`QuatWeight`, and composes the scalar `Roll` about `AimVector`; with
`ReverseOrder=false`, the result is `bend @ twist`. `TargetRotate` performs
the corresponding identity-to-full-`RotateQuat` interpolation and bypasses
Bend/Roll decomposition. On `PC0000_00`, this produces the authored 50% knee
bend (`L/R_Knee_Spo`) and 60% elbow rotation (`L/R_Elbow_Spo`) exactly.
`NeutralRotate` remains rest-pose scaffolding and is not multiplied into
Blender's already-rest-relative pose quaternion. All 48 Cloud rotation
targets evaluate to identity at neutral after import.

### 3.3 `TargetPoscnsBody` / `TargetOricnsBody` — maintain-offset constraints

```json
// TargetPoscnsBody[0]
{
  "TargetObjectBoneName": "C_SpineDKdi",
  "OrientAffect": false, "ScaleAffect": false,
  "OffsetTranslate": { "X": -8.85e-14, "Y": 69.68951, "Z": 40.450104 },
  "SourceBoneNameArray": ["C_Spine_d"], "WeightArray": [1.0],
  "OffsetArray": [ { "X": 8.85e-14, "Y": -69.68951, "Z": -40.450104 } ],
  "TargetSegmentScaleCompensate": true, "IgnoreTSSC": false
}
```

**Purpose** [verified]: continuously make the target bone's *world*
transform track the source bone's world transform, preserving whatever
fixed offset existed between them **at bind time** — i.e. exactly a
"maintain offset" parent constraint, used here to snap a bone in KineDriver's
own parallel hierarchy (parented under a synthetic `C_KDIRoot`) onto the
motion of a bone in the real skeleton hierarchy, without merging the two
hierarchies.

- `OffsetTranslate` + `OffsetArray[0]` are (as far as tested) exact negatives
  of each other and, in the specimen file, they net to the target and
  source sharing **identical rest-pose world positions** — i.e. this
  specific Poscns usage is a pure snap, not a spatial offset. `TargetOricns`
  is *not* a pure identity — target and source rest orientations differ by a
  genuine, non-trivial rotation (their local "bone axis" conventions
  differ), which the offset fields correct for.
- `OrientAffect`/`ScaleAffect` — both `false` everywhere sampled; presumably
  toggle whether the position constraint also inherits rotation/scale
  effects from the source. Unexercised, so semantics for `true` are
  **[open]**.
- **Mesh-attach** [verified, CEDEC 2019 slide 31]: when
  `SourceBoneNameArray`/`WeightArray` describe more than one weighted
  source, the weights are computed **automatically from the mesh's skin
  weights at the constrained bone's attach point** rather than hand-set —
  described in the talk as commonly used to build Bonamik's simulation guide
  bones. This is the mechanism behind the corpus's 769 multi-source
  constraint instances; the specimen file uses none (all its `_Spo` bones
  snap to a single named bone instead of a mesh region).
- `TargetDircns` (aim constraint, corpus-only) additionally carries separate
  aim-bone / up-bone / source-offset fields; 2-point variant has no up
  vector (pure "point at"), 3-point variant adds one (also constrains
  twist). CEDEC 2019 slide 29.

### 3.4 `EffectorEZParamLinkBody` — the curve

```json
{
  "PX0": -1.5707964, "VX1_0": 1.5707964, "VX2_1": 1.5707964,
  "Grad0": 0.0, "Grad1": 0.0,
  "PY0": 0.0, "PY0A": 0.0, "PY0B": 0.0,
  "PY1": 0.0, "PY1A": 0.0, "PY1B": 0.34906587, "PY2": 0.34906587,
  "ByCoef": false
}
```

Two cubic Bézier segments over `[x0,x1]` and `[x1,x2]`, plus linear
extrapolation outside the domain:

```
x0 = PX0            x1 = x0 + VX1_0            x2 = x1 + VX2_1

x <= x0:  y = PY0 + Grad0*(x - x0)                          # left extrapolation
x0<x<=x1: y = Bezier(PY0, PY0A, PY0B, PY1;  t=(x-x0)/VX1_0)  # segment 0
x1<x<=x2: y = Bezier(PY1, PY1A, PY1B, PY2;  t=(x-x1)/VX2_1)  # segment 1
x >= x2:  y = PY2 + Grad1*(x - x2)                           # right extrapolation

Bezier(p0,p1,p2,p3; t) = (1-t)^3*p0 + 3(1-t)^2*t*p1 + 3(1-t)*t^2*p2 + t^3*p3
```

`Grad0`/`Grad1` **[inferred, not directly proven]** — read as the
extrapolation slopes past the curve's domain edges, consistent with every
sampled curve, but no ground truth exists past the domain edge to confirm
against. `ByCoef` — always `false`; unexercised, semantics **[open]**.

Values are in the *effector's own edited units* — usually radians for
angle-domain curves (verified: multiple curve domains are exact multiples of
π/2), but the CEDEC talk (slide 35–36) explicitly states effector nodes
are edited in whatever unit the artist prefers ("Unitless" internally, with a
Maya `UnitConversion` node auto-inserted on connections crossing a
unit-typed boundary) — this is exactly what `Coef` (§2.2, §5.2) captures.
Don't assume radians; check `Coef` on the connection feeding the curve.

### 3.5 `EffectorEZParamLinkLinearBody` — the simple case

```json
{ "Scale": 0.25, "Offset": 0.0, "ClampMin": -0.17453294, "ClampMax": 0.17453294, "EnableMin": false, "EnableMax": false }
```

`y = x*Scale + Offset`, then clamped to `[ClampMin, ClampMax]` only if the
corresponding `Enable*` flag is true.

### 3.6 `EffectorExprBody` — the bytecode VM (corpus only, not in specimen)

```json
{ "Code": "var 0;var 1;op add;", "Inputs": [ /* driver-variable-like refs */ ] }
```

**[verified, CEDEC 2019 slides 51–54]**: this is Maya's general expression
system, compiled at export time. Pipeline: artist writes
`OBJ2.tx = (Obj1.tx + 3) * 2` → resolved to internal variable references
`(x0 + 3) * 2` → tokenized → shunting-yard → postfix/RPN `x0 3 + 2 *` →
serialized as mnemonic opcodes for the KDI file: `var 0; push 3; op add;
push 2; op mul;`. `var N` pushes input N (`Inputs[N]`), `push V` pushes a
float literal, `op OP` pops and combines. Grammar: one expression, four
arithmetic operators + parens, builtin functions (`sin`, `log`, `max`, …),
3D vectors and quaternions are first-class values, **no loops or
branches**. The full builtin-function set is **[open]** — not enumerated
anywhere in the sampled corpus or the talk.

### 3.7 `EffectorInverseBody` — undocumented (corpus only)

Exactly one instance across the corpus; body is a literal empty JSON object
`{}`. Not named among the CEDEC talk's five official effector types
(`EZParamLinkLinear`, `EZParamLink`, `LinkWith`, `RBFInterp`, `Expr`) — a
2019-talk-postdating UE4-plugin-specific addition, presumably (name
suggests) negating or reciprocal-transforming its input, but **entirely
unverified — [open]**.

---

## 4. Bones as nodes: the two parallel hierarchies

A KDI-equipped character skeleton contains, alongside its normal animated
bones, a second parallel chain of purely-procedural bones recognizable by
naming suffix:

- **`*Kdi`** bones — parented under a synthetic `C_KDIRoot`, entirely
  separate from the real skeleton hierarchy (e.g.
  `C_SpineDKdi < C_KDIRoot < Trans`, vs. the real `C_Spine_d < C_Spine_c <
  ... < C_Hip_a < Trans`). These exist purely to be driven — snapped onto
  real-skeleton motion via `TargetPoscns`/`TargetOricns` (§3.3), then used
  as **sources** for further downstream driver chains (hair tufts, scapula
  correction, etc).
- **`*_Spo`** bones — live *inside* the real hierarchy (parented to an
  actual animated ancestor, e.g. `C_HairD_Spo < C_Neck_b < ...`), but are
  likewise non-animated and purely driven. CEDEC 2019 slide 31 identifies
  these as commonly built via mesh-attach and used as **Bonamik's guide
  bones** — confirmed independently: Bonamik's kinematic anchor bodies
  parent onto exactly these bones (and onto `*Kdi` bones — see §1).
- **`*_Phy`** bones — Bonamik/ragdoll physics bones, a third, unrelated
  category (see §7).

Neither `*Kdi` nor `*_Spo` bones carry any user animation; every channel on
them is either constant (rest pose) or driven. This is a clean, reliable way
to partition a skeleton into "animated" vs "procedural" for import purposes.

---

## 5. The math, in full

### 5.1 Bend & Roll — the swing/twist rotation decomposition

**[verified — published formula, CEDEC 2019 slides 20–26, closed-form,
independently round-trip-tested]**

KineDriver represents a bone's rotation two ways depending on context:

- **Bend & Roll** ("曲げとひねり") — the default, used for hand-authored
  drivers. Bend = the shortest rotation pointing the bone's local aim axis
  at the target direction; Roll = whatever rotation is left over after
  Bend is removed. Bend itself splits into two scalar angles via
  **stereographic projection** onto the bone's local up/cross axes.
  Composition order (bend-then-twist vs twist-then-bend) is a real,
  per-body-part authored choice — the `ReverseOrder` flag (always `false`
  in the specimen file).
- **Expmap** (quaternion exponential/logarithmic map) — used exclusively by
  the auto helper-bone generator (§5.6), specifically *because* it has no
  such ordering ambiguity and doesn't depend on a "bone direction" concept —
  useful for mechanically-derived (not hand-authored) rotation control.

Formula, given the bone's local reference axes `vx` (aim), `vy` (up), `vz`
(cross) — normally `(1,0,0)`, `(0,1,0)`, `(0,0,1)` — and `vx'` = the aim axis
after the driving rotation `q` is applied (`vx' = q @ vx`):

```
Decompose (rotation -> two angles):
  denom   = vx·vx' + 1
  theta_h = -2 * atan2(vz·vx', denom)      # BendT in the Blender importer
  theta_v =  2 * atan2(vy·vx', denom)      # BendS in the Blender importer

Recompose (two angles -> rotation), exact CEDEC closed form:
  s   = 2 / ( tan(-theta_h/2)^2 + tan(theta_v/2)^2 + 1 )
  vx' = (s-1)*vx + s*tan(theta_v/2)*vy + s*tan(-theta_h/2)*vz
  swing = shortest-arc rotation from vx to vx'      # e.g. Vector.rotation_difference in mathutils
  Roll  = residual twist about vx: twist = swing^-1 * q ; angle = signed_angle(twist, about vx)
  full_rotation = swing * Quaternion(axis=vx, angle=Roll)
```

For a target operator, the serialized `AimVector`/`UpVector`/`CrossVector`
triplet is the authored orthonormal reference frame. Its columns are transposed
(equivalently inverted) before recomposition so the reference-space BendS/T
components become target-local components. Using the stored columns directly is
indistinguishable for cardinal frames, nearly plausible for small rotations,
and wrong for strongly oblique frames such as `PC0012_00` Jacket C/E.

Numerically verified (pure-Python reimplementation, no mathutils
dependency): `decompose` then `recompose` round-trips a random quaternion to
`<5e-13` error across 2000 samples, confirming the atan2-based decomposition
and the CEDEC-published recomposition formula are mutually exact inverses.

**BendS vs BendT axis assignment [empirically resolved for this Blender
pipeline]**: CEDEC slide 23's diagram pairs the two bend channels with the
stereographic axes but does not literally label which is S and which is T.
Testing Cloud's actual graph resolves the assignment: pants helpers are wired
from `L_Foreleg_a.BendT`, and anatomical knee flexion on the imported bone's
local Y axis produces the cross-axis formula above. Therefore this importer
uses `BendT = theta_h` and `BendS = theta_v`. The previous candidate labeling
had these two names reversed.

### 5.2 `Coef` — baked unit conversion, not artistic tuning

**[verified, CEDEC 2019 slides 35–36, plus a live corpus example]**. Source/
Target node attributes are typed (`Angle`, `Distance`) and always computed
internally in fixed units (**radians**, **centimeters**); Effector node
attributes are `Unitless`. Whenever a connection crosses that boundary, Maya
auto-inserts a `UnitConversion` node showing the artist their preferred
display unit (e.g. degrees) while storing radians underneath — and that
conversion factor is exactly what survives into the exported connection's
`Coef`. Confirmed with a real example from the corpus: a `SourceRotate.Roll`
output (radians) feeding an effector edited in degrees carries
`Coef: 57.29578` — precisely 180/π. Any importer that ignores `Coef` will
work by coincidence on files that happen to use `Coef=1.0` everywhere (like
the specimen file) and silently produce rigs off by a factor of ~57 on the
53/58 corpus files that don't.

### 5.3 `SegmentScaleCompensate` — a Maya/UE4 impedance-mismatch flag, not physics

**[verified, CEDEC 2019 slide 47]**. Maya's segment-scale compensation
means a bone's scale does **not** propagate to its children by default;
UE4's hierarchical bone scaling means it **does**. KineDriver's
`TargetScaleOp` explicitly corrects for this: in the UE4 plugin, on the
*children* of whatever bone a scale-driver targets, segment-scale
compensation is applied so the net visual result matches what the same rig
would do in Maya. This flag is meaningless in Blender, which behaves like
Maya here (no default scale propagation quirk to correct for) — safe to
ignore entirely for a Blender target.

### 5.4 Units and coordinate/axis conventions

**[verified]** internal units are radians and centimeters, matching UE's
native centimeter convention exactly — no import-scale correction needed
beyond whatever unit setting the destination DCC tool uses.

**Axis convention going into Blender** — do **not** re-derive this from the
KDI file's own internal reference-vector values in isolation; use whatever
UE→Blender convention your own pipeline has already established and
verified for actor/mesh import, and apply the *vector* half of it (not the
quaternion half) to KineDriver's `AimVector`/`UpVector`/`CrossVector`, since
those are pure 3-component vectors with no `W`/handedness component of
their own. For this project specifically, the established, working
convention is:

```
Position (UE -> Blender): negate Y only         — (X, -Y, Z)
Rotation (UE -> Blender): mirror similarity     — Quaternion(W, -X, Y, -Z)
```

Since `AimVector` etc. are vectors (no `W`), the relevant half is the position
one: negate Y, leave X and Z alone. The package-skeleton coordinate profile
then accounts separately for its intentional Blender display-bone roll.

### 5.5 Mesh-attach constraints

See §3.3. Worth restating as its own concept: a `TargetPoscns`/`TargetOricns`
node with more than one weighted source is (per CEDEC 2019 slide 31) very
likely the output of a semi-automatic tool that samples skin weights at a
bone's attachment point on the mesh surface and turns them directly into
constraint weights, rather than a hand-picked single parent. This explains
why the corpus shows heavy multi-source usage (769 instances, 42/58 files)
concentrated in exactly the kind of guide-bone role Bonamik needs, while a
hand-tuned rig like the specimen file uses single-source snapping
throughout.

### 5.6 Automatic helper-bone generation (design-time tool, not runtime data)

**[verified, CEDEC 2019 slides 68–86]** — background context for *why* a
given KDI file's driver graph looks the way it does, not something present
in the runtime data itself. A separate authoring tool: given sculpted
example poses (wrinkles, muscle bulges, pose-space deformation targets), it
runs **Smooth Skinning Decomposition with Rigid Bones (SSDR)** [Le & Deng
2012] to place new helper bones and assign skin weights, then fits a
**Thin-Plate-Spline RBF interpolation** to drive each new bone from 1–2
parent/driver bones — always via **Expmap** rotation (never Bend/Roll),
translate raw, scale logarithmic. Process: skin-weight optimization →
iteratively insert bones at max-error vertices → optimize bone transforms →
decide each new bone's parent (and separately its 1–2 RBF drivers, which
need not be the same bones) → reduce RBF keys down from "cover everything"
to a minimal set. This pipeline is the most likely origin of most driver
instances in the wider corpus (the 769 multi-source constraints and 308
`EffectorExpr` uses look like its output); a hand-tuned file like the
specimen (single-source, Bend/Roll only, no expressions) was evidently
authored by hand instead.

---

## 6. Curve/effector node catalog quick reference

| Field group | Meaning |
|---|---|
| `PX0, VX1_0, VX2_1` | Domain: `x0=PX0`, segment widths to `x1`, `x2` |
| `PY0, PY0A, PY0B, PY1` | Segment 0 Bézier control points |
| `PY1, PY1A, PY1B, PY2` | Segment 1 Bézier control points (shares `PY1`) |
| `Grad0, Grad1` | Extrapolation slopes outside `[x0,x2]` **[inferred]** |
| `Scale, Offset` | Linear effector: `y = x*Scale + Offset` |
| `ClampMin/Max, EnableMin/Max` | Optional linear-effector output clamp |

---

## 7. The companion asset ecosystem

A character's KDI file never stands alone; these sibling assets sit
alongside it in the export and matter for a full rig-transfer pipeline:

| Asset | Class | Relevance |
|---|---|---|
| `*_BNM.json` | `SQEX_BonamikAsset` | Jiggle-physics (hair/cloth) sim. Reads KDI-driven bones as kinematic collision anchors — see §1. Not itself portable to another engine; the anchor dependency is a hard evaluation-order constraint. |
| `*_PhysicsAsset.json` | `PhysicsAsset` | Standard UE ragdoll collision. Unrelated to rigging despite sharing bone names with KDI's *sources* (real skeleton bones) — easy to confuse with rig data at a glance. |
| `*_CapsuleShadow.json` | `PhysicsAsset` (cut-down) | Coarse capsule proxy, almost certainly for blob/capsule shadow casting. Same caveat as above. |
| `*_Condition.json` | `SkeletalMesh` | A second, complete mesh for a story/battle-damage state swap. Shares the same skeleton and (implicitly) the same KDI rig — the swap is material/mesh-only, not a re-rig. |
| `Emissive/*.json` | `EndEmissiveColorSettings` | Glow color/intensity (e.g. tail flame), shader-side only. |
| `*_vfx.json` | `EffectAppendixMesh` | The one companion file with **real geometry** — e.g. Red XIII's tail-flame trail: a ribbon mesh swept along a spline over time, its own skin weights, driven by a separate VFX system. Don't confuse its skin weights with the character mesh's (still generally unavailable in these JSON exports — no character mesh geometry ships in this export family at all). |
| `ControlRig/*_BCR.json` | `EndBodyControlRig` | Full-body IK config (foot/hand effectors + forward axis). For a quadruped, front paws double as "feet" alongside real feet. |
| `ControlRig/*_Rig.json` | `ControlRigBlueprintGeneratedClass` | The actual compiled Control Rig — a `RigVM` bytecode graph, UE's own node-based rig system. Large (character-dependent, several hundred KB), **unrelated to KineDriver's node model** beyond both ultimately writing bone transforms. Decoding its bytecode is a separate reverse-engineering project on the scale of this one — not attempted here. |

---

## 8. Corpus census — feature prevalence across 58 exported `*_KDI.json` files

A single specimen file (however clean) is a poor basis for generalizing an
importer. Census below shows what a schema-complete reader actually needs to
handle; "files"/"uses" both count only *live* (Operators-referenced)
occurrences.

| Feature | Files | Uses | Note |
|---|---:|---:|---|
| `Coef != 1.0` on a connection | 53/58 | 15,716 | The single most dangerous thing to skip — see §5.2 |
| `TargetBendRoll` node | 50/58 | 421 | Distinct 2-channel rotation-write path, different fields than `TargetBendSTRoll` |
| `BaseSpaceType_NODE` | 47/58 | 344 | Base space is a named bone, not the parent |
| Quaternion-typed connections (`RotateQuat`, `Coef=0.0`) | 43/58 | 132 | Whole-rotation routing, not scalar |
| Multi-source weighted constraints | 42/58 | 769 | Mesh-attach — see §5.5 |
| `TargetRotate` | 29/58 | 57 | Direct rotation write, bypasses Bend/Roll |
| `EffectorExpr` | 23/58 | 308 | Bytecode VM — see §3.6 |
| `TargetDircns` | 22/58 | 66 | Aim constraint — see §3.3 |
| `EnableMirroring` | 2/58 | 32 | Semantics unverified — **[open]** |
| `EffectorInverse` | 1/58 | 2 | Undocumented, empty body — **[open]** |

**Practical implication**: the specimen file (Red XIII, Standard costume) is
an unusually clean example — every optional flag at default, every weight
1.0, every `Coef` 1.0, single source per constraint, no expressions. Good
for *learning* the format; a poor basis for *validating* an importer against.
Build against it, then validate against files that actually exercise
expressions, quaternion routing, and non-unit coefficients (e.g. Cloud's or
Aerith's KDI assets in the same corpus).

---

## 9. Practical lessons for implementing a Blender importer

These came from an actual attempt to rebuild the specimen file's driver
graph as Blender constraints + drivers on an already-imported armature.
Recorded here because each was a real, non-obvious failure mode.

### 9.1 `TargetPoscns`/`TargetOricns` are *not* a Child Of constraint

Child Of re-parents a bone: `final = target_matrix @ inverse_matrix @
local_before`, where `local_before` already includes whatever the bone's
*actual* armature parent contributes. That's fine only if the parent is
static. It is not, in general — e.g. the specimen file's
`C_SpineDKdi -> C_NeckAKdi -> C_NeckBKdi -> C_HeadAKdi` chain is bones each
parented to the *previous* one, all of them independently `TargetOricns`-driven.
Child Of's composition order doesn't cancel that out: the real parent's
motion and the constraint's motion both land in the result, compounding
rather than replacing.

**Fix**: compute explicitly, as a driver (not a constraint):
`world = source_world(t) @ K`, where `K` is the fixed source-local rest-pose
offset between source and target (`K = source_rest_world⁻¹ @
target_rest_world`, computed once from rest data), then convert that
desired world value into the bone's own local channel against its *actual
current* parent (read live, so a multi-level chain resolves correctly
regardless of depth). This was verified by a from-scratch numeric simulation
of a 3-level chain (armature → driven bone P → driven bone T, T's real
parent = P) before touching the real script: the naive version was off by
~1 radian / ~78 units; the corrected version round-trips to `<1e-13` across
500 random trials. Two subtle bugs were found and fixed in that process:

- When a driven bone has **no bone-parent** (an armature root), its
  "rest relative to parent" is its own rest transform, not identity — and
  its live "parent" reading should be the **armature object's own**
  transform (via a TRANSFORMS driver variable with an empty `bone_target`),
  not a hardcoded identity.
- `pose_bone.location` is expressed in the bone's own **rest-relative-to-
  parent rotated axes**, not the parent's raw axes — the position offset
  must be un-rotated by rest_local's own rotation before subtracting, not
  just subtracted directly.

### 9.2 `PoseBone.matrix` driver paths use reversed nested indices

Python reads a matrix component as `pose_bone.matrix[row][column]`, but an
RNA data path used by a driver variable must spell the same semantic component
as `pose.bones[...].matrix[column][row]`. Treating the data-path indices as
row-major silently transposes every matrix supplied to the driver. This was
the cause of two apparently unrelated axis failures in Cloud's rig:
`L_Foreleg_a` Z rotation made the `L_StdCalf*_Spo` chain rotate on X, and
`C_Spine_d` X/Y motion twisted the shirt collar. Reading the matrix with
reversed data-path indices makes the supplied values exactly match the live
`mathutils.Matrix`; both chains then preserve their expected parent-relative
orientation on all tested axes. Translation is affected too: semantic
`matrix[row][3]` must be requested as `matrix[3][row]` in a driver path.

### 9.3 Depsgraph evaluation order on first setup

Right after adding drivers via the Python API, Blender doesn't necessarily
do a full, correctly-ordered re-evaluation until something forces one. A
chained bone (§9.1) can briefly read its parent's **stale pre-driver**
transform on the very first evaluation, landing on a visibly wrong pose
that free-corrects itself only on the next frame change or redraw. Fix:
call `bpy.context.view_layer.update()` explicitly after building the
drivers, before handing control back — forces one clean, fully-ordered pass.

### 9.4 `ChannelDriver.expression` has a hard 256-character limit

Blender silently truncates a driver's stored expression string past 256
characters — no error at write time, just a corrupted expression that fails
at evaluation with a misleading `SyntaxError: '(' was never closed`.
Embedding a curve's dozen full-precision float literals directly in the
expression text (e.g. `kdi_ez(x, -1.5707964, 1.5707964, ..., 0.34906587,
0.0, 0.0)`), or worse, nesting two such calls for a Bend+Roll recomposition,
blows past this trivially. **Fix**: never put more than a couple of literal
numbers in a driver expression. Store curve/constant parameters in a plain
Python dict at script-run time, keyed by a short integer id; register one
small dispatch function per math primitive in `bpy.app.driver_namespace`
that looks the id up; the expression itself only ever carries that id plus
short driver-variable names, e.g. `kdi_curve(10, kdi_theta_v(L10w,L10x,
L10y,L10z))` instead of a 150+-character literal-laden call. Verified
worst-case expression length in the specimen file (a 3-channel hip
recomposition, nesting three curve calls) is 165 characters after this
fix — comfortable margin.

### 9.5 `bpy.app.driver_namespace` state is session-only

Both the registered functions *and* any backing dict of constants (§9.4)
live only in the current Python session's memory — they are not saved into
the `.blend` file. Reopening the file requires re-running the setup script
before any driver will evaluate correctly again. Not a bug, just a real
operational constraint worth documenting for whoever runs the importer.

### 9.6 Bone-selection scripting APIs are not stable across Blender versions

`Bone.select` (long-standing, used for e.g. programmatically driving
`bpy.ops.constraint.childof_set_inverse`) was removed as of the Blender 5.x
line. Any importer design that depends on setting an "active bone" via
selection state to drive an operator is fragile across versions for this
reason alone — independent of whether the operator's *result* would have
been correct. Prefer computing the needed result directly from stable,
foundational data-model APIs (`PoseBone.matrix`, `Bone.matrix_local`,
`Constraint.mute`) over operator calls that need UI-adjacent context state.

---

## 10. Summary of open questions

| Gap | Status |
|---|---|
| BendS/BendT naming | Resolved empirically for this Blender pipeline in §5.1; CEDEC does not literally label S/T |
| `Grad0`/`Grad1` exact extrapolation semantics | Consistent inference, never proven past domain edge |
| `EffectorInverse` semantics | Single corpus instance, empty body, no documentation |
| `EffectorLinkWith` / `EffectorRBFInterp` — why never exported | Named in CEDEC 2019 but absent from all 58 sampled files |
| `EnableMirroring`/`MirrorParams` semantics | 2/58 files use it; never independently exercised |
| `TargetPoscns`/`TargetOricns`'s `OrientAffect`/`ScaleAffect = true` behavior | Always `false` in every sampled file |
| `ByCoef` flag on `EffectorEZParamLinkBody` | Always `false`; unexercised |
| `EffectorExpr` builtin function set | Grammar is known (§3.6); the actual function list is not enumerated anywhere seen |
| Character mesh skin weights | Not present in this JSON export family at all — no `.psk`/mesh-geometry export sampled alongside these KDI files |
| Exact KineDriver placement relative to IK/retargeting (beyond "after the base pose") | CEDEC talk confirms post-pose, pre/post-IK specifically not stated |

---

## Appendix: specimen file facts (Red XIII, Standard costume, `PC0004_00_KDI.json`)

For calibration — these are the concrete numbers behind every "specimen
file" reference above, useful for sanity-checking a from-scratch
reimplementation against the same file.

- 193 KB, 5928 lines, `WorkNum: 29`, 137 `Operators` entries.
- Op types used: 10 of the 15 known (`SourceTranslate`, `SourceRotate`,
  `TargetTranslate`, `TargetScale`, `TargetBendSTRoll`, `TargetPoscns`,
  `TargetOricns`, `EffectorEZParamLink`, `EffectorEZParamLinkLinear`,
  `Connection`). Missing: `TargetBendRoll`, `TargetDircns`, `TargetRotate`,
  `EffectorExpr`, `EffectorInverse`.
- Body counts: `SourceTranslateBody`=2, `SourceRotateBody`=7,
  `TargetTranslateBody`=6, `TargetScaleBody`=2, `TargetBendSTRollBody`=12,
  `TargetPoscnsBody`=10, `TargetOricnsBody`=8, `EffectorEZParamLinkBody`=24,
  `EffectorEZParamLinkLinearBody`=6, `ConnectionBody`=156 (60 live, 96 dead
  duplicates — see §2.2).
- 34 unique bones touched total; 10 maintain-offset constraints
  (`TargetPoscns`/`TargetOricns` merged by target bone); 30 scalar driver
  links (source→effector→target chains, §2.3); 12 `TargetBendSTRoll` target
  nodes; 10 `TargetTranslate`/`TargetScale` simple targets.
- The 10 constraints: `C_SpineDKdi<-C_Spine_d`, `C_NeckAKdi<-C_Neck_a`,
  `C_NeckBKdi<-C_Neck_b`, `C_HeadAKdi<-C_Head_a` (all loc+rot, forming the
  spine→neck→neck→head chain from §9.1); `C_HairD_Spo<-C_HairDCKdi`,
  `C_HairE_Spo<-C_HairECKdi`, `C_HairF_Spo<-C_HairFCKdi`,
  `C_HairG_Spo<-C_HairGCKdi` (loc+rot); `L_UpperArmKdi<-L_UpperArm_a`,
  `R_UpperArmKdi<-R_UpperArm_a` (location only, no orientation constraint on
  these two).
- Driver links cover: 3 hair-tuft chains off `C_HeadAKdi`/`C_NeckAKdi`/
  `C_NeckBKdi` rotation (BendS/BendT/Roll → various `C_Hair*Kdi` bend/
  translate targets), 2 shoulder→scapula chains (`L`/`R_Shoulder_a` rotation
  and `L`/`R_UpperArmKdi` translation → `L`/`R_Scapula_Spo`), and 2
  full-featured leg→hip chains (`L`/`R_UpperLeg_a` rotation, all three of
  BendS/BendT/Roll, → `L`/`R_Hip_Spo` bend/scale/translate — the only bones
  in this file where all three `TargetBendSTRoll` channels are driven at
  once).
