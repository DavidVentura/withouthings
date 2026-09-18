/* The nrfx glue the HWA10 image was built with, recovered from its own SAADC
   bodies (abi/patches/nrfx/saadc-withings.patch documents the measurement).
   Everything not named here comes from the SDK's own glue, which is what
   #include_next picks up.

   NRFX_ASSERT is live in the image and its failure arm is a bare `udf #0`,
   not GCC's `__builtin_trap()` (which is `udf #255`): every NRFX_ASSERT site
   in the image branches over a 0xde00. The trap has to be noreturn or GCC
   merges neighbouring asserts into one range check, which the image does not
   do (saadc_channel_count_get keeps `cbnz`/`cmp #0xff` as two tests).

   NRFX_IRQ_PRIORITY_SET is a real call in the image (nrfx_saadc_init ends
   `movs r0,#7; bl 0x933c6`), to a helper that traps on a priority the
   SoftDevice reserves -- the mask it tests is 0x13, so 0, 1 and 4 are
   refused -- and then tail-calls the SVC wrapper. The SDK's glue sets NVIC
   IPR inline instead. */

#ifndef WITHINGS_NRFX_GLUE_H__
#define WITHINGS_NRFX_GLUE_H__

#include_next <nrfx_glue.h>

#undef NRFX_ASSERT
#define NRFX_ASSERT(expression)                     \
    do {                                            \
        if (!(expression))                          \
        {                                           \
            __asm__ volatile ("udf #0");            \
            __builtin_unreachable();                \
        }                                           \
    } while (0)

void withings_irq_priority_set(IRQn_Type irq_number, uint8_t priority);

#undef NRFX_IRQ_PRIORITY_SET
#define NRFX_IRQ_PRIORITY_SET(irq_number, priority) \
    withings_irq_priority_set(irq_number, priority)

#endif
