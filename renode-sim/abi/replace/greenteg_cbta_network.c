/* The greenTEG core-body-temperature network, in C.
 *
 * 0x8c000 lays five tensor descriptors on its stack and calls a stack of
 * generic kernels -- nn_gru_run, nn_gru_cell, nn_dense_layer,
 * nn_dense_accumulate and the three activations -- to run one GRU timestep and
 * an 8-to-1 output layer. Everything that generality buys is unused here: the
 * sequence is one timestep, the batch is one row, the direction is forwards
 * and the output activation is the identity. What is left when those are
 * folded away is this file, and abi/greenteg.py is the same arithmetic in
 * Python against the same declared tensors.
 *
 * Float32 is the contract, not an implementation detail, so the operations are
 * written one at a time:
 *
 *   the accumulate is `vfma.f32` in the image (0xa7610), so the product is
 *   never rounded before it is added -- __builtin_fmaf rather than `acc + w *
 *   x`, and a builtin rather than a call because the relink compiles with
 *   -fno-builtin and the image has no fmaf body to call;
 *
 *   the bias goes on after the whole sum (0xa762a) and not into the
 *   accumulator;
 *
 *   the two gate sums are (x-part) + (h-part) (0xa780a) and the candidate is
 *   (x-part) + r * (h-part) (0xa7876, 0xa789a), each its own rounding;
 *
 *   the state update is fmaf(z, h, (1 - z) * n) (0xa78c4): the (1 - z) * n
 *   product is rounded and the z * h one is not.
 *
 * Contraction is turned off for the file rather than left to the relink's
 * flags, because the one multiply that feeds an addition here is the
 * candidate's `x + r * h` and the image rounds that product (`vmul` at 0xa7876,
 * `vadd` at 0xa789a). At the relink's -std=gnu99 the default is
 * -ffp-contract=fast, which folds the pair into a `vfma` and moves four of the
 * run's 151 inferences by one ulp.
 *
 * expf and expm1f are the image's own bodies at 0x8c9dc and 0x8cacc, which are
 * what nn_activation_logistic and nn_activation_elu call; the relink resolves
 * both names to those unless REPLACE=libm takes them from libm.a, in which case
 * the kernels this replaces would have moved with them.
 *
 * The GOT at 0x20007974 and the three activation pointer words at 0x200074dc
 * stay in the image, and this body names neither: the position-independent
 * addressing was the vendor code's way of reaching its own data, and C names
 * greenteg_cbta_nn_tensors directly. Nothing executes the three activations
 * once the runner is C -- the runner was their only reader -- but they are
 * still reached, so gc keeps them: the trio is one live `.data` item, the rest
 * of the drop reaches its own statics through the same GOT, and the reach map
 * has three unclassified words that may name kernels of this subtree (0xd2b08
 * -> nn_gru_cell, 0xd01d8 -> nn_gru_run, 0xc65a4 -> nn_activation_logistic), so
 * a GC=1 link KEEPs those two and everything they call. What it does drop today
 * is the runner and nn_dense_layer; the other five wait on those words being
 * settled rather than on anything this file does. */

#include "replace.h"
#include "withings/vendor.h"

#include <math.h>

#pragma GCC optimize("fp-contract=off")

/* Update, reset, candidate, in the order the weight tensors stack their
   blocks: 0xa7702 divides the input weight's row count by this and walks the
   blocks in turn, so a gate's weights are contiguous. */
#define GRU_GATES 3

enum gru_gate {
    GRU_GATE_Z = 0,
    GRU_GATE_R = 1,
    GRU_GATE_N = 2
};

/* Which half of the split bias vector a gate's projection takes. The tensor
   holds six unit-wide vectors: the three the input projections add, then the
   three the recurrent ones do, which is PyTorch's split bias and Keras'
   reset_after layout. */
enum gru_bias_half {
    GRU_BIAS_INPUT = 0,
    GRU_BIAS_RECURRENT = 1
};

