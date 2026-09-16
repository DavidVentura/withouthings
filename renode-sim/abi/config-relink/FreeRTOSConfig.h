/* FreeRTOSConfig.h for the relink: abi/refbuild.sh's converged config plus the
   three corrections abi/boundary.yaml argues for from the image's own struct
   layouts (Queue_t 0x54 with pxQueueSetContainer at 0x48, TCB_t 0x60 with
   pxTaskTag at 0x54). */
#define configUSE_TRACE_FACILITY      1
#define configUSE_APPLICATION_TASK_TAG 1
#define configMAX_TASK_NAME_LEN       12
#define configRECORD_STACK_HIGH_ADDRESS 1
#ifndef REF_ASSERT
#define REF_ASSERT                    3
#endif
#define ref_assert_log                vAssertCalled
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include "nrf.h"
#include "nrf_assert.h"

#define FREERTOS_USE_RTC      0
#define FREERTOS_USE_SYSTICK  1
#define configTICK_SOURCE     FREERTOS_USE_RTC

#define configUSE_PREEMPTION                    1
#ifndef configUSE_PORT_OPTIMISED_TASK_SELECTION
#define configUSE_PORT_OPTIMISED_TASK_SELECTION 1
#endif
#define configUSE_TICKLESS_IDLE                 1
#define configUSE_TICKLESS_IDLE_SIMPLE_DEBUG    1
#define configCPU_CLOCK_HZ                      ( SystemCoreClock )
#define configTICK_RATE_HZ                      1000
#ifndef configMAX_PRIORITIES
#define configMAX_PRIORITIES ( 5 )
#endif
#define configMINIMAL_STACK_SIZE                ( 60 )
#define configTOTAL_HEAP_SIZE                   ( 16384 )
#ifndef configMAX_TASK_NAME_LEN
#define configMAX_TASK_NAME_LEN ( 16 )
#endif
#define configUSE_16_BIT_TICKS                  0
#define configIDLE_SHOULD_YIELD                 1
#define configUSE_MUTEXES                       1
#define configUSE_RECURSIVE_MUTEXES             1
#define configUSE_COUNTING_SEMAPHORES           1
#define configUSE_ALTERNATIVE_API               0
#ifndef configQUEUE_REGISTRY_SIZE
#define configQUEUE_REGISTRY_SIZE 0
#endif
#define configUSE_QUEUE_SETS                    1
#ifndef configUSE_TIME_SLICING
#define configUSE_TIME_SLICING 1
#endif
#define configUSE_NEWLIB_REENTRANT              0
#ifndef configENABLE_BACKWARD_COMPATIBILITY
#define configENABLE_BACKWARD_COMPATIBILITY 0
#endif
#define configSUPPORT_STATIC_ALLOCATION         1
#define configSUPPORT_DYNAMIC_ALLOCATION        1
#define configUSE_TASK_NOTIFICATIONS            1
#define configUSE_IDLE_HOOK                     0
#define configUSE_TICK_HOOK                     0
#ifndef configCHECK_FOR_STACK_OVERFLOW
#define configCHECK_FOR_STACK_OVERFLOW 2
#endif
#define configUSE_MALLOC_FAILED_HOOK            0
#ifndef configGENERATE_RUN_TIME_STATS
#define configGENERATE_RUN_TIME_STATS 0
#endif
#ifndef configUSE_TRACE_FACILITY
#define configUSE_TRACE_FACILITY 0
#endif
#define configUSE_STATS_FORMATTING_FUNCTIONS    0
#define configUSE_CO_ROUTINES                   0
#define configMAX_CO_ROUTINE_PRIORITIES         ( 2 )
#ifndef configUSE_TIMERS
#define configUSE_TIMERS 1
#endif
#define configTIMER_TASK_PRIORITY               ( 2 )
#define configTIMER_QUEUE_LENGTH                32
#define configTIMER_TASK_STACK_DEPTH            ( 80 )
#define configEXPECTED_IDLE_TIME_BEFORE_SLEEP   2

/* REF_ASSERT picks the assert flavour; the variants build both, because which
   one the image used is only decidable by matching. __builtin_trap() is the udf
   the image's assert path ends in. */
#if REF_ASSERT == 1
#define configASSERT( x ) do { if( !( x ) ) { __builtin_trap(); } } while( 0 )
#elif REF_ASSERT == 2
#define configASSERT( x ) ( ( void ) 0 )
#elif REF_ASSERT == 3
/* The image's assert is a logging call taking __FILE__ in r0 and __LINE__ in r1,
   followed by an infinite loop (e.g. xQueueGenericSend @0x73aa4). */
extern void ref_assert_log( const char *file, unsigned int line );
#define configASSERT( x ) do { if( !( x ) ) { ref_assert_log( __FILE__, __LINE__ ); for( ;; ) {} } } while( 0 )
#endif
/* REF_ASSERT == 0 leaves configASSERT undefined, which also drops the
   configASSERT_DEFINED code (the volatile sizeof(StaticTask_t) store in
   xTaskCreateStatic) -- the image has no such store. */

#define INCLUDE_vTaskPrioritySet                1
#define INCLUDE_uxTaskPriorityGet               1
#define INCLUDE_vTaskDelete                     1
#define INCLUDE_vTaskSuspend                    1
#define INCLUDE_xResumeFromISR                  1
#define INCLUDE_vTaskDelayUntil                 1
#define INCLUDE_vTaskDelay                      1
#define INCLUDE_xTaskGetSchedulerState          1
#define INCLUDE_xTaskGetCurrentTaskHandle       1
#define INCLUDE_uxTaskGetStackHighWaterMark     1
#define INCLUDE_xTaskGetIdleTaskHandle          1
#define INCLUDE_xTimerGetTimerDaemonTaskHandle  1
#define INCLUDE_pcTaskGetTaskName               1
#define INCLUDE_eTaskGetState                   1
#define INCLUDE_xEventGroupSetBitFromISR        1
#define INCLUDE_xTimerPendFunctionCall          1

/* basepri 0xe0 kernel / 0xc0 max-syscall as traced, with 3 priority bits. */
#define configLIBRARY_LOWEST_INTERRUPT_PRIORITY      0x7
#define configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY 0x6
#define configKERNEL_INTERRUPT_PRIORITY      configLIBRARY_LOWEST_INTERRUPT_PRIORITY
#define configMAX_SYSCALL_INTERRUPT_PRIORITY configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY

#define vPortSVCHandler     SVC_Handler
#define xPortPendSVHandler  PendSV_Handler
/* RTC2, not the SDK example's RTC1: rtc2_init @0x74050 writes PRESCALER 0x20. */
#define xPortSysTickHandler RTC2_IRQHandler
#define configSYSTICK_CLOCK_HZ ( 32768UL )

#if !(defined(__ASSEMBLY__) || defined(__ASSEMBLER__))
#include "nrf.h"
#ifdef __NVIC_PRIO_BITS
#define configPRIO_BITS __NVIC_PRIO_BITS
#else
#error "This port requires __NVIC_PRIO_BITS to be defined"
#endif
#endif

#define configUSE_DISABLE_TICK_AUTO_CORRECTION_DEBUG 0

#endif
