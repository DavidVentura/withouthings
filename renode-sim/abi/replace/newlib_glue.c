/* The reentrant allocator the image puts in front of FreeRTOS.
 *
 * newlib is configured with nano-malloc, so libc.a carries an sbrk-based
 * _malloc_r; this firmware has no sbrk heap and never pulls it in, because
 * Withings define the four reentrant entry points themselves as shims onto
 * heap_4. These are those shims, read off 0x97f6c..0x97fce and written in C so
 * that the archive's own allocator stays unreferenced.
 */

#include "replace.h"

extern void *pvPortMalloc(unsigned int size);
extern void vPortFree(void *p);
extern void *memcpy(void *to, const void *from, unsigned int n);
extern void *memset(void *to, int c, unsigned int n);

void *_malloc_r(void *reent, unsigned int size)
{
    (void)reent;
    return pvPortMalloc(size);
}

void _free_r(void *reent, void *p)
{
    (void)reent;
    vPortFree(p);
}

void *_realloc_r(void *reent, void *p, unsigned int size)
{
    void *q;

    (void)reent;
    if (p == 0) {
        return pvPortMalloc(size);
    }
    if (size == 0) {
        vPortFree(p);
        return 0;
    }
    q = pvPortMalloc(size);
    if (q == 0) {
        return 0;
    }
    /* The image reads `size` bytes out of the old block, whose length heap_4
     * does not report; growing a block therefore copies past its end. */
    memcpy(q, p, size);
    vPortFree(p);
    return q;
}

void *_calloc_r(void *reent, unsigned int n, unsigned int size)
{
    void *p;

    (void)reent;
    n *= size;
    p = pvPortMalloc(n);
    if (p != 0) {
        memset(p, 0, n);
    }
    return p;
}
