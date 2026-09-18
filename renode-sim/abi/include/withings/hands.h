/* HWA10 (ScanWatch 2) application firmware v3411: the step motor module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses.
 *
 * Three Lavet-type steppers, one per hand -- tracker, hour, minute -- each on
 * its own nrfx PWM instance. A position is one step and a revolution is 360 of
 * them, so a position is also the hand's angle on the dial in degrees: the hour
 * hand turns once in twelve hours and the minute hand once in one. Nothing
 * senses where a hand is; the position is dead reckoning, kept in
 * step_motor_positions, which sits inside rambkp_block so it survives the reset
 * the fault handler takes; the boot log prints it as
 * "[M] last min t=%d, h=%d, m=%d". What dblib keeps is the phase of
 * each motor (DBLIB_IE_HANDS_CAL_PHASE) and which motors are fitted
 * (DBLIB_IE_HANDS_PRESENT). Calibration is the wearer's: the watch asks for the
 * hands to be turned to noon with the crown and takes the click as the zero. */
#ifndef WITHINGS_HANDS_H
#define WITHINGS_HANDS_H

#include "withings/hw.h"

enum step_motor_id {
    STEP_MOTOR_TRACKER = 0,
    STEP_MOTOR_HOUR = 1,
    STEP_MOTOR_MINUTE = 2,
    STEP_MOTOR_COUNT = 3,
};

/* What a caller asks for: where to put the hand and how fast. The driver
   plays speed_on drive pulses out of every speed_period slots, the rest idle,
   so the pair is the step rate; `wait` asks the caller's move to block. */
struct step_motor_move {
    unsigned int target;
    unsigned char wait;
    unsigned char speed_on;
    unsigned char speed_period;
    unsigned char pad;
};

/* Direction as the driver indexes its waveform tables with it, and as
   step_motor_pwm_handler applies it to the position. */
enum step_motor_direction {
    STEP_MOTOR_CW = 0,   /* position + 1 */
    STEP_MOTOR_CCW = 1,  /* position - 1 */
};

/* One row per motor, in flash. The PWM channels carry the two ends of the
   winding on 0 and 2 and the common on 1; channel 3 is unconnected. */
struct step_motor_hw {
    unsigned short pwm_instance;
    struct gpio_pin winding_a;
    struct gpio_pin common;
    struct gpio_pin winding_b;
    unsigned short pad;
    unsigned int max_position;   /* 359 on all three */
};

/* The dead-reckoned state of one hand, the eight bytes the driver hands to
   step_motor_play_step and writes back. */
struct step_motor_position {
    unsigned int position;
    unsigned char pad;
    unsigned char phase;         /* which of the direction's two waveforms is next */
    unsigned char phase_pending;
    unsigned char pad2;
};

/* The driver's per-motor object. The committed half at +0x10 is the copy
   step_motor_service takes of the requested half at +0x08 once the motor is
   free, so a request can be staged while a move is in flight. */
struct step_motor {
    const struct step_motor_hw *hw;
    unsigned char id;
    unsigned char requested;
    unsigned short pad;
    unsigned int target;
    unsigned char direction;
    unsigned char pad2[3];
    unsigned int committed_target;
    unsigned char committed_direction;
    unsigned char speed_on;      /* drive pulses per speed_period slots */
    unsigned char speed_period;
    unsigned char pad3;
    unsigned char slot;          /* speed_period counter */
    unsigned char pad4[3];
};

/* One step: three channel compares and the COUNTERTOP that replaces the
   register for the period, played with DECODER.LOAD = WaveForm. */
struct step_motor_waveform {
    unsigned short winding_a;
    unsigned short common;
    unsigned short winding_b;
    unsigned short countertop;
};

/* functions */
/* Builds the four step waveforms and the idle sequences in RAM, probes the
   coils, reloads the calibration phases and nrfx_pwm_init's the three
   instances with PRESCALER 5 and DECODER.LOAD = WaveForm. */
