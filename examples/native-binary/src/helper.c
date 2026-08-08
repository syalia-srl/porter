/* The library half of the gallery's native payload.
 *
 * It exists so `probe` links something that is NOT on the client: the bake step
 * builds it as `libporterprobe.so.1` and the package ships it, which is the
 * shape ainbox's engine has (a compiled `llama-server` beside hand-picked CUDA
 * libraries). porter must therefore leave it out of `Depends:` -- a soname the
 * payload provides for itself is not a system dependency, and asking apt for it
 * on an airgapped client is asking for a package no mirror has ever carried.
 */
#include <stddef.h>

size_t porter_probe_answer(void)
{
    return 42;
}
