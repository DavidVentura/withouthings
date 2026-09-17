/* The always-allowed send gate, as a section rather than as a branch edit.
 *
 * abi/prunes.yaml's wpps_tls_tunnel removes everything that could establish the
 * TLS tunnel, so the gate's refusal arm -- taken while the tunnel is selected
 * and not established -- is unreachable in the pruned image, and what is left
 * of the gate is the tail call it made when it allowed the send. */

#include "replace.h"

int wpp_send_gate(void *frame, void *len)
{
    return wpp_send(frame, len);
}
