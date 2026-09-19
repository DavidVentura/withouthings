/* HWA10 (ScanWatch 2) application firmware v3411: the vendor drops' ABI.
 *
 * What the libraries the firmware links but did not write take and return.
 * Which addresses belong to which component is abi/vendor.yaml and the
 * `component:` field of abi/symbols.yaml; this is the shapes, and only the
 * shapes the interface uses. What happens inside a drop is not declared here
 * and is not claimed anywhere: the interiors stay unnamed on purpose. */
#ifndef WITHINGS_VENDOR_H
#define WITHINGS_VENDOR_H

/* --------------------------------------------- greenTEG CBTA (greenteg_cbta)

   The core-body-temperature model. Two instances live in the library's own
   RAM -- the free-living one and the sport one -- and the firmware never
   holds a handle to either: every entry names its instance instead, which is
   why there are two of each call and no context argument anywhere. */

/* The calibration the firmware keeps for the sensor and hands to
   vendor_greenteg_cbta_sample_convert. The shell's `greenteg test` passes the
   sixteen bytes at 0xb410c, and the firmware's own copy is what "[GREENTEG]
   CBTA parameters: bin: '%c', skin temp. offset: %d(x1000), heatflux offset:
   %d(x100), integration factor: %d(x10000)" (0x4dafc) prints field for field,
   in that order. The scale factors in that line are the log's, not the
   struct's: the words here are floats and ones. */
struct greenteg_cbta_config {
    /* The sensitivity bin, one character. 0xb410c holds 'J'; the shell's
       `greenteg bin set <c>` writes it and 0x4d7a4 rejects anything outside
       the table with "[GREENTEG] Bad bin '%c'". Stored as a word. */
    unsigned int sensitivity_bin;
    float integration_factor;
    float heatflux_offset;
    float skin_temp_offset;
};

/* What a get call writes. The two byte fields are what "[BODY_TEMP][info]
   Hourly stats" (0x42fb0) prints as FL{T=%u, Q=%u} and WO{T=%u, Q=%u} beside
   the temperature, and what the shell prints as the "%d" of "fl: %d %lf".
   0x8bf94 writes exactly six bytes through its argument: the word, then the
   two bytes at +4 and +5. */
struct greenteg_cbta_result {
    float temperature_c;
    unsigned char quality;
    unsigned char state;
};

/* The two raw readings the watch takes, converted into the units the model
   wants. The conversion is the library's, not the firmware's: 0x8bd04 is
   handed the configuration and the two raw floats and writes these two. */
struct greenteg_cbta_sample {
    float heat_flux;
    float skin_temperature;
};

/* Both inits return zero on failure, which is what the firmware logs as
   "algorithm init failed"; both updates return the instance's ready flag. */
int vendor_greenteg_cbta_free_living_init(void);
int vendor_greenteg_cbta_sport_init(void);
void vendor_greenteg_cbta_free_living_reset(void);
void vendor_greenteg_cbta_sport_reset(void);

/* The configuration in, the model's two inputs out. What 0x8bd04 computes:
   the bin character is a sensitivity in microvolts per unit, (c - 'A' + 1)/10
   for a letter of either case and (c - 0xa4)/10 + 0.05 for the half-step
   range above 0xa4, so 'J' is 1.0; the heat flux is the raw bridge reading
   over that sensitivity, over the integration factor, less the heat-flux
   offset; and the skin temperature is the raw thermometer reading less the
   skin-temperature offset. A non-positive integration factor makes the call
   answer zero and convert nothing, which is the only thing it refuses. */
void vendor_greenteg_cbta_sample_convert(const struct greenteg_cbta_config *cfg,
                                         float *out_heat_flux,
                                         float *out_skin_temperature,
                                         float raw_heat_flux,
                                         float raw_skin_temperature);
void vendor_greenteg_cbta_config_apply(const struct greenteg_cbta_config *cfg);
int vendor_greenteg_cbta_config_validate(const struct greenteg_cbta_config *cfg);

/* One sample. The first two are the converted readings and the third is the
   heart rate: 0x8c0cc runs a mean over each of s0, s1 and s2 and over nothing
   else, and the minute-means then go through the three centres the network
   normalises against -- 31.87/2.42, 84.89/42.60 and 75.85/21.51 -- which is
   skin temperature in degrees, heat flux, and beats per minute. What the
   firmware logs as the interface agrees: "Hourly stats: ... last{worn_ts=%u,
   bpm=%u, skinT=%u, Hflux=%d, ...}" (0x42fb0).

   The last three floats the call takes are read by neither instance: the free
   living step reads s0, s1 and s2, the sport step the same three. They are
   declared as what the shell test and 0x42558 pass. */
