#!/usr/bin/env python3
"""greenTEG's core-body-temperature network, read out of the image and run.

    python3 abi/greenteg.py                       # the model, as the image holds it
    python3 abi/greenteg.py --pairs out/gt.txt    # check it against a run's captures

What the free-living CBTA instance computes once a minute is a one-step GRU of
eight units over three normalised inputs, followed by a linear layer down to
one number, which is then an offset and a scale away from a temperature in
degrees Celsius. The weights are five tensors in flash; the descriptors that
say where they are and what shape they have are `greenteg_cbta_nn_tensors`,
declared in abi/include/withings/vendor.h and read here through that
declaration rather than off hardcoded offsets.

Where the arithmetic comes from, so that "reproduces the output" means the
bits and not the neighbourhood:

    the dense kernel 0xa759e accumulates with `vfma.f32`, a fused
    multiply-add: the product is not rounded before it is added. So a plain
    `acc + w * x` in float32 is the wrong operation, and `fma` below is exact
    -- the product of two float32s fits a float64 exactly, and the sum is
    taken as a Fraction and rounded once;

    the bias is added after the whole sum, not seeded into it (0xa762a);

    the GRU's two gate sums are formed as (x-part) + (h-part) elementwise
    (0xa780a) and the candidate as (x-part) + r * (h-part) (0xa789a), each a
    separate float32 rounding;

    the only place the rounding here can differ from the watch's is expf and
    expm1f: the image's newlib bodies at 0x8c9dc and 0x8cacc are not this
    host's, so the logistic and the ELU are computed in float64 and rounded
    to float32, which is correctly rounded and the watch's need not be.

Where the pairs come from. A scratch copy of abi/algos.py's `bodytemp`
scenario with two hooks instead of the tracepoint set:

    cpu AddHook 0x8c1c0   # r0 is the hidden state, sp+4 the three inputs
    cpu AddHook 0x8c09e   # r6 points at the one float the runner answers

both reading through cpu.GetMachine().SystemBus, one line of twelve hex words
per call. `greenteg test` alone gives 151 of them, one per minute of the 9120
samples it synthesises.
"""

import argparse
import os
import struct
import sys
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.dirname(HERE)

sys.path.insert(0, HERE)

import shapes                                                   # noqa: E402
import symbols as symmap                                        # noqa: E402

APP_BASE = 0x27000

# The instance state's fields, off 0x8c0cc and 0x8c15c. The network runs on the
# minute: 0x8c15c divides the sample counter by 60 and only a zero remainder
# reaches 0x8c1c0.
SAMPLES_PER_INFERENCE = 60

# The normalisation 0x8c16e applies to the three minute-means before the
# network sees them, and the affine 0x8c1c6 applies to the one number that
# comes back. The constants are the literals at 0x8c210..0x8c22c; the pairing
# of each with its input is which state field 0x8c16e loads it against.
NORMALISE = (("skin_temperature_c", 31.874719619750977, 2.415987491607666),
             ("heat_flux", 84.89434051513672, 42.59768295288086),
             ("heart_rate_bpm", 75.8488540649414, 21.506879806518555))
OUTPUT_OFFSET_C = 37.13923263549805
OUTPUT_SCALE_C = 0.5900092124938965

GATES = 3
GATE_Z, GATE_R, GATE_N = 0, 1, 2


def f32(x):
    """The float32 nearest `x`, which may be a float or a Fraction."""
    if isinstance(x, float):
        return struct.unpack("<f", struct.pack("<f", x))[0]
    lo = struct.unpack("<f", struct.pack("<f", float(x)))[0]
    # float(Fraction) rounds to float64 and packing rounds again, so the
    # double-rounded candidate is not always the nearest float32. Its two
    # neighbours bracket the true value; picking the nearest of the three by
    # exact comparison is the single rounding the hardware does.
    best = None
    for cand in (_next_f32(lo, -1), lo, _next_f32(lo, 1)):
        if cand != cand or cand in (float("inf"), float("-inf")):
            continue
        gap = abs(Fraction(cand) - x)
        if best is None or gap < best[0]:
            best = (gap, cand)
        elif gap == best[0] and _even_mantissa(cand):
            best = (gap, cand)
    return best[1]


def _next_f32(x, direction):
    bits = struct.unpack("<I", struct.pack("<f", x))[0]
    negative = bool(bits >> 31)
    step = direction if not negative else -direction
    if bits in (0x00000000, 0x80000000) and step < 0:
        bits = 0x80000000 if not negative else 0x00000000
        step = 1
    bits = (bits + step) & 0xFFFFFFFF
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def _even_mantissa(x):
    return not struct.unpack("<I", struct.pack("<f", x))[0] & 1


def fma(a, b, c):
    """float32 fused multiply-add: one rounding, as `vfma.f32` does it."""
    return f32(Fraction(a) * Fraction(b) + Fraction(c))