extern void step_motor_init(const unsigned int restored_positions[STEP_MOTOR_COUNT]);
/* step_motor_init behind a once guard; logs "already initialized" after. */
extern void step_motor_init_once(void);
/* Stages a move. Panics if target is above the motor's max_position. */
extern void step_motor_request_move(struct step_motor *motor, const struct step_motor_move *move);
/* The shorter way round, into motor->direction. */
extern void step_motor_pick_direction(struct step_motor *motor, const struct step_motor_move *move);
/* Commits a staged request and plays the first step of it. */
extern void step_motor_service(unsigned char id);
extern void step_motor_service_all(void);
/* Plays one drive pulse: waveform = table[direction][phase], flips the phase,
   and hands SEQ0 plus the idle SEQ1 to nrfx_pwm_complex_playback. */
extern void step_motor_play_step(unsigned char id, struct step_motor_position *position,
                                 unsigned char direction);
/* One slot of the speed pattern: a drive pulse while slot < speed_on, the idle
   waveform otherwise, so the two together set the step rate. */
extern void step_motor_step_toward(unsigned char id, struct step_motor *motor,
                                   struct step_motor_position *position);
/* The nrfx PWM STOPPED callback. Moves the position by one in the committed
   direction -- but only if the phase changed, which is what tells a drive pulse
   from an idle one -- wraps it at max_position, and either takes the next step
   or finishes the move and saves the positions. */
extern void step_motor_pwm_handler(unsigned char id);
/* One step outside the bookkeeping: it drives off a copy of the position and
   writes that copy back, so the stored position does not follow the hand. Only
   the lapping test and the shell's `step_motor move` use it. */
extern void step_motor_step_blocking(unsigned char id, unsigned char direction,
                                     unsigned int speed_on, unsigned int speed_period);
/* Tracker percentage both ways, against the tracker's max_position. */
extern int step_motor_move_tracker_percent(unsigned int percent, unsigned char speed_on,
                                           unsigned char speed_period);
extern unsigned int step_motor_tracker_percent_of_position(unsigned int position, unsigned int max);
/* The nesting counter that holds the position save off while a caller is in
   the middle of a batch of moves. */
extern void step_motor_lock(void);
extern void step_motor_unlock(void);
/* Blocks until all three hands are at the given positions. */
extern void step_motor_wait_positions(const unsigned int positions[STEP_MOTOR_COUNT]);
extern void step_motor_print_positions(void);
extern void step_motor_print_positions_vs_targets(const unsigned int targets[STEP_MOTOR_COUNT]);
/* Winding continuity, the only thing the firmware can sense about a motor:
   both ends are read back as inputs while the common is driven high and then
   low, and both must follow. 0 if the motor is there. */
extern int step_motor_coil_check(unsigned char id, unsigned char unused);
/* Runs step_motor_coil_check over the three motors and writes the two-bits-per
   -motor result to dblib item DBLIB_IE_HANDS_PRESENT. */
extern void step_motor_probe_coils(void);
/* The calibration phase of each motor, in dblib item DBLIB_IE_HANDS_CAL_PHASE:
   one byte per motor, exclusive-or'd with the low bit of the restored position
   to give the phase the next pulse must use. */
extern void step_motor_load_cal_ph(const unsigned int positions[STEP_MOTOR_COUNT], int count);
extern void step_motor_save_cal_ph(void);

/* Decides once, from the model code, whether this watch has hands, and stores
   the answer in dblib item DBLIB_IE_MOVE_HANDS; every boot after reads it back
   and logs "[UI] move_hands feature %sabled". */
extern void ui_move_hands_feature_init(unsigned int model_code);

/* globals */
extern const struct step_motor_hw step_motor_hw_table[STEP_MOTOR_COUNT];
extern struct step_motor *step_motors[STEP_MOTOR_COUNT];
extern struct step_motor_position step_motor_positions[STEP_MOTOR_COUNT];
/* [direction][phase] -> one waveform, filled in at init. */
extern const struct step_motor_waveform *step_motor_step_waveforms[2][2];

#endif