int vendor_greenteg_cbta_free_living_update(float heat_flux,
                                            float skin_temperature,
                                            float heart_rate_bpm,
                                            float aux_a, float aux_b,
                                            float aux_c);
int vendor_greenteg_cbta_sport_update(float heat_flux,
                                      float skin_temperature,
                                      float heart_rate_bpm,
                                      float aux_a, float aux_b,
                                      float aux_c);

void vendor_greenteg_cbta_free_living_get(struct greenteg_cbta_result *out);
void vendor_greenteg_cbta_sport_get(struct greenteg_cbta_result *out);

unsigned char vendor_greenteg_cbta_get_flag_a(void);
unsigned char vendor_greenteg_cbta_get_flag_b(void);

/* The free-living instance's model, as the library serialised it. The sport
   instance has no network: 0x8c500 runs windowed statistics over the same
   three readings instead, which is why only one set of weights is here.

   The two tables below are the only thing in the image that names the model.
   Nothing takes their address: the library is compiled -fPIC and reaches its
   own data through a GOT at 0x20007974, so the network runner 0x8c000 loads
   GOT slots 0, 4 and 8 for the activations and pc-relative displacements for
   the tensors. Both tables are in the `.data` initialiser image at 0xefe58
   that the startup copies to 0x200066a8, so their run-time addresses are
   0x200074dc and 0x200074e8. */

/* The three activation slots at 0x200074dc, 0x200074e0 and 0x200074e4 hold
   nn_activation_elu, nn_activation_identity and nn_activation_logistic. They
   are three objects and not an array: the GOT takes the address of each one
   separately, which is also why they stay three RAM items. Each kernel walks
   n floats in place. The network runner hands the GRU cell the logistic for
   its gates and the ELU for its candidate, and the output layer the
   identity. */
/* One tensor of the model. `rank` is 1 for a bias vector and 2 for a weight
   matrix; `count` is the number of floats, which is `rows * cols`; and a
   matrix is row-major with `rows` inputs and `cols` outputs, because the
   dense kernel 0xa759e walks the weight pointer in strides of `cols` floats
   over `rows` terms and indexes the bias by the output. The data stays in
   flash: the initialiser image holds the pointer, not the floats. */
struct greenteg_nn_tensor {
    const float *data;
    unsigned short rank;
    unsigned short count;
    unsigned short rows;
    unsigned short cols;
};

/* The five tensors at 0xf0c98, in the order the runner uses them:
   the output layer's bias (1), its weights (8 -> 1), the GRU's bias
   (48 = six eight-float vectors), its input weights (3 stacked 3 x 8 blocks)
   and its recurrent weights (3 stacked 8 x 8 blocks). */
extern struct greenteg_nn_tensor greenteg_cbta_nn_tensors[5];

/* --------------------------------------------------------- ECGSW2 (ecgsw2)

   The ECG library, named by the firmware's own "[ECG DIAGNOSIS] ECGSW2"
   lines. Unlike the greenTEG drop it is re-entrant: every call takes its
   context, and there are two of them -- the session the diagnosis runs on,
   embedded in ecg_session at +0x28, and the filter's own object at
   0x20013c04. Both are the library's and neither is declared field by field.
   ecg.h holds the entry prototypes and what each call does; what is here is
   only what ecg.h had no shape for.

   That the library is a drop and not firmware is not a shape and lives in
   abi/vendor.yaml: it diagnoses itself through nrf_fprintf in plain English
   where every Withings line goes to wlog behind a bracketed module tag, and
   it is the only thing in the image that calls nrf_fprintf at all. */

/* The 56 bytes ecg_algo_result_copy lifts out of the session at +0x10 and
   ecg_task_state_machine puts into ecg_session+0x30d4. Its first four words
   are the class probabilities the finish normalised, which is what "[ECG
   DIAGNOSIS] ECGSW2 out=%d, %d, %d, %d, qrs_blocks_size=%li" prints. */
#define ECGSW2_RESULT_BYTES 56

struct ecgsw2_result {
    unsigned char bytes[ECGSW2_RESULT_BYTES];
};

/* What vendor_ecgsw2_configure takes from ecg_module_init. The three ranges
   it rejects are what name the three fields: "sampling frequency is not in
   the [%d, %d] range", "Gain is not in the [%d, %d] range", "lfboost mode
   should be between %d and %d". */
struct ecgsw2_config {
    long sampling_frequency_hz;
    long gain;
    long lfboost_mode;
};

extern int vendor_ecgsw2_configure(void *ctx, const struct ecgsw2_config *cfg);

#endif
