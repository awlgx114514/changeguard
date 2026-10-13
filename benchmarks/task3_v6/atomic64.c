#include <windows.h>

__declspec(dllexport) LONG64 __cdecl task3_interlocked_compare_exchange64(
    volatile LONG64 *destination,
    LONG64 exchange,
    LONG64 comparand
) {
    return InterlockedCompareExchange64(destination, exchange, comparand);
}