class Tensor(object):
    """One row of greenteg_cbta_nn_tensors, with its floats read out of flash."""

    def __init__(self, address, rank, count, rows, cols, data):
        self.address, self.rank = address, rank
        self.count, self.rows, self.cols = count, rows, cols
        self.data = data
        if count != len(data) or (rank == 2 and rows * cols != count):
            raise SystemExit("the tensor at 0x%x declares %d floats in %dx%d"
                             " and holds %d" % (address, count, rows, cols,
                                                len(data)))

    def at(self, row, col):
        return self.data[row * self.cols + col]

    def block(self, gate, rows):
        """Gate `gate`'s `rows` x cols slice of a stacked weight matrix.

        The cell kernel 0xa76ea divides its weight tensor's row count by three
        and walks the three blocks in turn, so a gate's weights are contiguous
        and not interleaved by row.
        """
        base = gate * rows * self.cols
        return self.data[base:base + rows * self.cols]

    def __repr__(self):
        return ("<tensor 0x%x rank %d %dx%d>"
                % (self.address, self.rank, self.rows, self.cols))


class Model(object):
    """The five tensors, named by the role the runner 0x8c000 gives each."""

    def __init__(self, tensors):
        if len(tensors) != 5:
            raise SystemExit("the model is five tensors, not %d" % len(tensors))
        self.out_bias, self.out_weight = tensors[0], tensors[1]
        self.gru_bias, self.weight_ih, self.weight_hh = tensors[2:]
        self.units = self.weight_hh.cols
        self.inputs = self.weight_ih.rows // GATES
        if (self.weight_hh.rows != GATES * self.units
                or self.weight_ih.cols != self.units
                or self.gru_bias.count != 2 * GATES * self.units
                or self.out_weight.rows != self.units
                or self.out_weight.cols != 1 or self.out_bias.count != 1):
            raise SystemExit("the five tensors do not make a %d-unit GRU over"
                             " %d inputs" % (self.units, self.inputs))

    def bias(self, half, gate):
        """b_ih (half 0) or b_hh (half 1) for one gate.

        0x8c0cc's cell hands the input projections the first three eight-float
        vectors and the recurrent ones the last three, which is PyTorch's
        split bias and Keras' reset_after layout.
        """
        base = (half * GATES + gate) * self.units
        return self.gru_bias.data[base:base + self.units]


def dense(inputs, weights, bias, rows, cols):
    """0xa759e for one row: out[n] = bias[n] + sum_k in[k] * w[k*cols + n].

    The accumulator starts at zero and the bias goes on at the end, which is
    the order the kernel writes: the `vfma` loop runs over k first and only
    then loads the bias vector and adds it.
    """
    out = []
    for n in range(cols):
        acc = 0.0
        for k in range(rows):
            acc = fma(inputs[k], weights[k * cols + n], acc)
        out.append(f32(Fraction(bias[n]) + Fraction(acc)))
    return out


def logistic(values):
    """0xa7542: 1 / (expf(-x) + 1), one element at a time."""
    import math
    return [f32(Fraction(1.0) / Fraction(f32(f32(math.exp(f32(-x))) + 1.0)))
            for x in values]


def elu(values):
    """0xa7576: x where x > 0, expm1f(x) where it is not."""
    import math
    return [x if x > 0.0 else f32(math.expm1(x)) for x in values]


def identity(values):
    return list(values)


def gru_step(model, x, h):
    """0xa76ea, one timestep. Returns the new hidden state.

    Gate order is z, r, n -- the update gate is the one blended against the
    old state at 0xa78ac and the reset gate is the one multiplying the
    recurrent candidate at 0xa7876 -- and the candidate's activation is the
    ELU where a stock GRU has tanh.
    """
    xs, hs = [], []
    for gate in range(GATES):
        xs.append(dense(x, model.weight_ih.block(gate, model.inputs),
                        model.bias(0, gate), model.inputs, model.units))
        hs.append(dense(h, model.weight_hh.block(gate, model.units),
                        model.bias(1, gate), model.units, model.units))
    z = logistic([f32(Fraction(a) + Fraction(b))
                  for a, b in zip(xs[GATE_Z], hs[GATE_Z])])
    r = logistic([f32(Fraction(a) + Fraction(b))
                  for a, b in zip(xs[GATE_R], hs[GATE_R])])
    gated = [f32(Fraction(ri) * Fraction(hi))
             for ri, hi in zip(r, hs[GATE_N])]
    n = elu([f32(Fraction(a) + Fraction(b))
             for a, b in zip(xs[GATE_N], gated)])
    # h' = (1 - z) * n + z * h, as the fma at 0xa78c4 forms it: the (1-z)*n
    # product is rounded, the z*h one is not.
    return [fma(zi, hi, f32(Fraction(f32(Fraction(1.0) - Fraction(zi)))
                            * Fraction(ni)))
            for zi, ni, hi in zip(z, n, h)]