/* 0xa759e for one row: out[n] = bias[n] + sum over k of in[k] * w[k * cols +
   n]. The kernel's batch loop is not here because every call the model makes
   passes one row -- the input tensor is 1 x 3 and the hidden state is one
   eight-float vector. */
static void dense(float *out, const float *in, const float *weights,
                  const float *bias, unsigned int rows, unsigned int cols)
{
    for (unsigned int n = 0; n < cols; n++) {
        float acc = 0.0f;
        for (unsigned int k = 0; k < rows; k++)
            acc = __builtin_fmaf(in[k], weights[k * cols + n], acc);
        out[n] = bias[n] + acc;
    }
}

/* 0xa7542 and 0xa7576, the two activations the cell is handed. */
static float logistic(float x)
{
    return 1.0f / (expf(-x) + 1.0f);
}

static float elu(float x)
{
    return x > 0.0f ? x : expm1f(x);
}

static const float *gate_block(const struct greenteg_nn_tensor *weights,
                               enum gru_gate gate, unsigned int rows)
{
    return weights->data + (unsigned int)gate * rows * weights->cols;
}

static const float *gate_bias(const struct greenteg_nn_tensor *bias,
                              enum gru_bias_half half, enum gru_gate gate,
                              unsigned int units)
{
    return bias->data + ((unsigned int)half * GRU_GATES + (unsigned int)gate)
                        * units;
}

/* 0xa76ea, one timestep, in place over the caller's hidden state. */
static void gru_step(float *hidden, const float *x,
                     const struct greenteg_nn_tensor *bias,
                     const struct greenteg_nn_tensor *weight_ih,
                     const struct greenteg_nn_tensor *weight_hh)
{
    const unsigned int units = weight_hh->cols;
    const unsigned int inputs = weight_ih->rows / GRU_GATES;
    /* Six unit-wide projections, which is the workspace the cell is handed at
       0x8c072 as well; sized off the tensors rather than off a constant so the
       declaration stays the only place the model's shape is written down. */
    float from_input[GRU_GATES][units];
    float from_hidden[GRU_GATES][units];
    float update[units], reset[units], candidate[units];

    for (enum gru_gate g = GRU_GATE_Z; g < GRU_GATES; g++) {
        dense(from_input[g], x, gate_block(weight_ih, g, inputs),
              gate_bias(bias, GRU_BIAS_INPUT, g, units), inputs, units);
        dense(from_hidden[g], hidden, gate_block(weight_hh, g, units),
              gate_bias(bias, GRU_BIAS_RECURRENT, g, units), units, units);
    }
    for (unsigned int i = 0; i < units; i++)
        update[i] = logistic(from_input[GRU_GATE_Z][i]
                             + from_hidden[GRU_GATE_Z][i]);
    for (unsigned int i = 0; i < units; i++)
        reset[i] = logistic(from_input[GRU_GATE_R][i]
                            + from_hidden[GRU_GATE_R][i]);
    /* The reset gate multiplies the recurrent candidate after its bias, not
       before: 0xa782c accumulates with bias[5] and 0xa7876 scales the result. */
    for (unsigned int i = 0; i < units; i++)
        candidate[i] = elu(from_input[GRU_GATE_N][i]
                           + reset[i] * from_hidden[GRU_GATE_N][i]);
    for (unsigned int i = 0; i < units; i++)
        hidden[i] = __builtin_fmaf(update[i], hidden[i],
                                   (1.0f - update[i]) * candidate[i]);
}

float greenteg_cbta_network_run(float *hidden, const float *inputs)
{
    const struct greenteg_nn_tensor *tensors = greenteg_cbta_nn_tensors;
    const struct greenteg_nn_tensor *out_bias = &tensors[0];
    const struct greenteg_nn_tensor *out_weight = &tensors[1];
    float output;

    gru_step(hidden, inputs, &tensors[2], &tensors[3], &tensors[4]);
    /* The output layer, with the identity for its activation: 0xa76e8 tail
       calls nn_activation_identity, which is `bx lr`. */
    dense(&output, hidden, out_weight->data, out_bias->data,
          out_weight->rows, out_weight->cols);
    return output;
}
