/* A small real compiled binary, and the reason this example is not a mock.
 *
 * It links two things: libc, which the client has and apt can name
 * (`libc6`), and libporterprobe.so.1, which the client has never heard of and
 * which travels inside the package. Both facts are in the ELF header and in
 * neither line of porter.yaml -- rule 11: `Depends:` is derived, never
 * hand-written.
 *
 * `-Wl,-rpath,$ORIGIN` in the bake step is what makes the second one work after
 * installation: the loader looks beside the binary, in /usr/lib/<pkg>/, and
 * nothing has to be added to the client's ld.so.conf. A package that skipped it
 * would install perfectly and die with "cannot open shared object file".
 */
#include <stdio.h>
#include <stddef.h>

size_t porter_probe_answer(void);

int main(void)
{
    printf("PROBE_OK answer=%zu\n", porter_probe_answer());
    return 0;
}