def run(model, x, h):
    """0x8c000: one GRU step and the linear output layer. (output, new h)."""
    h = gru_step(model, x, h)
    out = identity(dense(h, model.out_weight.data, model.out_bias.data,
                         model.out_weight.rows, model.out_weight.cols))
    return out[0], h


def normalise(skin_temperature_c, heat_flux, heart_rate_bpm):
    """0x8c16e: each minute-mean less its centre, over its scale."""
    raw = (skin_temperature_c, heat_flux, heart_rate_bpm)
    return [f32(Fraction(f32(Fraction(v) - Fraction(centre))) / Fraction(scale))
            for v, (_, centre, scale) in zip(raw, NORMALISE)]


def temperature_c(output):
    """0x8c1c6: the network's one number as a core temperature."""
    return fma(output, OUTPUT_SCALE_C, OUTPUT_OFFSET_C)


def load(image=None, smap=None, types=None):
    """The model, read through the declaration in vendor.h."""
    image = image or os.path.join(SIM, "flash.bin")
    smap = smap or symmap.load()
    types = types or shapes.load()
    blob = open(image, "rb").read()
    table = None
    for candidate in types.tables(smap):
        if candidate.name == "greenteg_cbta_nn_tensors":
            table = candidate
    if table is None:
        raise SystemExit("greenteg_cbta_nn_tensors is not a declared table")
    row = table.row
    load_at = _load_address(table.address, smap)
    tensors = []
    for i in range(table.count):
        base = load_at + i * table.stride
        values = {}
        for field in row.fields:
            at = base + field.offset
            values[field.name] = int.from_bytes(blob[at:at + field.size],
                                                "little")
        data = values["data"]
        floats = list(struct.unpack_from("<%df" % values["count"], blob, data))
        tensors.append(Tensor(table.address + i * table.stride, values["rank"],
                              values["count"], values["rows"], values["cols"],
                              floats))
    return Model(tensors)


def _load_address(ram, smap):
    """Where a `.data` object's initialiser sits in flash.

    The startup at 0x36e1c copies 0xefe58 onwards to 0x200066a8, so the offset
    between the two is fixed and one subtraction answers it. The two ends are
    the literals at 0x36e38 and 0x36e40; they are read out of the image rather
    than written here so that a different build moves them together.
    """
    blob = open(os.path.join(SIM, "flash.bin"), "rb").read()
    dst = int.from_bytes(blob[0x36E3C:0x36E40], "little")
    src = int.from_bytes(blob[0x36E40:0x36E44], "little")
    end = int.from_bytes(blob[0x36E38:0x36E3C], "little")
    if not dst <= ram < end:
        raise SystemExit("0x%x is not in the .data image 0x%x..0x%x"
                         % (ram, dst, end))
    return src + (ram - dst)


def describe(model):
    lines = ["greenTEG CBTA free-living network, from %s" % SIM,
             "  GRU  %d inputs -> %d units, gates z/r/n, logistic gates,"
             " ELU candidate" % (model.inputs, model.units),
             "  dense %d -> 1, identity" % model.units,
             "  inputs (mean, scale): "
             + ", ".join("%s %.6f/%.6f" % n for n in NORMALISE),
             "  output: %.6f + %.6f * y  degC" % (OUTPUT_OFFSET_C,
                                                  OUTPUT_SCALE_C),
             "  tensors:"]
    for name in ("out_bias", "out_weight", "gru_bias", "weight_ih",
                 "weight_hh"):
        t = getattr(model, name)
        lines.append("    %-10s %r first %s"
                     % (name, t, ["%.6f" % v for v in t.data[:3]]))
    return "\n".join(lines)


def read_pairs(path):
    """The (input vector, hidden state, output) triples a run captured.

    One line per capture, all floats as the hex of their 32 bits, in the order
    the hooks wrote them: three inputs, eight hidden-state words as they were
    before the call, then the one output word.
    """
    out = []
    for line in open(path):
        parts = line.split()
        if len(parts) != 12 or parts[0].startswith("#"):
            continue
        words = [struct.unpack("<f", struct.pack("<I", int(p, 16)))[0]
                 for p in parts]
        out.append((words[:3], words[3:11], words[11]))
    return out


def check(model, pairs):
    worst, exact = 0.0, 0
    for x, h, want in pairs:
        got, _ = run(model, x, h)
        if struct.pack("<f", got) == struct.pack("<f", want):
            exact += 1
        worst = max(worst, abs(got - want))
    return exact, worst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=os.path.join(SIM, "flash.bin"))
    ap.add_argument("--pairs")
    args = ap.parse_args()
    model = load(args.image)
    print(describe(model))
    if not args.pairs:
        return
    pairs = read_pairs(args.pairs)
    if not pairs:
        raise SystemExit("%s holds no captures" % args.pairs)
    exact, worst = check(model, pairs)
    print("%d captured calls: %d bit-exact in float32, worst |error| %.3e"
          % (len(pairs), exact, worst))
    return sys.exit(0 if exact == len(pairs) else 1)


if __name__ == "__main__":
    main()
